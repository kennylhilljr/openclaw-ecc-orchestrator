"""Dispatcher: assigns ready units, never two overlapping units in parallel."""

from ..runs import state as S
from ..runs.envelope import check, envelope
from ..runs.manager import RunError
from .conflicts import scope_overlaps


class ConflictError(RunError):
    pass


def _files(unit):
    """Declared scope; empty means the whole repository (see scope_overlaps)."""
    return list((unit.get("scope") or {}).get("files") or [])


def conflicts_with(unit, others):
    hits = []
    for other in others:
        if other["id"] == unit["id"]:
            continue
        overlaps = scope_overlaps(_files(unit), _files(other))
        if overlaps:
            hits.append({"unit_id": other["id"], "overlaps": overlaps})
    return hits


def plan_dispatch(candidates, active, limit=None):
    """Greedy, order-preserving selection of non-overlapping candidates."""
    selected = list(active)
    dispatch, deferred = [], []
    for unit in candidates:
        if limit is not None and len(dispatch) >= limit:
            deferred.append({"unit_id": unit["id"], "conflicts_with": [], "reason": "no_worker"})
            continue
        hits = conflicts_with(unit, selected)
        if hits:
            deferred.append({"unit_id": unit["id"], "conflicts_with": [h["unit_id"] for h in hits],
                             "overlaps": [o for h in hits for o in h["overlaps"]], "reason": "scope_overlap"})
            continue
        dispatch.append(unit["id"])
        selected.append(unit)
    return {"dispatch": dispatch, "deferred": deferred}


def assert_parallel_safe(unit, active_units):
    hits = conflicts_with(unit, active_units)
    if hits:
        raise ConflictError(f"unit {unit['id']!r} overlaps active unit(s) {[h['unit_id'] for h in hits]}")


class Dispatcher:
    def __init__(self, manager):
        self.manager = manager

    def _guard(self, run, unit_id):
        """Re-checked under the run lock at assignment time."""
        plan_units, active, _ = self._split(run)
        assert_parallel_safe(plan_units[unit_id], active)

    def _split(self, run):
        plan_units = {u["id"]: u for u in run["plan"]["units"]}
        order = run.get("order") or sorted(plan_units)
        active = [plan_units[u] for u in order if run["units"][u]["state"] in S.ACTIVE]
        ready = [plan_units[u] for u in order if run["units"][u]["state"] == S.READY]
        return plan_units, active, ready

    def dispatch(self, run_id, conductor_id, workers, dry_run=False):
        op = "dispatch"
        run = self.manager.load(run_id)
        _, active, ready = self._split(run)
        plan = plan_dispatch(ready, active, limit=len(workers))
        assignments = [{"unit_id": uid, "worker_id": w} for uid, w in zip(plan["dispatch"], workers)]
        if dry_run:
            return envelope(op, changed=False, data={"assigned": assignments, "deferred": plan["deferred"]})
        done, checks, actions = [], [], []
        for item in assignments:
            res = self.manager.assign_unit(run_id, conductor_id, item["unit_id"], item["worker_id"],
                                           guard=self._guard)
            checks.extend(res["checks"])
            actions.extend(res["required_user_actions"])
            if res["ok"]:
                done.append(item)
        return envelope(op, ok=all(c["ok"] for c in checks), changed=bool(done), checks=checks,
                        required_user_actions=actions, data={"assigned": done, "deferred": plan["deferred"]})

    def dispatch_unit(self, run_id, conductor_id, unit_id, worker_id, dry_run=False, routing=None):
        """Assign one named unit; refused when it overlaps any active unit.
        `routing` (runner selection) is recorded with the assignment."""
        op = "dispatch.unit"
        run = self.manager.load(run_id)
        plan_units, active, _ = self._split(run)
        if unit_id not in plan_units:
            return envelope(op, ok=False, checks=[check("unit_exists", False, unit_id)])
        try:
            assert_parallel_safe(plan_units[unit_id], active)
        except ConflictError as exc:
            return envelope(op, ok=False, checks=[check("no_parallel_overlap", False, str(exc))])
        res = self.manager.assign_unit(run_id, conductor_id, unit_id, worker_id, dry_run=dry_run, guard=self._guard,
                                       routing=routing)
        res["operation"] = op
        res["checks"] = [check("no_parallel_overlap", True)] + res["checks"]
        return res
