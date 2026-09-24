"""Compatibility re-export of the single redaction engine.

All detection and redaction lives in :mod:`openclaw_ecc_orchestrator.handoffs.redaction`.
"""

from ..handoffs.redaction import (  # noqa: F401
    MASK,
    MIN_SECRET_LEN,
    REDACTED,
    Redactor,
    StreamRedactor,
    contains_secret,
    looks_secret_name,
    redact_argv,
    redact_obj,
    redact_text,
)

__all__ = ["MASK", "MIN_SECRET_LEN", "REDACTED", "Redactor", "StreamRedactor", "contains_secret",
           "looks_secret_name", "redact_argv", "redact_obj", "redact_text"]
