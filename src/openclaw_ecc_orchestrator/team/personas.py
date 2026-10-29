"""Role prompts and verdict parsing (TEAM-DELIVERY-v2 sections 3 and 6.3).

Checklists are adapted from the agent-engineers persona prompts (pr_reviewer,
security_reviewer, qa, architect, designer); their Linear, Slack and
report-file mechanics are dropped. Every role ends with one parsable line.
"""

import json
import os
import re

REVIEW_MARK = "ECC_REVIEW"
VERIFY_MARK = "ECC_VERIFY"
REVIEW_VERDICTS = ("approved", "changes_requested", "rejected")
VERIFY_VERDICTS = ("pass", "fail", "inconclusive")
SEVERITIES = ("blocker", "major", "minor")

TITLES = {
    "pr_reviewer": "PR reviewer",
    "security_reviewer": "security reviewer (information security officer)",
    "architect": "architect",
    "designer": "designer",
}

CHECKLISTS = {
    "pr_reviewer": [
        "Does the change do what the unit description and its design sections say, with the names the design "
        "owns (modules, classes, functions, settings keys)?",
        "Are the unit's acceptance tests present, meaningful (they would fail if the behavior broke), and do "
        "they carry their test ids first in the docstring?",
        "Does it stay inside the unit scope and follow the repository rules?",
        "Error handling: no bare excepts, no swallowed errors, no raw exception text returned to callers.",
        "Is it as simple as the design allows (no dead code, no speculative abstraction, no duplicated logic)?",
        "No secret, token or credential in code, tests, fixtures or logs.",
    ],
    "security_reviewer": [
        "OWASP Top 10 as it applies: broken access control, cryptographic failures, injection (SQL, command, "
        "path), insecure design, misconfiguration, vulnerable components, authentication failures, integrity "
        "failures, logging failures, SSRF.",
        "Secrets: read only through the designated secret store and type; never in files, argv, logs, errors, "
        "tool results, fixtures or test snapshots; redaction applies to every output path.",
        "Credentials attached only to their own origin; no credential in a URL unless the design allows it.",
        "Network and files: downloads and fetches confined to allowed hosts and paths; no caller-chosen local "
        "paths; no SQL or file access beyond what the design allows.",
        "Untrusted content (fetched text) is labeled and never executed or interpreted as instructions.",
        "Tests prove the controls (negative tests for leaks, traversal, SSRF), not only the happy path.",
    ],
    "architect": [
        "The change matches the design's module boundaries, layering and ownership of names.",
        "Contracts other units depend on (types, protocols, schemas, migrations) are complete, stable and "
        "documented where the design says; no hidden coupling or import-time side effects.",
        "Decisions recorded in the design and ADRs are followed; any deviation is stated and justified.",
        "Nothing in the change makes a later planned unit harder or forces it to edit this unit's files.",
    ],
    "designer": [
        "Pages match the UI requirements: the information shown, states (empty, loading, error) and actions.",
        "Accessibility basics (WCAG 2.1 AA): labels, keyboard use, focus, contrast, semantic markup.",
        "Consistency with the rest of the UI: layout, wording, no em or en dashes in UI text.",
    ],
}

_SEVERITY_RULES = (
    "Finding severity: blocker (must be fixed before merge: wrong or missing behavior, missing or hollow "
    "required tests, a security issue, a scope or rule violation, a name that differs from the design), major "
    "(should be fixed now), minor (note only).\n"
    "Verdict: approved only when there is no blocker; changes_requested when the coder can fix the blockers; "
    "rejected only when the approach itself is wrong for the design and needs an owner decision.")


LESSONS_RULE = (
    "Lessons (TEAM-DELIVERY v7): if this change shows that the repository's own instructions (AGENTS.md, a skill "
    "in .agents/skills, or the specification) should say something they do not, so that future units avoid what "
    "you found, add it to \"lessons\": one rule per item, in your own area, naming the file or skill it belongs "
    "in. Only lessons that would have prevented a finding or that apply beyond this unit; an empty list is normal.")
LESSONS_JSON = ', "lessons": [{"practice": "<file or skill>", "lesson": "<one rule>"}]'


def review_prompt(role, pack, head):
    title = TITLES.get(role, role)
    checklist = "\n".join(f"- {item}" for item in CHECKLISTS[role])
    return "\n\n".join([
        f"You are the {title} on an agent delivery team. Another agent wrote this change; you did not. "
        "Review it from the evidence below (you may also read repository files if your tools allow). "
        "Do not modify any file.",
        "Checklist:\n" + checklist,
        _SEVERITY_RULES,
        LESSONS_RULE,
        "End your reply with exactly one line of single-line JSON, nothing after it:\n"
        f'{REVIEW_MARK} {{"verdict": "approved|changes_requested|rejected", "head": "{head}", '
        '"findings": [{"severity": "blocker|major|minor", "where": "<path:line>", "what": "<text>"}]'
        + LESSONS_JSON + '}',
        "# Evidence\n\n" + pack,
    ])


