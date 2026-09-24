"""Run manager: durable unit state, conductor leases, handoffs, resume.

Every mutation happens under the per-run lock and follows the same order:
append events to the log (fsync), then atomically replace the snapshot. On
load, events newer than the snapshot's `last_event_seq` are replayed, so a
crash between the two steps loses nothing. Each unit event carries the full
unit record, which makes the replay a plain overwrite.
"""

import copy
import time
import uuid

from .. import schemas
from ..routing.classify import classify
from . import state as S
from .dag import analyze_plan, normalize_units, plan_sha256
from .envelope import SCHEMA_VERSION, check, envelope
from . import liveness as L
from .store import RunStore, StoreError, iso, valid_identifier


class RunError(RuntimeError):
    pass


class LeaseError(RunError):
    pass


ATTENTION_STATES = {S.NEEDS_USER, S.INTERRUPTED, S.BLOCKED}
# "handoff" is deliberately absent: only record_handoff may write it, after
# validating the document, because the verifying transition trusts it.
ANNOTATION_KEYS = {"verification", "review", "merge", "conflicts", "notes", "escalation"}


class _Tx:
    def __init__(self):
        self.events = []
        self.external = []

    def add(self, event_type, data):
        self.events.append((event_type, data))


def _new_unit_record(unit):
    return {
        "id": unit["id"],
        "state": S.PENDING,
        "owner": None,
        "attempts": 0,
        "attempts_by_tier": {},
        "minutes_used": 0.0,
        "cost_usd": 0.0,
        "budget": dict(unit.get("budget") or {}),
        "routing": None,
        "depends_on": list(unit.get("depends_on", [])),
        "scope_files": list((unit.get("scope") or {}).get("files", [])),
        "process": None,
        "workspace": None,
        "handoff_ref": None,
        "last_reason": None,
        "annotations": {},
        "history": [],
    }


def _apply_event(doc, event):
    data = event.get("data") or {}
    if event.get("type") == "run.created":
        doc = copy.deepcopy(data["run"])
    elif doc is None:
        raise RunError("event log does not start with run.created")
    if "unit" in data and "unit_id" in data:
        doc["units"][data["unit_id"]] = copy.deepcopy(data["unit"])
    if "lease" in data:
        doc["lease"] = copy.deepcopy(data["lease"])
    if "handoff" in data:
        ids = {h["id"] for h in doc.setdefault("handoffs", [])}
        if data["handoff"]["id"] not in ids:
            doc["handoffs"].append(copy.deepcopy(data["handoff"]))
    doc["last_event_seq"] = int(event["seq"])
    return doc


def _action(kind, run_id, unit_id=None, detail=""):
    return {"kind": kind, "run_id": run_id, "unit_id": unit_id, "detail": detail}


def _liveness_status(result):
    """Normalize an is_alive result (bool or liveness status) to a status."""
    if result is True:
        return L.ALIVE
    if result is False or result is None:
        return L.DEAD
    return result if result in (L.ALIVE, L.DEAD, L.UNVERIFIED) else L.UNVERIFIED


