"""`.orchestration/team.json` (TEAM-DELIVERY-v2 section 8) with defaults."""

import copy

CLASS_TIER = {"economical": 0, "standard": 1, "advanced": 2}
ROLES = ("pr_reviewer", "security_reviewer", "verifier", "architect", "designer", "delivery_lead",
         "product_owner")

DEFAULTS = {
    "schema_version": "2.0",
    "merge_approval": "human",
    "handoff_repair_turns": 1,
    "preflight": "required",
    "share_cap": 0.4,
    # Largest evidence pack (estimated tokens) each provider takes. Groq's free
    # tier allows 8K tokens per minute, so it only takes small packs.
    "max_pack_tokens": {"groq": 6000, "gemini": 400000, "kimi": 200000, "claude": 150000, "codex": 150000},
    "roles": {
        "pr_reviewer": {"class": "unit_review_tier", "panels": {
            "economical": ["gemini", "groq", "kimi", "codex", "claude"],
            "standard": ["kimi", "gemini", "codex", "claude"],
            "advanced": ["kimi", "codex", "claude"]}, "cost_cap_usd_per_turn": 0.50},
        "security_reviewer": {"class": "advanced", "panel": ["codex", "kimi", "claude"],
                              "when": "high_risk_paths_or_security_signals",
                              "distinct_from": ["author", "pr_reviewer"], "cost_cap_usd_per_turn": 1.00},
        "verifier": {"class": "standard", "tools": True, "panel": ["codex", "claude"], "timeout_minutes": 30},
        "architect": {"class": "advanced", "panel": ["kimi", "codex", "gemini", "claude"],
                      "when": ["milestone_start", "contract_units", "milestone_end"], "contract_paths": [],
                      "cost_cap_usd_per_turn": 1.50},
        "designer": {"class": "standard", "images": True, "panel": ["gemini", "codex", "claude"],
                     "when_paths": [], "cost_cap_usd_per_turn": 0.50},
        "delivery_lead": {"class": "standard", "panel": ["gemini", "groq", "kimi", "codex", "claude"],
                          "when": ["budget_stop", "milestone_end"], "cost_cap_usd_per_turn": 0.25},
        "product_owner": {"class": "advanced", "pair": True, "panel": ["kimi", "codex", "claude"],
                          "at": "milestone_end", "cost_cap_usd_per_turn": 2.00},
    },
}


class TeamConfigError(ValueError):
    pass


def load(doc):
    """Merge `doc` (parsed team.json, or {} / None) over the defaults and
    validate the parts the orchestrator relies on."""
    cfg = copy.deepcopy(DEFAULTS)
    doc = doc or {}
    if not isinstance(doc, dict):
        raise TeamConfigError("team.json must be an object")
    for key, value in doc.items():
        if key == "roles":
            if not isinstance(value, dict):
                raise TeamConfigError("roles must be an object")
            for role, spec in value.items():
                if role not in ROLES:
                    raise TeamConfigError(f"unknown role {role!r}")
                if not isinstance(spec, dict):
                    raise TeamConfigError(f"role {role} must be an object")
                cfg["roles"][role].update(copy.deepcopy(spec))
        elif key == "max_pack_tokens":
            cfg["max_pack_tokens"].update(value or {})
        else:
            cfg[key] = copy.deepcopy(value)
    if cfg["merge_approval"] not in ("agents", "human"):
        raise TeamConfigError("merge_approval must be agents or human")
    if not (0 < float(cfg["share_cap"]) <= 1):
        raise TeamConfigError("share_cap must be in (0, 1]")
    for role, spec in cfg["roles"].items():
        cls = spec.get("class")
        if cls != "unit_review_tier" and cls not in CLASS_TIER:
            raise TeamConfigError(f"role {role}: unknown class {cls!r}")
        panels = spec.get("panels") or {"*": spec.get("panel")}
        for name, panel in panels.items():
            if not isinstance(panel, list) or not panel or not all(isinstance(p, str) for p in panel):
                raise TeamConfigError(f"role {role}: panel {name} must be a non-empty list of providers")
    return cfg


def panel_for(cfg, role, capability_class):
    spec = cfg["roles"][role]
    if spec.get("panels"):
        return list(spec["panels"].get(capability_class) or [])
    return list(spec.get("panel") or [])


def role_class(cfg, role, review_tier=None):
    cls = cfg["roles"][role]["class"]
    if cls == "unit_review_tier":
        return {0: "economical", 1: "standard"}.get(review_tier, "advanced")
    return cls
