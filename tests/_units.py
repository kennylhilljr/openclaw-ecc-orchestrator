"""Shared builders for schema valid work units and policies used by tests."""

import copy


def work_unit(uid, deps=(), files=None, commands=None, risk="low", budget=None, routing=None, **extra):
    """A work unit that passes `schemas.validate_work_unit`.

    `budget` and `routing` are merged over sensible defaults, so a test only
    names the fields it cares about.
    """
    doc = {
        "schema_version": "1.0",
        "id": uid,
        "title": f"unit {uid}",
        "depends_on": list(deps),
        "scope": {"files": list(files if files is not None else [f"src/{uid}.py"])},
        "acceptance": {"commands": list(commands if commands is not None else ["python3 -c pass"])},
        "risk": risk,
        "capabilities": {"network": False, "secrets": []},
        "routing": {"initial_tier": 0, "maximum_tier": 2, "reviewer_must_differ_from_author": True},
        "budget": {"attempts": 5, "minutes": 600, "maximum_cost_usd": 100.0},
        "rollback": "revert the unit branch",
    }
    doc["routing"].update(routing or {})
    doc["budget"].update(budget or {})
    doc.update(copy.deepcopy(extra))
    return doc


POLICY = {
    "schema_version": "1.0",
    "high_risk_paths": ["**/security/**", "migrations/**"],
    "required_checks": ["unit-tests"],
    "protected_commands": ["git push*"],
    "allowed_providers": ["claude", "codex", "gemini", "groq", "kimi"],
    "budget": {"max_cost_usd_per_unit": 50, "max_cost_usd_total": 500, "max_minutes_per_unit": 600},
    "cli_models": {"codex": {"economical": "codex-econ", "standard": "codex-std", "advanced": "codex-adv"},
                   "claude": {"economical": "haiku", "standard": "sonnet", "advanced": "opus"}},
    "runner_costs": {"codex": {"economical": 0.05, "standard": 0.4, "advanced": 1.5},
                     "claude": {"economical": 0.02, "standard": 0.5, "advanced": 2.0}},
}


def policy(**overrides):
    doc = copy.deepcopy(POLICY)
    doc.update(overrides)
    return doc
