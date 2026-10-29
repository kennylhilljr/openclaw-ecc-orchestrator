"""Repair loop support (TEAM-DELIVERY-v2 sections 5 and 6.2).

Pure functions, no I/O beyond reading a log tail the caller names:

* `classify_runner_failure` tells failures that retrying cannot fix (no login,
  missing CLI, denied tools) from ordinary ones (section 5.3).
* `failure_evidence` gathers what the next attempt needs to know, from
  evidence the orchestrator saw, never from the runner's own summary.
* `failure_brief` renders that evidence for the coder's next prompt (6.2).
* `handoff_repair_prompt` asks for a corrected handoff only (5.2).
"""

import json
import os
import re

MAX_BRIEF_CHARS = 6000
MAX_TAIL_CHARS = 1500
LOG_TAIL_BYTES = 64 * 1024
REPAIR_PROMPT_TOKEN = "{ECC_REPAIR_PROMPT}"

# The handoff template and rules every coding prompt carries; the template
# passes schemas.validate_handoff (tests/test_runners_cli_flags.py).
HANDOFF_TEMPLATE = (
    '{"schema_version": "1.0", "unit_id": "<UNIT_ID>", "status": "succeeded", '
    '"outcome": "<one paragraph>", "files_changed": ["<repo/relative/path>"], '
    '"behavior": "<what now works, one string>", '
    '"commands": [{"command": "<command>", "exit_code": 0, "result": "<short result>"}], '
    '"unresolved_failures": [], "assumptions": ["<assumption>"], "risks": ["<risk>"], '
    '"next_action": "<next step>", '
    '"commit": {"sha": "<full sha>", "branch": "<branch>", "worktree": "<worktree path>"}, '
    '"usage": null, "user_input_required": false, "notes": "<optional>"}')
HANDOFF_RULES = ("Rules: status is one of succeeded, failed, partial, blocked; succeeded needs every "
                 "command exit_code 0 and an empty unresolved_failures; behavior is one string, not a list; "
                 "every command has command, exit_code (integer) and result; files_changed holds "
                 "repository-relative paths inside the unit scope; commit.sha is the full hex SHA; "
                 "usage is null when token counts are unknown; put anything else in notes (a string).")
HANDOFF_FAILURES = ("handoff_missing", "handoff_unreadable", "handoff_too_large", "handoff_invalid")

# (kind, pattern). Matched case-insensitively against the runner's output tail.
_STOP_PATTERNS = (
    # Specific phrases only: a unit's own output often mentions 401s or
    # "unauthorized" while testing auth code, which must not stop the unit.
    ("runner_auth", r"not logged in|please run /login|invalid api key|authentication_error|"
                    r"oauth token (has )?expired|run `?codex login|not authenticated with"),
    ("runner_missing", r"(claude|codex): command not found|no such file or directory: '?(claude|codex)|"
                       r"is older than the minimum|unknown option '--"),
)
_DENIAL = re.compile(r'"permission_denials"\s*:\s*\[(.+?)\]\s*,\s*"', re.S)
_TOOL = re.compile(r'"tool_name"\s*:\s*"([^"]+)"')


