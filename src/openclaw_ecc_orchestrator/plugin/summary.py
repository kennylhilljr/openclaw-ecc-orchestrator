"""Summarize what the operator must do next, for `required_user_actions`."""

import time

_STATE_ACTIONS = {
    "interrupted": ("reassign_interrupted_unit", "worker process is gone; reassign explicitly (a handoff is recorded)"),
    "blocked": ("unblock_unit", "unit is blocked"),
    "failed": ("retry_or_reassign_failed_unit", "unit failed; retry with the same owner or reassign"),
}

# needs_user reasons where the unit's worker process may still be alive.
_PROCESS_CHECK_REASONS = ("lease_lost", "liveness_unverified")


def summarize_required_user_actions(run, pending_approvals=(), queue_state=None, now=None):
    now = time.time() if now is None else now
    run_id = run.get("run_id")
    actions = []
    for req in pending_approvals or ():
        if req.get("run_id") != run_id or req.get("status", "pending") != "pending":
            continue
        if float(req.get("expires_at", 0)) <= now:
            continue
        actions.append({"kind": "approve", "run_id": run_id, "unit_id": req.get("unit_id"),
                        "request_id": req.get("request_id"), "action": req.get("action"),
                        "expires_at": req.get("expires_at"), "detail": req.get("summary", "")})
    for uid, unit in sorted((run.get("units") or {}).items()):
        state, reason = unit.get("state"), unit.get("last_reason")
        if state == "needs_user" and reason in _PROCESS_CHECK_REASONS:
            pid = (unit.get("process") or {}).get("pid")
            actions.append({"kind": "verify_worker_process", "run_id": run_id, "unit_id": uid,
                            "detail": f"pid {pid} may still be running ({reason}); stop it or confirm it is "
                                      f"gone before reassigning"})
        elif state == "needs_user":
            kind = "budget_exhausted" if reason == "budget_exhausted" else "resolve_needs_user"
            detail = "raise the budget or cancel the unit" if kind == "budget_exhausted" else (reason or "input needed")
            actions.append({"kind": kind, "run_id": run_id, "unit_id": uid, "detail": detail})
        elif state in _STATE_ACTIONS:
            kind, text = _STATE_ACTIONS[state]
            detail = f"{text} (owner {unit.get('owner')}, reason {reason})"
            actions.append({"kind": kind, "run_id": run_id, "unit_id": uid, "detail": detail})
    merged = {uid for uid, u in (run.get("units") or {}).items() if u.get("state") == "merged"}
    seen = set()
    for rec in (queue_state or {}).get("history", []):
        if rec.get("run_id") != run_id or rec.get("unit_id") in merged:
            continue
        if rec.get("status") == "conflict" and rec.get("unit_id") not in seen:
            seen.add(rec.get("unit_id"))
            actions.append({"kind": "resolve_conflict", "run_id": run_id, "unit_id": rec.get("unit_id"),
                            "detail": ", ".join(rec.get("conflicts") or [])})
    actions.sort(key=lambda a: (a.get("unit_id") or "", a["kind"]))
    return actions
