"""Plan DAG validation: unique ids, known dependencies, cycles, layers."""

import hashlib
import json


class PlanError(ValueError):
    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(e["message"] for e in self.errors) or "invalid plan")


def normalize_units(plan):
    """Accept either a list of units or a plan dict with a `units` key."""
    if isinstance(plan, dict):
        plan = plan.get("units", [])
    if not isinstance(plan, list):
        raise PlanError([{"code": "invalid_plan", "message": "plan must be a list of units"}])
    return plan


def plan_sha256(plan):
    """Stable sha256 over the canonical JSON form of the plan's units."""
    units = normalize_units(plan)
    canonical = json.dumps(units, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _find_cycle(ids, deps):
    """Return one cycle as [a, b, ..., a] or None. Deterministic order."""
    WHITE, GREY, BLACK = 0, 1, 2
    color = {i: WHITE for i in ids}
    stack = []

    def visit(node):
        color[node] = GREY
        stack.append(node)
        for dep in sorted(deps[node]):
            if dep not in color:
                continue
            if color[dep] == GREY:
                start = stack.index(dep)
                return stack[start:] + [dep]
            if color[dep] == WHITE:
                found = visit(dep)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for node in sorted(ids):
        if color[node] == WHITE:
            found = visit(node)
            if found:
                return found
    return None


def analyze_plan(plan):
    """Validate the plan structure and compute topological layers.

    Returns {"ok", "errors", "cycle", "layers", "order"}; never raises for
    content problems so callers can report every error at once.
    """
    errors = []
    try:
        units = normalize_units(plan)
    except PlanError as exc:
        return {"ok": False, "errors": exc.errors, "cycle": None, "layers": [], "order": []}

    ids = []
    deps = {}
    seen = set()
    for index, unit in enumerate(units):
        if not isinstance(unit, dict):
            errors.append({"code": "invalid_unit", "index": index, "message": f"unit #{index} is not an object"})
            continue
        uid = unit.get("id")
        if not isinstance(uid, str) or not uid:
            errors.append({"code": "invalid_id", "index": index, "message": f"unit #{index} has no string id"})
            continue
        if uid in seen:
            errors.append({"code": "duplicate_id", "unit_id": uid, "message": f"duplicate unit id {uid!r}"})
            continue
        seen.add(uid)
        ids.append(uid)
        raw = unit.get("depends_on", [])
        if not isinstance(raw, list) or not all(isinstance(d, str) for d in raw):
            errors.append({"code": "invalid_depends_on", "unit_id": uid, "message": f"unit {uid!r} depends_on must be a list of ids"})
            raw = []
        deps[uid] = list(dict.fromkeys(raw))

    for uid in ids:
        for dep in deps[uid]:
            if dep not in deps:
                errors.append({
                    "code": "missing_dependency",
                    "unit_id": uid,
                    "dependency": dep,
                    "message": f"unit {uid!r} depends on unknown unit {dep!r}",
                })

    cycle = _find_cycle(ids, deps)
    if cycle:
        errors.append({"code": "cycle", "cycle": cycle, "message": "dependency cycle: " + " -> ".join(cycle)})

    layers = []
    order = []
    if not errors:
        remaining = {uid: {d for d in deps[uid]} for uid in ids}
        done = set()
        while remaining:
            layer = sorted(uid for uid, ds in remaining.items() if ds <= done)
            if not layer:  # defensive, cycles are reported above
                break
            layers.append(layer)
            order.extend(layer)
            done.update(layer)
            for uid in layer:
                del remaining[uid]

    return {"ok": not errors, "errors": errors, "cycle": cycle, "layers": layers, "order": order}


def validate_plan(plan):
    """Raise PlanError when invalid, otherwise return the analysis."""
    result = analyze_plan(plan)
    if not result["ok"]:
        raise PlanError(result["errors"])
    return result