class RunManager:
    def __init__(self, store, clock=time.time, id_gen=None, is_alive=None, emitter=None, lease_ttl=300.0,
                 probe=None):
        if not isinstance(store, RunStore):
            raise TypeError("store must be a RunStore")
        self.store = store
        self.clock = clock
        self.id_gen = id_gen or (lambda: uuid.uuid4().hex)
        # `probe` is the liveness platform strategy (see runs.liveness); `is_alive`
        # may override the check and return a bool or a liveness status string.
        self.probe = probe
        self.is_alive = is_alive or (lambda record: L.process_liveness(record, probe=self.probe))
        self.emitter = emitter
        self.lease_ttl = float(lease_ttl)

    # == loading ==
    def _load_unlocked(self, run_id):
        try:
            doc = self.store.load(run_id)
        except (StoreError, ValueError):
            doc = None
        base = int(doc.get("last_event_seq", 0)) if doc else 0
        for event in self.store.read_events(run_id, after_seq=base):
            doc = _apply_event(doc, event)
        if doc is None:
            raise RunError(f"run {run_id!r} not found")
        return doc

    def load(self, run_id):
        with self.store.lock(run_id):
            return self._load_unlocked(run_id)

    # == leases ==
    def _lease(self, conductor_id, ttl=None):
        now = self.clock()
        return {
            "conductor_id": conductor_id,
            "acquired_at": iso(now),
            "expires_at": now + (self.lease_ttl if ttl is None else float(ttl)),
        }

    def require_lease(self, run, conductor_id):
        lease = run.get("lease") or {}
        if lease.get("conductor_id") != conductor_id:
            raise LeaseError(f"conductor {conductor_id!r} does not hold the lease (holder {lease.get('conductor_id')!r})")
        if float(lease.get("expires_at", 0)) <= self.clock():
            raise LeaseError(f"lease of {conductor_id!r} expired")

    def _lease_blocker(self, run, conductor_id):
        lease = run.get("lease") or {}
        holder = lease.get("conductor_id")
        if holder and holder != conductor_id and float(lease.get("expires_at", 0)) > self.clock():
            return holder
        return None

    # == mutation core ==
    def _mutate(self, run_id, operation, conductor_id, fn, dry_run=False, require_lease=True):
        if not valid_identifier(run_id):
            return envelope(operation, ok=False, checks=[check("run_id_valid", False, repr(run_id))])
        with self.store.lock(run_id):
            try:
                run = self._load_unlocked(run_id)
            except RunError as exc:
                return envelope(operation, ok=False, checks=[check("run_exists", False, str(exc))])
            if require_lease:
                try:
                    self.require_lease(run, conductor_id)
                except LeaseError as exc:
                    return envelope(operation, ok=False, checks=[check("lease_held", False, str(exc))])
            work = copy.deepcopy(run)
            tx = _Tx()
            try:
                result = fn(work, tx) or {}
            except (S.IllegalTransition, RunError) as exc:
                return envelope(operation, ok=False, checks=[check(operation, False, str(exc))])
            ok = result.get("ok", True)
            common = dict(
                checks=result.get("checks") or [check(operation, ok)],
                warnings=result.get("warnings"),
                required_user_actions=result.get("required_user_actions"),
                data=result.get("data"),
            )
            if dry_run:
                return envelope(operation, ok=ok, changed=False, **common)
            for event_type, data in tx.events:
                event = self.store.append_event(run_id, event_type, data)
                work["last_event_seq"] = event["seq"]
            if tx.events:
                self.store.save(work)
            warnings = list(common.pop("warnings") or [])
            warnings.extend(self._emit_external(run_id, tx.events))
            return envelope(
                operation,
                ok=ok,
                changed=bool(tx.events),
                rollback_checkpoint={"run_id": run_id, "event_seq": run.get("last_event_seq", 0)},
                warnings=warnings,
                **common,
            )

    def _emit_external(self, run_id, events):
        if self.emitter is None:
            return []
        warnings = []
        for event_type, data in events:
            try:
                if event_type == "run.created":
                    run = data["run"]
                    self.emitter.emit("run.created", run_id, data={
                        "plan_sha256": run["plan_sha256"],
                        "unit_ids": sorted(run["units"]),
                        "layers": run["layers"],
                        "conductor_id": data.get("conductor_id"),
                    })
                elif event_type == "unit.state_changed":
                    payload = {k: data.get(k) for k in ("from", "to", "reason", "conductor_id")}
                    payload["owner"] = data["unit"].get("owner")
                    self.emitter.emit("unit.state_changed", run_id, unit_id=data["unit_id"], data=payload)
                    if data.get("to") in ATTENTION_STATES:
                        self.emitter.emit("attention.required", run_id, unit_id=data["unit_id"], data={
                            "state": data["to"], "reason": data.get("reason") or data["to"],
                        })
            except Exception as exc:  # emission must never corrupt durable state
                warnings.append(f"event emission failed for {event_type}: {exc}")
        return warnings

    # == unit helpers ==
    def _unit(self, run, unit_id):
        unit = run["units"].get(unit_id)
        if unit is None:
            raise RunError(f"unknown unit {unit_id!r}")
        return unit

    def _set_state(self, run, tx, unit_id, target, conductor_id, reason=None):
        unit = self._unit(run, unit_id)
        current = unit["state"]
        S.check_transition(current, target)
        if target == S.VERIFYING:
            handoff = (unit.get("annotations") or {}).get("handoff") or {}
            if not (handoff.get("valid") is True and handoff.get("attempt") == unit["attempts"]):
                raise RunError(f"unit {unit_id!r} has no valid handoff for attempt {unit['attempts']}; "
                               "record_handoff must pass before verifying")
        unit["state"] = target
        unit["last_reason"] = reason
        unit["history"].append({
            "from": current, "to": target, "at": iso(self.clock()),
            "conductor_id": conductor_id, "owner": unit.get("owner"), "reason": reason,
        })
        tx.add("unit.state_changed", {
            "unit_id": unit_id, "from": current, "to": target, "reason": reason,
            "conductor_id": conductor_id, "unit": copy.deepcopy(unit),
        })
        if target == S.MERGED:
            self._promote_ready(run, tx, conductor_id)

    def _touch(self, run, tx, unit_id, event_type="unit.updated"):
        tx.add(event_type, {"unit_id": unit_id, "unit": copy.deepcopy(self._unit(run, unit_id))})

    def _promote_ready(self, run, tx, conductor_id):
        for uid in sorted(run["units"]):
            unit = run["units"][uid]
            if unit["state"] != S.PENDING:
                continue
            if all(run["units"][d]["state"] == S.MERGED for d in unit["depends_on"]):
                self._set_state(run, tx, uid, S.READY, conductor_id, reason="dependencies_merged")

    @staticmethod
    def _budget_limits(unit):
        """Budget in schema terms. `attempts` is per tier (as the escalation
        controller uses it), `minutes` and `maximum_cost_usd` are per unit.
        Runs persisted before the rename may carry `max_attempts` (a total
        cap) and `max_minutes`; they are read as deprecated aliases only."""
        budget = unit.get("budget") or {}
        per_tier = budget.get("attempts")
        total = budget.get("max_attempts") if per_tier is None else None
        minutes = budget.get("minutes", budget.get("max_minutes"))
        cost = budget.get("maximum_cost_usd")
        return per_tier, total, minutes, cost

    def _budget_exhausted(self, unit, tier=None):
        per_tier, total, minutes, cost = self._budget_limits(unit)
        used = int((unit.get("attempts_by_tier") or {}).get(str(tier), 0)) if tier is not None else unit["attempts"]
        if per_tier is not None and used >= int(per_tier):
            return f"attempts {used}/{per_tier} at tier {tier}"
        if total is not None and unit["attempts"] >= int(total):
            return f"attempts {unit['attempts']}/{total}"
        if minutes is not None and float(unit.get("minutes_used", 0.0)) > float(minutes):
            return f"minutes {unit['minutes_used']}/{minutes}"
        if cost is not None and float(unit.get("cost_usd", 0.0)) >= float(cost):
            return f"cost {unit.get('cost_usd', 0.0)}/{cost} USD"
        return None

    # == public API ==
    def create_run(self, plan, conductor_id, run_id=None, dry_run=False, metadata=None, policy=None):
        """Validate and persist a plan. Every unit must pass
        `schemas.validate_work_unit`; `policy`, when given, must pass
        `schemas.validate_repository_policy` and is stored with the run so
        classification uses it on every assignment."""
        op = "run.create"
        analysis = analyze_plan(plan)
        if not analysis["ok"]:
            return envelope(op, ok=False, checks=[check("plan_valid", False, e["message"]) for e in analysis["errors"]],
                            data={"errors": analysis["errors"], "cycle": analysis["cycle"]})
        units = normalize_units(plan)
        invalid = {}
        for index, unit in enumerate(units):
            report = schemas.validate_work_unit(unit)
            if not report.ok:
                label = unit.get("id") if isinstance(unit, dict) and isinstance(unit.get("id"), str) else f"#{index}"
                invalid[label] = list(report.errors) + [v.get("kind", "violation") for v in report.violations]
        if invalid:
            detail = "; ".join(f"{uid}: {', '.join(errs)}" for uid, errs in sorted(invalid.items()))
            return envelope(op, ok=False, checks=[check("work_units_valid", False, detail)],
                            data={"invalid_units": invalid})
        if policy is not None:
            preport = schemas.validate_repository_policy(policy)
            if not preport.ok:
                return envelope(op, ok=False, checks=[check("repository_policy_valid", False,
                                                            "; ".join(preport.errors) or "policy violations")],
                                data={"errors": list(preport.errors)})
        unsafe = [u["id"] for u in units if not valid_identifier(u["id"]) or u["id"].startswith(".")]
        if unsafe:
            # Unit ids become directory and branch names; refuse anything path-hostile.
            return envelope(op, ok=False, checks=[check("unit_ids_path_safe", False, repr(unsafe))])
        run_id = run_id or self.id_gen()
        if not valid_identifier(run_id):
            return envelope(op, ok=False, checks=[check("run_id_valid", False, repr(run_id))])
        now = self.clock()
        doc = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "created_at": iso(now),
            "plan_sha256": plan_sha256(units),
            "plan": {"units": copy.deepcopy(units)},
            "layers": analysis["layers"],
            "order": analysis["order"],
            "lease": self._lease(conductor_id),
            "units": {u["id"]: _new_unit_record(u) for u in units},
            "handoffs": [],
            "metadata": copy.deepcopy(metadata or {}),
            "policy": copy.deepcopy(policy),
            "last_event_seq": 0,
        }
        roots = [uid for uid in analysis["order"] if not doc["units"][uid]["depends_on"]]
        summary = {"run_id": run_id, "plan_sha256": doc["plan_sha256"], "layers": doc["layers"], "ready": roots}
        if dry_run:
            return envelope(op, changed=False, checks=[check("plan_valid", True)], data=summary)
        with self.store.lock(run_id):
            try:
                self.store.create(doc)
            except StoreError as exc:
                return envelope(op, ok=False, checks=[check("run_id_unique", False, str(exc))])
            event = self.store.append_event(run_id, "run.created", {"run": copy.deepcopy(doc), "conductor_id": conductor_id})
            doc["last_event_seq"] = event["seq"]
            tx = _Tx()
            self._promote_ready(doc, tx, conductor_id)
            for event_type, data in tx.events:
                doc["last_event_seq"] = self.store.append_event(run_id, event_type, data)["seq"]
            self.store.save(doc)
        warnings = self._emit_external(run_id, [("run.created", {"run": doc, "conductor_id": conductor_id})] + tx.events)
        return envelope(op, changed=True, checks=[check("plan_valid", True), check("work_units_valid", True)],
                        warnings=warnings, data=summary, rollback_checkpoint={"run_id": run_id, "event_seq": 0})

    def acquire_lease(self, run_id, conductor_id, ttl=None, dry_run=False):
        def fn(run, tx):
            holder = self._lease_blocker(run, conductor_id)
            if holder:
                raise LeaseError(f"lease held by {holder!r}")
            previous = (run.get("lease") or {}).get("conductor_id")
            run["lease"] = self._lease(conductor_id, ttl)
            data = {"lease": copy.deepcopy(run["lease"]), "previous": previous}
            if previous and previous != conductor_id:
                data["handoff"] = self._conductor_handoff(run, previous, conductor_id, "lease_expired")
            tx.add("lease.acquired", data)
            return {"data": {"lease": run["lease"], "previous": previous}}
        return self._mutate(run_id, "lease.acquire", conductor_id, fn, dry_run, require_lease=False)

    def renew_lease(self, run_id, conductor_id, ttl=None):
        def fn(run, tx):
            run["lease"] = self._lease(conductor_id, ttl)
            tx.add("lease.renewed", {"lease": copy.deepcopy(run["lease"])})
            return {"data": {"lease": run["lease"]}}
        return self._mutate(run_id, "lease.renew", conductor_id, fn)

    def release_lease(self, run_id, conductor_id):
        def fn(run, tx):
            run["lease"]["expires_at"] = self.clock()
            tx.add("lease.released", {"lease": copy.deepcopy(run["lease"])})
        return self._mutate(run_id, "lease.release", conductor_id, fn)

    def _conductor_handoff(self, run, frm, to, reason):
        handoff = {
            "id": f"handoff-{self.id_gen()}", "kind": "conductor", "from": frm, "to": to,
            "reason": reason, "at": iso(self.clock()),
        }
        run.setdefault("handoffs", []).append(handoff)
        return copy.deepcopy(handoff)

    def transfer_lease(self, run_id, from_conductor, to_conductor, ttl=None, reason="explicit_transfer"):
        def fn(run, tx):
            run["lease"] = self._lease(to_conductor, ttl)
            handoff = self._conductor_handoff(run, from_conductor, to_conductor, reason)
            tx.add("lease.transferred", {"lease": copy.deepcopy(run["lease"]), "handoff": handoff})
            return {"data": {"lease": run["lease"], "handoff": handoff}}
        return self._mutate(run_id, "lease.transfer", from_conductor, fn)

    def transition(self, run_id, conductor_id, unit_id, target, reason=None, dry_run=False):
        def fn(run, tx):
            if target == S.ASSIGNED:
                raise RunError("use assign_unit or reassign_unit to assign work")
            self._set_state(run, tx, unit_id, target, conductor_id, reason)
            return {"data": {"unit_id": unit_id, "state": target}}
        return self._mutate(run_id, "unit.transition", conductor_id, fn, dry_run)

    def _routing_record(self, run, unit, unit_id, routing):
        """Classify the plan unit and merge the caller's runner selection.

        Returns (record, error). The record always carries the classify()
        decision, so every assignment has a routing decision on file even
        when no runner was selected (runner fields are then None)."""
        plan_unit = self.plan_unit(run, unit_id)
        classification = classify(plan_unit, run.get("policy"),
                                  previous_attempt_failed=unit["attempts"] > 0)
        if not classification.ok:
            return None, "; ".join(classification.errors) or "classification failed"
        routing = dict(routing or {})
        # Without an explicit tier a retry stays on the previous attempt's
        # tier: only the escalation controller moves a unit between tiers.
        previous = (unit.get("routing") or {}).get("tier")
        tier = routing.get("tier", previous if previous is not None else classification.chosen_tier)
        if not (isinstance(tier, int) and not isinstance(tier, bool)
                and classification.minimum_tier <= tier <= classification.maximum_tier):
            return None, (f"tier {tier!r} outside {classification.minimum_tier}.."
                          f"{classification.maximum_tier}")
        decision = classification.to_routing_decision(
            iso(self.clock()), runner=routing.get("runner"), provider=routing.get("provider"),
            model=routing.get("model"), estimated_cost_usd=routing.get("estimated_cost_usd"))
        report = schemas.validate_routing_decision(decision)
        if not report.ok:
            return None, "; ".join(report.errors)
        record = {
            "decision": decision, "tier": tier, "runner": routing.get("runner"),
            "provider": routing.get("provider"), "model": routing.get("model"),
            "family": routing.get("family"), "risk": classification.risk,
            "minimum_tier": classification.minimum_tier, "review_tier": classification.review_tier,
            "escalation": copy.deepcopy(routing.get("escalation")),
        }
        return record, None

    def _assign(self, run, tx, unit_id, worker_id, conductor_id, run_id, routing=None):
        unit = self._unit(run, unit_id)
        record, error = self._routing_record(run, unit, unit_id, routing)
        if error:
            self._set_state(run, tx, unit_id, S.NEEDS_USER, conductor_id, reason="routing_invalid")
            return {"ok": False, "checks": [check("routing_valid", False, error)],
                    "required_user_actions": [_action("resolve_needs_user", run_id, unit_id, error)]}
        exhausted = self._budget_exhausted(unit, record["tier"])
        if exhausted:
            self._set_state(run, tx, unit_id, S.NEEDS_USER, conductor_id, reason="budget_exhausted")
            return {"ok": False, "checks": [check("budget_available", False, exhausted)],
                    "required_user_actions": [_action("budget_exhausted", run_id, unit_id, exhausted)]}
        unit["attempts"] += 1
        by_tier = unit.setdefault("attempts_by_tier", {})
        by_tier[str(record["tier"])] = int(by_tier.get(str(record["tier"]), 0)) + 1
        unit["owner"] = worker_id
        unit["routing"] = record
        self._set_state(run, tx, unit_id, S.ASSIGNED, conductor_id, reason="assigned")
        return {"data": {"unit_id": unit_id, "owner": worker_id, "attempt": unit["attempts"],
                         "handoff_ref": unit.get("handoff_ref"), "routing": copy.deepcopy(record)}}

    def assign_unit(self, run_id, conductor_id, unit_id, worker_id, dry_run=False, guard=None, routing=None):
        """Assign a ready unit. `guard(run, unit_id)` runs under the run lock and
        may raise RunError to veto (the dispatcher uses it for overlap checks).
        `routing` carries the runner selection (`tier`, `runner`, `provider`,
        `model`, `family`, `estimated_cost_usd`); the classify() decision is
        always recorded on the unit as `routing`."""
        def fn(run, tx):
            unit = self._unit(run, unit_id)
            if unit["state"] != S.READY:
                raise S.IllegalTransition(f"unit {unit_id!r} is {unit['state']}, not ready")
            if unit.get("owner") not in (None, worker_id):
                raise RunError(f"unit {unit_id!r} belongs to {unit['owner']!r}; use reassign_unit to record a handoff")
            if guard is not None:
                guard(run, unit_id)
            return self._assign(run, tx, unit_id, worker_id, conductor_id, run_id, routing)
        return self._mutate(run_id, "unit.assign", conductor_id, fn, dry_run)

    def reassign_unit(self, run_id, conductor_id, unit_id, new_owner, reason, dry_run=False, routing=None):
        def fn(run, tx):
            unit = self._unit(run, unit_id)
            allowed = {S.INTERRUPTED, S.FAILED, S.NEEDS_USER, S.READY, S.BLOCKED}
            if unit["state"] not in allowed:
                raise RunError(f"unit {unit_id!r} is {unit['state']}; only stopped units can be reassigned")
            previous_state = unit["state"]
            handoff = {
                "id": f"handoff-{self.id_gen()}", "kind": "unit", "unit_id": unit_id,
                "from": unit.get("owner"), "to": new_owner, "reason": reason,
                "previous_state": previous_state, "process": copy.deepcopy(unit.get("process")),
                "workspace": copy.deepcopy(unit.get("workspace")), "at": iso(self.clock()),
                "conductor_id": conductor_id,
            }
            run.setdefault("handoffs", []).append(handoff)
            unit["handoff_ref"] = handoff["id"]
            unit["process"] = None
            if previous_state != S.READY:
                self._set_state(run, tx, unit_id, S.READY, conductor_id, reason=f"handoff:{handoff['id']}")
            unit["owner"] = new_owner
            tx.add("handoff.recorded", {"unit_id": unit_id, "unit": copy.deepcopy(unit), "handoff": copy.deepcopy(handoff)})
            result = self._assign(run, tx, unit_id, new_owner, conductor_id, run_id, routing)
            result.setdefault("data", {})["handoff"] = copy.deepcopy(handoff)
            return result
        return self._mutate(run_id, "unit.reassign", conductor_id, fn, dry_run)

    def record_process(self, run_id, conductor_id, unit_id, process):
        def fn(run, tx):
            unit = self._unit(run, unit_id)
            record = dict(process)
            if ("start_ticks" not in record and "start_token" not in record
                    and isinstance(record.get("pid"), int)):
                record.update(L.capture_identity(record["pid"], probe=self.probe))
            record.setdefault("recorded_at", iso(self.clock()))
            unit["process"] = record
            self._touch(run, tx, unit_id)
        return self._mutate(run_id, "unit.record_process", conductor_id, fn)

    def record_process_exit(self, run_id, conductor_id, unit_id, result):
        """Mark the unit's runner process finished, so a restart never treats
        a unit that is past its runner as a lost worker."""
        def fn(run, tx):
            unit = self._unit(run, unit_id)
            proc = dict(unit.get("process") or {})
            proc.update({"finished": True, "finished_at": iso(self.clock()),
                         "exit_code": (result or {}).get("exit_code"), "signal": (result or {}).get("signal"),
                         "timed_out": bool((result or {}).get("timed_out")),
                         "cancelled": bool((result or {}).get("cancelled"))})
            unit["process"] = proc
            self._touch(run, tx, unit_id)
        return self._mutate(run_id, "unit.record_process_exit", conductor_id, fn)

    def surrender_unit(self, run_id, conductor_id, unit_id, reason="lease_lost", detail=None):
        """Escape hatch for a conductor that lost the run lease while its
        runner was in flight. Allowed without the lease, but only for the
        conductor recorded on the unit's process, and only toward
        `needs_user`: the unit is never advanced, the process record (pid)
        is kept for the operator, and a `verify_worker_process` action is
        returned."""
        def fn(run, tx):
            unit = self._unit(run, unit_id)
            proc = unit.get("process") or {}
            if proc.get("conductor_id") != conductor_id:
                raise RunError(f"unit {unit_id!r} process was not started by {conductor_id!r}")
            if unit["state"] not in S.IN_FLIGHT:
                raise RunError(f"unit {unit_id!r} is {unit['state']}, not in flight")
            proc = dict(proc, surrendered_at=iso(self.clock()), surrender_detail=detail)
            unit["process"] = proc
            self._set_state(run, tx, unit_id, S.NEEDS_USER, conductor_id, reason=reason)
            text = (f"pid {proc.get('pid')} was started by {conductor_id}, which lost the run lease; "
                    f"confirm the process is stopped before reassigning")
            return {"required_user_actions": [_action("verify_worker_process", run_id, unit_id, text)],
                    "data": {"unit_id": unit_id, "state": S.NEEDS_USER, "pid": proc.get("pid")}}
        return self._mutate(run_id, "unit.surrender", conductor_id, fn, require_lease=False)

    def record_handoff(self, run_id, conductor_id, unit_id, handoff, head=None):
        """Validate a runner handoff with `schemas.validate_handoff(handoff,
        unit)` and record the outcome for the current attempt. The unit can
        enter `verifying` only after a valid handoff with status `succeeded`
        (and, when `head` is given, a commit sha naming that head)."""
        def fn(run, tx):
            unit = self._unit(run, unit_id)
            plan_unit = self.plan_unit(run, unit_id)
            report = schemas.validate_handoff(handoff, plan_unit)
            problems = list(report.errors) + [v.get("kind", "violation") for v in report.violations]
            doc = handoff if isinstance(handoff, dict) else {}
            status = doc.get("status")
            if report.ok and status != "succeeded":
                problems.append(f"status {status}")
            sha = (doc.get("commit") or {}).get("sha") if isinstance(doc.get("commit"), dict) else None
            if report.ok and head is not None and not (isinstance(sha, str) and len(sha) >= 7
                                                       and head.startswith(sha)):
                problems.append("commit.sha does not name the worktree head")
            files = doc.get("files_changed") if isinstance(doc.get("files_changed"), list) else []
            usage = doc.get("usage") if isinstance(doc.get("usage"), dict) else {}
            cost = usage.get("cost_usd")
            unit["annotations"]["handoff"] = {
                "attempt": unit["attempts"], "valid": not problems, "status": status if isinstance(status, str) else None,
                "problems": problems, "violations": [dict(v) for v in report.violations],
                "commit": sha if isinstance(sha, str) else None,
                "files_changed": [f for f in files if isinstance(f, str)][:200],
                "cost_usd": cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
                "user_input_required": doc.get("user_input_required") is True,
                "recorded_at": iso(self.clock()),
            }
            self._touch(run, tx, unit_id)
            return {"ok": not problems, "checks": [check("handoff_valid", not problems, "; ".join(problems))],
                    "data": {"handoff": copy.deepcopy(unit["annotations"]["handoff"])}}
        return self._mutate(run_id, "unit.record_handoff", conductor_id, fn)

    def record_workspace(self, run_id, conductor_id, unit_id, workspace):
        def fn(run, tx):
            self._unit(run, unit_id)["workspace"] = dict(workspace)
            self._touch(run, tx, unit_id)
        return self._mutate(run_id, "unit.record_workspace", conductor_id, fn)

    def annotate_unit(self, run_id, conductor_id, unit_id, key, value):
        def fn(run, tx):
            if key not in ANNOTATION_KEYS:
                raise RunError(f"annotation key {key!r} not allowed")
            self._unit(run, unit_id)["annotations"][key] = copy.deepcopy(value)
            self._touch(run, tx, unit_id)
        return self._mutate(run_id, "unit.annotate", conductor_id, fn)

    def record_usage(self, run_id, conductor_id, unit_id, minutes, cost_usd=0.0):
        def fn(run, tx):
            unit = self._unit(run, unit_id)
            if float(minutes) < 0 or float(cost_usd) < 0:
                raise RunError("usage must be non-negative")
            unit["minutes_used"] = float(unit["minutes_used"]) + float(minutes)
            unit["cost_usd"] = float(unit.get("cost_usd", 0.0)) + float(cost_usd)
            self._touch(run, tx, unit_id)
            _, _, limit, cost_limit = self._budget_limits(unit)
            detail = None
            if limit is not None and unit["minutes_used"] > float(limit):
                detail = f"minutes {unit['minutes_used']}/{limit}"
            elif cost_limit is not None and unit["cost_usd"] > float(cost_limit):
                detail = f"cost {unit['cost_usd']}/{cost_limit} USD"
            if detail:
                if S.NEEDS_USER in S.TRANSITIONS[unit["state"]]:
                    self._set_state(run, tx, unit_id, S.NEEDS_USER, conductor_id, reason="budget_exhausted")
                return {"ok": False, "checks": [check("budget_available", False, detail)],
                        "required_user_actions": [_action("budget_exhausted", run_id, unit_id, detail)]}
            return {"data": {"minutes_used": unit["minutes_used"], "cost_usd": unit["cost_usd"]}}
        return self._mutate(run_id, "unit.record_usage", conductor_id, fn)

    def resume(self, run_id, conductor_id, dry_run=False, ttl=None):
        """Rebuild state, take the lease, and mark dead in-flight units interrupted."""
        def fn(run, tx):
            holder = self._lease_blocker(run, conductor_id)
            if holder:
                raise LeaseError(f"lease held by {holder!r}; wait for expiry or an explicit transfer")
            previous = (run.get("lease") or {}).get("conductor_id")
            run["lease"] = self._lease(conductor_id, ttl)
            lease_data = {"lease": copy.deepcopy(run["lease"]), "previous": previous}
            if previous and previous != conductor_id:
                lease_data["handoff"] = self._conductor_handoff(run, previous, conductor_id, "resume")
            tx.add("lease.acquired", lease_data)
            interrupted, unverified = [], []
            for uid in sorted(run["units"]):
                unit = run["units"][uid]
                # Only units whose runner may still be working are examined;
                # verifying, reviewing and queued_for_merge are past it.
                if unit["state"] not in S.IN_FLIGHT:
                    continue
                proc = unit.get("process")
                if proc and proc.get("finished"):
                    status = L.DEAD  # runner exited; its result was never processed
                else:
                    status = _liveness_status(self.is_alive(proc)) if proc else L.DEAD
                if status == L.ALIVE:
                    continue
                if status == L.UNVERIFIED:
                    # The worker may still be running: never hand the unit to
                    # someone else automatically; ask the operator instead.
                    self._set_state(run, tx, uid, S.NEEDS_USER, conductor_id, reason="liveness_unverified")
                    unverified.append(uid)
                    continue
                self._set_state(run, tx, uid, S.INTERRUPTED, conductor_id, reason="process_gone")
                interrupted.append(uid)
            tx.add("run.resumed", {"conductor_id": conductor_id, "previous": previous,
                                   "interrupted": interrupted, "unverified": unverified})
            actions = [_action("reassign_interrupted_unit", run_id, uid, f"previous owner {run['units'][uid]['owner']}")
                       for uid in interrupted]
            for uid in unverified:
                proc = run["units"][uid]["process"]
                actions.append(_action("verify_worker_process", run_id, uid,
                                       f"pid {proc.get('pid')} may still be running (identity unverified, "
                                       f"method {proc.get('start_method') or 'none'}); stop it or confirm it is "
                                       f"gone before reassigning"))
            return {"required_user_actions": actions, "data": {
                "interrupted": interrupted, "unverified": unverified, "previous_conductor": previous,
                "states": {uid: u["state"] for uid, u in run["units"].items()},
            }}
        return self._mutate(run_id, "run.resume", conductor_id, fn, dry_run, require_lease=False)

    # == queries ==
    @staticmethod
    def units_in(run, states):
        states = {states} if isinstance(states, str) else set(states)
        return sorted(uid for uid, u in run["units"].items() if u["state"] in states)

    def plan_unit(self, run, unit_id):
        for unit in run["plan"]["units"]:
            if unit["id"] == unit_id:
                return unit
        raise RunError(f"unknown unit {unit_id!r}")
