"""Boundary result envelope shared by runtime operations."""

SCHEMA_VERSION = "1.0"


def check(name, ok, detail=""):
    """Return one structured check entry."""
    return {"name": name, "ok": bool(ok), "detail": detail}


def envelope(
    operation,
    *,
    ok=True,
    changed=False,
    checks=None,
    warnings=None,
    required_user_actions=None,
    rollback_checkpoint=None,
    data=None,
):
    """Build the standard boundary result envelope.

    `data` carries the operation specific payload (for example a dry-run plan).
    """
    result = {
        "ok": bool(ok),
        "operation": operation,
        "changed": bool(changed),
        "checks": list(checks or []),
        "warnings": list(warnings or []),
        "required_user_actions": list(required_user_actions or []),
        "rollback_checkpoint": rollback_checkpoint,
    }
    if data is not None:
        result["data"] = data
    return result
