"""Approval consumer: OpenClaw decides, this runtime validates the binding.

A request binds (run_id, unit_id, action, plan_sha256, session_id), an
optional verified head commit (`head_sha`, set by the runtime, never by a
decision) and an expiry. A decision is accepted only when every bound field
matches, the request is still pending and unexpired, and it was never
resolved before. Mismatched decisions are audited but do not consume the
request.

An approved request is usable until its `expires_at`, once: `consume`
re-checks status, expiry and every binding (including `head_sha`) under the
broker lock and marks the request `consumed`. Callers that act on an
approval (the merge queue) must go through `usable`/`consume`; an approval
record handed over by anyone else is never trusted on its own.
"""

import hashlib
import json
import os
import time
import uuid

from ..runs.envelope import SCHEMA_VERSION, check, envelope
from ..runs.fsutil import FileLock, atomic_write_json, read_json
from ..runs.store import iso, valid_identifier

BINDING_FIELDS = ("run_id", "unit_id", "action", "plan_sha256", "session_id")
DECISIONS = ("approved", "rejected")


def plan_digest(obj):
    """sha256 of the canonical JSON of a plan (for example a dry-run result)."""
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ApprovalBroker:
    def __init__(self, state_path, clock=time.time, id_gen=None, emitter=None):
        self.state_path = os.path.abspath(state_path)
        self.clock = clock
        self.id_gen = id_gen or (lambda: uuid.uuid4().hex)
        self.emitter = emitter

    def _lock(self):
        return FileLock(self.state_path + ".lock")

    def state(self):
        if os.path.exists(self.state_path):
            return read_json(self.state_path)
        return {"schema_version": SCHEMA_VERSION, "requests": {}, "rejected_attempts": []}

    def _save(self, st):
        st["schema_version"] = SCHEMA_VERSION
        atomic_write_json(self.state_path, st)

    def _emit(self, event_type, req, data):
        if self.emitter is None:
            return []
        try:
            self.emitter.emit(event_type, req["run_id"], unit_id=req["unit_id"], data=data)
        except Exception as exc:
            return [f"event emission failed: {exc}"]
        return []

    def _expire(self, st):
        now = self.clock()
        changed = False
        for req in st["requests"].values():
            if req["status"] == "pending" and now >= req["expires_at"]:
                req["status"] = "expired"
                changed = True
        return changed

    def pending(self, run_id=None):
        st = self.state()
        now = self.clock()
        return [r for r in st["requests"].values()
                if r["status"] == "pending" and now < r["expires_at"] and (run_id is None or r["run_id"] == run_id)]

    def get(self, request_id):
        return self.state()["requests"].get(request_id)

    def request(self, *, run_id, unit_id, action, plan_sha256, session_id, ttl_s=3600, summary="", dry_run=False,
                head_sha=None):
        op = "approval.request"
        checks = [
            check("run_id_valid", valid_identifier(run_id), str(run_id)),
            check("unit_id_valid", valid_identifier(unit_id), str(unit_id)),
            check("action_present", isinstance(action, str) and bool(action), str(action)),
            check("plan_sha256_valid", isinstance(plan_sha256, str) and len(plan_sha256) == 64, ""),
            check("session_present", isinstance(session_id, str) and bool(session_id), ""),
            check("head_sha_valid", head_sha is None or (isinstance(head_sha, str) and len(head_sha) >= 40
                                                         and all(ch in "0123456789abcdef" for ch in head_sha)),
                  str(head_sha)),
        ]
        if not all(c["ok"] for c in checks):
            return envelope(op, ok=False, checks=checks)
        now = self.clock()
        req = {
            "schema_version": SCHEMA_VERSION,
            "request_id": f"apr-{self.id_gen()}",
            "run_id": run_id, "unit_id": unit_id, "action": action, "plan_sha256": plan_sha256,
            "session_id": session_id, "summary": summary, "head_sha": head_sha,
            "requested_at": iso(now), "expires_at": now + float(ttl_s), "status": "pending",
        }
        if dry_run:
            return envelope(op, changed=False, checks=checks, data=req)
        with self._lock():
            st = self.state()
            st["requests"][req["request_id"]] = req
            self._save(st)
        payload = {
            "request_id": req["request_id"], "action": action, "plan_sha256": plan_sha256,
            "requesting_session": session_id, "expires_at": req["expires_at"], "summary": summary,
        }
        if head_sha:
            payload["head_sha"] = head_sha
        warnings = self._emit("approval.requested", req, payload)
        return envelope(op, changed=True, checks=checks, warnings=warnings, data=req,
                        required_user_actions=[{"kind": "approve", "run_id": run_id, "unit_id": unit_id,
                                                "request_id": req["request_id"], "detail": summary}])

    def resolve(self, decision):
        op = "approval.resolve"
        if not isinstance(decision, dict):
            return envelope(op, ok=False, checks=[check("decision_is_object", False)])
        with self._lock():
            st = self.state()
            expired_changed = self._expire(st)
            rid = decision.get("request_id")
            req = st["requests"].get(rid) if isinstance(rid, str) else None
            checks = [check("request_known", req is not None, str(rid))]
            if req is not None:
                checks.append(check("request_pending", req["status"] == "pending", req["status"]))
                checks.append(check("not_expired", req["status"] != "expired" and self.clock() < req["expires_at"], ""))
                mismatched = [f for f in BINDING_FIELDS if decision.get(f) != req[f]]
                checks.append(check("binding_matches", not mismatched, ",".join(mismatched)))
            checks.append(check("decision_valid", decision.get("decision") in DECISIONS, str(decision.get("decision"))))
            decided_by = decision.get("decided_by")
            checks.append(check("decided_by_present", isinstance(decided_by, str) and bool(decided_by), ""))
            ok = all(c["ok"] for c in checks)
            if not ok:
                st["rejected_attempts"].append({
                    "request_id": rid if isinstance(rid, str) else None, "at": iso(self.clock()),
                    "failed": [c["name"] for c in checks if not c["ok"]],
                    "claimed": {f: decision.get(f) for f in BINDING_FIELDS},
                })
                self._save(st)
                return envelope(op, ok=False, checks=checks, changed=expired_changed)
            req["status"] = decision["decision"]
            req["resolved_at"] = iso(self.clock())
            req["decided_by"] = decided_by
            record = {
                "schema_version": SCHEMA_VERSION,
                "request_id": rid,
                **{f: req[f] for f in BINDING_FIELDS},
                "decision": decision["decision"],
                "decided_by": decided_by,
                "resolved_at": req["resolved_at"],
                "head_sha": req.get("head_sha"),
                "expires_at": req["expires_at"],
            }
            self._save(st)
        warnings = self._emit("approval.resolved", req, {
            "request_id": rid, "decision": record["decision"], "action": record["action"],
            "decided_by": decided_by,
        })
        return envelope(op, changed=True, checks=checks, warnings=warnings, data=record)

    # == use of a granted approval ==
    def _usable_checks(self, req, request_id, binding):
        checks = [check("request_known", req is not None, str(request_id))]
        if req is None:
            return checks
        checks.append(check("request_approved", req.get("status") == "approved", str(req.get("status"))))
        checks.append(check("not_expired", self.clock() < float(req.get("expires_at", 0)),
                            f"expires_at={req.get('expires_at')}"))
        mismatched = [f for f, v in sorted(binding.items()) if req.get(f) != v]
        checks.append(check("binding_matches", not mismatched, ",".join(mismatched)))
        return checks

    def usable(self, request_id, **binding):
        """Read only: checks that `request_id` is an approved, unexpired,
        unconsumed request whose recorded fields equal `binding` (for example
        run_id, unit_id, action, plan_sha256, session_id, head_sha)."""
        if not isinstance(request_id, str) or not request_id:
            return [check("request_known", False, str(request_id))]
        return self._usable_checks(self.state()["requests"].get(request_id), request_id, binding)

    def consume(self, request_id, **binding):
        """Atomically re-check `usable` and mark the request consumed."""
        op = "approval.consume"
        if not isinstance(request_id, str) or not request_id:
            return envelope(op, ok=False, checks=[check("request_known", False, str(request_id))])
        with self._lock():
            st = self.state()
            req = st["requests"].get(request_id)
            checks = self._usable_checks(req, request_id, binding)
            if not all(c["ok"] for c in checks):
                return envelope(op, ok=False, checks=checks)
            req["status"] = "consumed"
            req["consumed_at"] = iso(self.clock())
            self._save(st)
        return envelope(op, changed=True, checks=checks, data={"request_id": request_id, "status": "consumed"})
