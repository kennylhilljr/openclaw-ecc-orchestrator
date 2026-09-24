"""Envelope rendering, exit codes and redaction for everything the CLI prints.

Every printed string passes through the runtime's redaction engine: JSON
output through `redact_obj`, human lines through `Redactor.redact`.
"""

import json

from ..handoffs.redaction import Redactor, redact_obj
from ..runs.envelope import envelope

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_USER_ACTION = 3

ENVELOPE_KEYS = ("ok", "operation", "changed", "checks", "warnings", "required_user_actions", "rollback_checkpoint")

_REDACTOR = Redactor()


def redact(obj):
    return redact_obj(obj, _REDACTOR)


def redact_line(text):
    return _REDACTOR.redact(str(text))


def exit_code(env):
    """0 ok; 3 when the operation stopped for an approval or a user action;
    1 for any other failure. Usage errors (2) never reach an envelope here."""
    if env.get("ok"):
        return EXIT_OK
    return EXIT_USER_ACTION if env.get("required_user_actions") else EXIT_FAILED


def usage_envelope(message, check_name="usage"):
    return envelope("usage", ok=False, checks=[{"name": check_name, "ok": False, "detail": message}],
                    data={"error": message})


def public(env):
    """The envelope as printed: the standard keys plus `data` when present."""
    out = {k: env.get(k) for k in ENVELOPE_KEYS}
    out["checks"] = list(out["checks"] or [])
    out["warnings"] = list(out["warnings"] or [])
    out["required_user_actions"] = list(out["required_user_actions"] or [])
    if env.get("data") is not None:
        out["data"] = env["data"]
    return redact(out)


def write_json(stream, obj):
    stream.write(json.dumps(redact(obj), sort_keys=True, default=str) + "\n")
    stream.flush()


def _fmt_action(action):
    parts = [action.get("kind", "action")]
    target = "/".join(str(x) for x in (action.get("run_id"), action.get("unit_id")) if x)
    if target:
        parts.append(target)
    if action.get("request_id"):
        parts.append(f"request {action['request_id']}")
    if action.get("detail"):
        parts.append(str(action["detail"]))
    return " ".join(parts)


def render_human(env, stream, verbose=False, summary_lines=()):
    """Concise summary: status line, operation specific lines, failed checks,
    warnings and required user actions; all checks and data with --verbose."""
    doc = public(env)
    status = "ok" if doc["ok"] else "FAILED"
    changed = "changed" if doc["changed"] else "no changes"
    lines = [f"{doc['operation']}: {status} ({changed})"]
    lines += [f"  {line}" for line in summary_lines if line]
    for chk in doc["checks"]:
        if verbose or not chk.get("ok"):
            mark = "pass" if chk.get("ok") else "FAIL"
            detail = f": {chk['detail']}" if chk.get("detail") not in (None, "") else ""
            lines.append(f"  [{mark}] {chk.get('name')}{detail}")
    for warning in doc["warnings"]:
        lines.append(f"  warning: {warning}")
    for action in doc["required_user_actions"]:
        lines.append(f"  action required: {_fmt_action(action)}")
    if doc.get("rollback_checkpoint"):
        lines.append(f"  rollback: {json.dumps(doc['rollback_checkpoint'], sort_keys=True, default=str)}")
    if verbose and doc.get("data") is not None:
        lines.append("  data:")
        text = json.dumps(doc["data"], indent=2, sort_keys=True, default=str)
        lines += [f"    {line}" for line in text.splitlines()]
    for line in lines:
        stream.write(redact_line(line) + "\n")
    stream.flush()