def verify_prompt(pack, head):
    return "\n\n".join([
        "You are the QA verifier on an agent delivery team. Another agent wrote this change. You are in a "
        "throwaway checkout of the verified head; anything you change here is discarded. Do not commit.",
        "Verify the result the way its user or calling code will use it, not by re-reading the tests:\n"
        "- run the acceptance commands and confirm they pass;\n"
        "- exercise the user-facing behavior the unit adds (CLI commands, MCP tools, library calls, files "
        "written), including edge and invalid inputs;\n"
        "- compare the real outputs (shapes, values, errors, messages) with the design references and the "
        "requirement ids the unit traces to;\n"
        "- for a unit with no user-facing surface, check its contract with a sample of realistic inputs.",
        "A check fails when the real output differs from what the design says, even if the tests pass.",
        "A check your environment stops before it can observe the behavior (no network, a missing tool, a "
        "sandbox refusal) has not failed: give it \"ok\": false plus \"blocked\": \"<what stopped it>\", never "
        "a blocker finding. When every check that did not pass was blocked, the verdict is inconclusive, not fail.",
        LESSONS_RULE,
        "End your reply with exactly one line of single-line JSON, nothing after it:\n"
        f'{VERIFY_MARK} {{"verdict": "pass|fail|inconclusive", "head": "{head}", "checks": [{{"what": '
        '"<behavior>", "how": "<command or call>", "expected": "<text>", "actual": "<text>", "ok": true, '
        '"blocked": null}], "findings": [{"severity": "blocker|major|minor", "what": "<text>"}]'
        + LESSONS_JSON + '}',
        "# Evidence\n\n" + pack,
    ])


def parse_verdict(text, mark, head):
    """(verdict dict, None) or (None, problem). The last `mark` line wins."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip().startswith(mark)]
    if not lines:
        return None, f"no {mark} line"
    raw = lines[-1].strip()[len(mark):].strip()
    try:
        doc = json.loads(raw)
    except ValueError:
        m = re.search(r"\{.*\}", raw)
        try:
            doc = json.loads(m.group(0)) if m else None
        except ValueError:
            doc = None
    if not isinstance(doc, dict):
        return None, f"{mark} line is not JSON"
    allowed = REVIEW_VERDICTS if mark == REVIEW_MARK else VERIFY_VERDICTS
    if doc.get("verdict") not in allowed:
        return None, f"verdict {doc.get('verdict')!r} is not one of {', '.join(allowed)}"
    if not isinstance(doc.get("head"), str) or not head.startswith(doc["head"]) or len(doc["head"]) < 7:
        return None, f"verdict names head {doc.get('head')!r}, not the verified head"
    findings = [f for f in (doc.get("findings") or []) if isinstance(f, dict)]
    doc["findings"] = findings
    doc["lessons"] = [{"practice": str(x.get("practice") or "")[:200], "lesson": str(x["lesson"])[:1000]}
                      for x in (doc.get("lessons") or []) if isinstance(x, dict) and x.get("lesson")][:10]
    if mark == REVIEW_MARK and doc["verdict"] == "approved" and any(f.get("severity") == "blocker"
                                                                     for f in findings):
        doc["verdict"] = "changes_requested"
        doc["downgraded"] = "approved with a blocker finding"
    if mark == VERIFY_MARK:
        checks = [c for c in (doc.get("checks") or []) if isinstance(c, dict)]
        doc["checks"] = checks
        blocked = [c for c in checks if c.get("ok") is not True and c.get("blocked")]
        failed = [c for c in checks if c.get("ok") is False and not c.get("blocked")]
        if failed and doc["verdict"] != "fail":
            doc["downgraded"] = f"{doc['verdict']} with a failed check"
            doc["verdict"] = "fail"
        elif blocked and not failed and doc["verdict"] != "inconclusive" and not any(
                f.get("severity") == "blocker" for f in findings):
            # The environment, not the change, stopped these checks (live
            # finding, rm-m0b M0-02: no network for `uv run`).
            doc["downgraded"] = f"{doc['verdict']} with only blocked checks"
            doc["verdict"] = "inconclusive"
    return doc, None


STEWARD_MARK = "ECC_STEWARD"


def steward_prompt(lessons, ownership, ledger, spec_hint):
    """The delivery lead as practices steward (TEAM-DELIVERY v7 4.6)."""
    owned = "\n".join(f"- {role}: {', '.join(globs) or '(proposals only)'}" for role, globs in ownership.items())
    items = "\n".join(f"- {x['id']} [{x.get('role')}, {x.get('source')}, unit {x.get('unit') or '-'}]"
                      f"{' (' + x['practice'] + ')' if x.get('practice') else ''}: {x.get('what')}" for x in lessons)
    return "\n\n".join([
        "You are the delivery lead acting as practices steward on an agent delivery team. The team's personas "
        "recorded the lessons below while building. Turn the ones that generalize into the repository's own "
        "instructions, so units that start after you are guided by them.",
        "Rules:\n"
        "- Edit only the practice files each lesson's persona owns (listed below), plus the ledger.\n"
        "- Tighten or clarify only: add a rule, a checklist item, an example, a command. Never remove or relax a "
        "rule, a test requirement or a check; list such ideas under \"Proposed for the owner\" in the ledger "
        "instead.\n"
        f"- Never edit the specification ({spec_hint}); a lesson that needs a specification change goes under "
        "\"Proposed for the owner\" with the section it concerns.\n"
        "- Keep edits short and in each file's existing style; merge duplicates; skip lessons that are one-off.\n"
        f"- In the ledger ({ledger}) add one line per lesson you applied, proposed or skipped: id, date, the "
        "file you changed (or why not), and the rule in one sentence. Create the ledger if it is missing, with "
        "sections \"Applied\" and \"Proposed for the owner\".\n"
        "- Commit on the current branch with a message starting \"Practices:\" that lists the lesson ids. "
        "Never push or switch branches.",
        "Who owns which practice files:\n" + owned,
        "Lessons:\n" + items,
        "End your reply with exactly one line of single-line JSON, nothing after it:\n"
        f'{STEWARD_MARK} {{"applied": ["L0001"], "proposed": ["L0002"], "skipped": ["L0003"]}}',
    ])


def practices_review_prompt(role, lessons, pack, head):
    """A persona reviews the steward's change to the practice files in its area."""
    title = TITLES.get(role, role)
    items = "\n".join(f"- {x['id']}: {x.get('what')}" for x in lessons)
    return "\n\n".join([
        f"You are the {title} on an agent delivery team. The delivery lead changed the repository's practice "
        "files (agent instructions and skills) from lessons the team recorded, including lessons from your "
        "area. Review the change below. Do not modify any file.",
        "Checklist:\n- Each edit is supported by a cited lesson and states a rule future units can follow.\n"
        "- It only tightens or clarifies: no rule, test requirement or check is removed or relaxed.\n"
        "- Nothing contradicts the specification, the requirements or another practice file.\n"
        "- The specification itself is not edited; spec ideas are only proposed in the ledger.\n"
        "- It is short, specific and in the file's style.",
        _SEVERITY_RULES,
        "End your reply with exactly one line of single-line JSON, nothing after it:\n"
        f'{REVIEW_MARK} {{"verdict": "approved|changes_requested|rejected", "head": "{head}", '
        '"findings": [{"severity": "blocker|major|minor", "where": "<path:line>", "what": "<text>"}]}',
        "Lessons cited:\n" + items,
        "# The change\n\n" + pack,
    ])


