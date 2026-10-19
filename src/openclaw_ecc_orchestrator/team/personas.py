"""Role prompts and verdict parsing (TEAM-DELIVERY-v2 sections 3 and 6.3).

Checklists are adapted from the agent-engineers persona prompts (pr_reviewer,
security_reviewer, qa, architect, designer); their Linear, Slack and
report-file mechanics are dropped. Every role ends with one parsable line.
"""

import json
import re

REVIEW_MARK = "ECC_REVIEW"
VERIFY_MARK = "ECC_VERIFY"
REVIEW_VERDICTS = ("approved", "changes_requested", "rejected")
VERIFY_VERDICTS = ("pass", "fail")
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


def review_prompt(role, pack, head):
    title = TITLES.get(role, role)
    checklist = "\n".join(f"- {item}" for item in CHECKLISTS[role])
    return "\n\n".join([
        f"You are the {title} on an agent delivery team. Another agent wrote this change; you did not. "
        "Review it from the evidence below (you may also read repository files if your tools allow). "
        "Do not modify any file.",
        "Checklist:\n" + checklist,
        _SEVERITY_RULES,
        "End your reply with exactly one line of single-line JSON, nothing after it:\n"
        f'{REVIEW_MARK} {{"verdict": "approved|changes_requested|rejected", "head": "{head}", '
        '"findings": [{"severity": "blocker|major|minor", "where": "<path:line>", "what": "<text>"}]}',
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
        "End your reply with exactly one line of single-line JSON, nothing after it:\n"
        f'{VERIFY_MARK} {{"verdict": "pass|fail", "head": "{head}", "checks": [{{"what": "<behavior>", '
        '"how": "<command or call>", "expected": "<text>", "actual": "<text>", "ok": true}], '
        '"findings": [{"severity": "blocker|major|minor", "what": "<text>"}]}',
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
    if mark == REVIEW_MARK and doc["verdict"] == "approved" and any(f.get("severity") == "blocker"
                                                                     for f in findings):
        doc["verdict"] = "changes_requested"
        doc["downgraded"] = "approved with a blocker finding"
    if mark == VERIFY_MARK:
        checks = [c for c in (doc.get("checks") or []) if isinstance(c, dict)]
        doc["checks"] = checks
        if doc["verdict"] == "pass" and any(c.get("ok") is False for c in checks):
            doc["verdict"] = "fail"
            doc["downgraded"] = "pass with a failed check"
    return doc, None