def read_tail(path, limit=LOG_TAIL_BYTES):
    """Last `limit` bytes of a text file, or "" when it cannot be read."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            return fh.read().decode("utf-8", "replace")
    except (OSError, TypeError):
        return ""


def denied_tools(text):
    """Tool names a Claude print-mode run reports as permission denied."""
    match = _DENIAL.search(text or "")
    return sorted(set(_TOOL.findall(match.group(1)))) if match else []


def classify_runner_failure(text, exit_code):
    """For a runner that exited non-zero: (kind, detail) when retrying cannot
    help, else None. Kinds: runner_auth, runner_missing, runner_permission_denied."""
    if exit_code == 0:
        return None
    lowered = (text or "").lower()
    for kind, pattern in _STOP_PATTERNS:
        found = re.search(pattern, lowered)
        if found:
            return kind, _line_around(text, found.start())
    tools = denied_tools(text)
    if tools:
        return "runner_permission_denied", "denied: " + ", ".join(tools)
    return None


def stop_fix(kind, runner=None):
    """The exact next step for the owner, per stop kind (section 5.3)."""
    name = runner or "the runner"
    return {
        "runner_auth": f"sign {name} in again (for Claude: run `claude`, then /login; for Codex: `codex login`), "
                       "then return the unit to ready",
        "runner_missing": f"put a certified {name} first on PATH (see `ecc doctor`), then return the unit to ready",
        "runner_permission_denied": "platform defect: the runner was denied a tool the unit needs; extend the "
                                    "runner's allowed tools in openclaw-ecc-orchestrator",
    }.get(kind, "see the unit log")


def _line_around(text, index):
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    line = text[start:end if end >= 0 else len(text)]
    return _clip(line.strip(), 300)


def _clip(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _tail(text, limit=MAX_TAIL_CHARS):
    text = str(text or "").strip()
    return text if len(text) <= limit else "..." + text[-(limit - 3):]


def runner_result_text(text):
    """The final message of a Claude or Codex JSON log, when one is present."""
    m = re.search(r'"result"\s*:\s*"((?:[^"\\]|\\.)*)"', text or "")
    if m:
        try:
            return json.loads('"' + m.group(1) + '"')
        except ValueError:
            return m.group(1)
    return ""


def failure_evidence(reason, *, log_text=None, handoff_problems=None, files=None, gates=None,
                     review=None, verification=None):
    """A JSON-safe evidence record for `reason`. Every argument is optional;
    only what applies to the failure is kept."""
    ev = {"reason": reason}
    if log_text:
        final = runner_result_text(log_text)
        ev["runner_output_tail"] = _tail(final or log_text)
        tools = denied_tools(log_text)
        if tools:
            ev["denied_tools"] = tools
    if handoff_problems:
        ev["handoff_problems"] = [_clip(p, 300) for p in handoff_problems][:20]
    if files:
        ev["files"] = [_clip(f, 200) for f in files][:50]
    if gates:
        ev["gates"] = [{"name": g.get("name"), "command": _clip(g.get("command"), 200),
                        "status": g.get("status"), "exit_code": g.get("exit_code"),
                        "output_tail": _tail(g.get("output_tail"), 1200)}
                       for g in gates if g.get("status") != "passed"][:8]
    if review:
        ev["review"] = {"role": review.get("role", "pr_reviewer"), "verdict": review.get("verdict"),
                        "findings": [_clip(json.dumps(f) if not isinstance(f, str) else f, 400)
                                     for f in (review.get("findings") or [])][:20],
                        "notes": _clip(review.get("notes"), 1500)}
    if verification:
        ev["verification"] = {"verdict": verification.get("verdict"),
                              "checks": [c for c in (verification.get("checks") or []) if not c.get("ok")][:15],
                              "findings": (verification.get("findings") or [])[:15]}
    return ev


def failure_brief(unit_id, attempt, max_attempts, evidence):
    """Section 6.2: plain text, at most MAX_BRIEF_CHARS characters."""
    ev = evidence or {}
    reason = ev.get("reason", "unknown")
    lines = [f"Attempt {attempt} of {max_attempts} for {unit_id}. The previous attempt failed: {reason}.",
             "Evidence:"]
    for g in ev.get("gates") or []:
        lines.append(f"- gate {g.get('name')} (`{g.get('command')}`) {g.get('status')}, exit {g.get('exit_code')}; "
                     f"last lines:\n{g.get('output_tail') or '(no output)'}")
    if ev.get("handoff_problems"):
        lines.append("- handoff validation errors: " + "; ".join(ev["handoff_problems"]))
    if ev.get("files"):
        label = "files outside the unit scope" if reason == "out_of_scope_changes" else "files"
        lines.append(f"- {label}: " + ", ".join(ev["files"]))
    if ev.get("review"):
        r = ev["review"]
        lines.append(f"- {r.get('role')} verdict {r.get('verdict')}; findings: " + "; ".join(r.get("findings") or [])
                     + (f"; notes: {r['notes']}" if r.get("notes") else ""))
    if ev.get("verification"):
        v = ev["verification"]
        lines.append("- verifier verdict " + str(v.get("verdict")) + "; failed checks: "
                     + json.dumps(v.get("checks"))[:1500] + "; findings: " + json.dumps(v.get("findings"))[:800])
    if ev.get("denied_tools"):
        lines.append("- tools denied to you: " + ", ".join(ev["denied_tools"]))
    if ev.get("runner_output_tail"):
        lines.append("- your previous run ended with:\n" + ev["runner_output_tail"])
    if len(lines) == 2:
        lines.append("- (no further detail recorded)")
    if reason in HANDOFF_FAILURES:
        lines.append("The code may already be complete: check the acceptance commands first, change code only if "
                     "they fail, and write a correct handoff.")
    else:
        lines.append("Your earlier commits are on this branch. Inspect them (git log, git diff against the base), "
                     "keep what is right, fix the cause above, run the acceptance commands, commit, and write the "
                     "handoff.")
    text = "\n".join(lines)
    if len(text) > MAX_BRIEF_CHARS:
        head, tail = lines[0], lines[-1]
        budget = MAX_BRIEF_CHARS - len(head) - len(tail) - 30
        middle = "\n".join(lines[1:-1])
        text = head + "\n" + middle[:max(0, budget)] + "\n[evidence truncated]\n" + tail
    return text


def handoff_repair_prompt(unit_id, path_token, problems, template, rules):
    """Section 5.2: a short turn that rewrites only the handoff."""
    listed = "; ".join(_clip(p, 300) for p in (problems or [])[:20]) or "the handoff file is missing"
    return "\n".join([
        f"Your work on unit {unit_id} is committed in this worktree, but its handoff file is missing or invalid.",
        f"Problems: {listed}.",
        f"Rewrite only the handoff file at {path_token}. Its folder is writable for you; if your file-editing "
        "tool refuses a path outside the repository, write it from the shell (cat > \"$ECC_HANDOFF_PATH\" "
        "<<'JSON' ... JSON). Never write it anywhere else. Do not change, add or commit "
        "any repository file. Read `git log -1` for the commit sha and branch. Template (replace every <...>):",
        template.replace("<UNIT_ID>", unit_id),
        rules,
    ])