# == repository personas (SDD) ==
# A role can act as a persona the target repository defines in
# `.claude/agents/<name>.md` (the sdd_framework persona files). team.json maps
# roles to persona names under "personas"; the file is read from the main
# repository, never from a unit worktree, so a unit cannot rewrite the persona
# its reviewers play.
PERSONA_DIR = (".claude", "agents")
PERSONA_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
PERSONA_ROLES = ("coder", "steward")   # roles outside config.ROLES that may take a persona
MAX_PERSONA_CHARS = 8000


def persona_path(repo, name):
    return os.path.join(repo, *PERSONA_DIR, name + ".md")


def load_persona(repo, name):
    """The body of the persona file `name` in `repo` without its front matter,
    capped at MAX_PERSONA_CHARS, or None when the name is invalid or the file
    is missing or empty."""
    if not repo or not isinstance(name, str) or not PERSONA_NAME_RE.match(name):
        return None
    try:
        with open(persona_path(repo, name), encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    text = text.strip()
    return text[:MAX_PERSONA_CHARS] or None


def persona_for(team_cfg, role):
    """The persona name team.json assigns to `role`, or None."""
    return ((team_cfg or {}).get("personas") or {}).get(role)


def missing_personas(repo, team_cfg):
    """Persona names team.json assigns that `repo` does not define."""
    names = sorted(set(((team_cfg or {}).get("personas") or {}).values()))
    return [n for n in names if load_persona(repo, n) is None]


def with_persona(prompt, name, text):
    """`prompt` preceded by the persona definition, when there is one."""
    if not text:
        return prompt
    return "\n\n".join([
        f"You act as the `{name}` persona of this repository. Its definition follows; it sets your scope, "
        "inputs and limits. Where it conflicts with the instructions after it, the instructions after it win "
        "(they set this turn's task and the exact reply format).",
        f"<persona name=\"{name}\">\n{text}\n</persona>",
        prompt,
    ])
