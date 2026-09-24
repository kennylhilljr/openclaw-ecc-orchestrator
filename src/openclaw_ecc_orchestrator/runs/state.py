"""Unit state machine."""

PENDING = "pending"
READY = "ready"
ASSIGNED = "assigned"
RUNNING = "running"
VERIFYING = "verifying"
REVIEWING = "reviewing"
QUEUED_FOR_MERGE = "queued_for_merge"
MERGED = "merged"
FAILED = "failed"
CANCELLED = "cancelled"
BLOCKED = "blocked"
NEEDS_USER = "needs_user"
INTERRUPTED = "interrupted"

TRANSITIONS = {
    PENDING: {READY, BLOCKED, CANCELLED},
    READY: {ASSIGNED, BLOCKED, CANCELLED, NEEDS_USER},
    ASSIGNED: {RUNNING, INTERRUPTED, FAILED, CANCELLED, NEEDS_USER},
    RUNNING: {VERIFYING, FAILED, CANCELLED, NEEDS_USER, INTERRUPTED},
    VERIFYING: {REVIEWING, FAILED, CANCELLED, NEEDS_USER, INTERRUPTED},
    REVIEWING: {QUEUED_FOR_MERGE, READY, FAILED, CANCELLED, NEEDS_USER, INTERRUPTED},
    QUEUED_FOR_MERGE: {MERGED, FAILED, BLOCKED, CANCELLED, NEEDS_USER},
    FAILED: {READY, CANCELLED, NEEDS_USER},
    BLOCKED: {READY, CANCELLED, NEEDS_USER},
    NEEDS_USER: {READY, CANCELLED},
    INTERRUPTED: {READY, CANCELLED, NEEDS_USER},
    MERGED: set(),
    CANCELLED: set(),
}

STATES = frozenset(TRANSITIONS)
TERMINAL = frozenset({MERGED, CANCELLED})
# States in which a runner process may still be working. Resume examines only
# these; verifying, reviewing and queued_for_merge are past the runner.
IN_FLIGHT = frozenset({ASSIGNED, RUNNING})
# States that occupy a checkout (used for parallel overlap checks).
ACTIVE = frozenset({ASSIGNED, RUNNING, VERIFYING, REVIEWING, QUEUED_FOR_MERGE})


class IllegalTransition(ValueError):
    pass


def check_transition(current, target):
    if current not in TRANSITIONS:
        raise IllegalTransition(f"unknown state {current!r}")
    if target not in TRANSITIONS:
        raise IllegalTransition(f"unknown state {target!r}")
    if target not in TRANSITIONS[current]:
        raise IllegalTransition(f"illegal transition {current} -> {target}")
