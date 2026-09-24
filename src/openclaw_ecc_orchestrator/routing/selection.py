"""Cost-aware runner and reviewer selection.

``select_runner`` picks the cheapest eligible certified runner for a tier.
Eligibility, in order: not retired, known profile, certified, provider
allowed by policy, not excluded, breaker not open, role permitted, class
served, high-risk permission, conditional requirement (Kimi needs
``large_context``), and a model resolved from the live catalog (API
runners) or policy ``cli_models`` aliases (CLI runners). OpenRouter models
must also be in ``openrouter.approved_models``.

Cost: catalog pricing times ``policy.cost_estimation`` token counts, else
``policy.runner_costs[runner][class]`` (USD per attempt), else unknown.
Unknown sorts after every known cost. Ties break by runner name, then model.
Fallback-only runners (OpenRouter for authoring) are used only when no
primary candidate exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ..runners import discovery
from ..runners.registry import PROFILES, RunnerProfile, model_family
from ..schemas import TIERS, is_retired, normalize_provider

TIER_CLASS = {0: "economical", 1: "standard", 2: "advanced"}
DEFAULT_ESTIMATION = {"input_tokens": 20000, "output_tokens": 4000}
ROLES = ("author", "review", "coordination")


@dataclass
class Selection:
    ok: bool
    tier: int | None
    role: str
    runner: str | None = None
    provider: str | None = None
    family: str | None = None
    model: str | None = None
    capability_class: str | None = None
    estimated_cost_usd: float | None = None
    cost_source: str | None = None
    independent_family: bool | None = None
    blocking: bool = False
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _certified_names(certified_runners: Any) -> list[str]:
    if isinstance(certified_runners, Mapping):
        names = []
        for name, rec in certified_runners.items():
            if isinstance(rec, Mapping) and rec.get("status") not in (None, "certified"):
                continue
            names.append(name)
        return names
    names = []
    for item in certified_runners or []:
        if isinstance(item, Mapping):
            if item.get("status") == "certified" and isinstance(item.get("runner"), str):
                names.append(item["runner"])
        elif isinstance(item, str):
            names.append(item)
    return names


def resolve_model(profile: RunnerProfile, capability_class: str, catalog: Mapping | None,
                  policy: Mapping, now: float | None = None) -> tuple[str | None, str]:
    """Model id or alias for ``profile`` at ``capability_class``, or (None, reason)."""
    retired_ids = list(policy.get("retired_model_ids") or [])
    if profile.model_source == "cli":
        alias = ((policy.get("cli_models") or {}).get(profile.name) or {}).get(capability_class) \
            or profile.default_cli_models.get(capability_class)
        if not alias:
            return None, "no cli model alias configured for %s" % capability_class
        if alias in retired_ids:
            return None, "cli model alias is retired"
        return alias, ""
    if not catalog:
        return None, "no catalog snapshot"
    max_age = policy.get("catalog_max_age_seconds")
    if now is not None and max_age is not None and discovery.is_stale(catalog, now, max_age):
        return None, "catalog stale"
    prefs = (policy.get("model_preferences") or {}).get(profile.provider) or {}
    patterns = list(prefs.get(capability_class) or [])
    live = discovery.live_models(catalog, profile.provider, retired_ids)
    if profile.name == "openrouter":
        approved = list(((policy.get("openrouter") or {}).get("approved_models")) or [])
        ranked = [m for m in discovery.rank_models(live, patterns) if m in approved]
    else:
        ranked = discovery.rank_models(live, patterns)
    if not ranked:
        return None, "no live catalog model matches %s preferences" % capability_class
    return ranked[0], ""


def estimate_cost(profile: RunnerProfile, model: str, capability_class: str,
                  catalog: Mapping | None, policy: Mapping) -> tuple[float | None, str]:
    if profile.model_source == "catalog" and catalog:
        price = discovery.model_pricing(catalog, profile.provider, model)
        if price and price["input_usd_per_mtok"] is not None \
                and price["output_usd_per_mtok"] is not None:
            est = {**DEFAULT_ESTIMATION, **(policy.get("cost_estimation") or {})}
            cost = (est["input_tokens"] * price["input_usd_per_mtok"]
                    + est["output_tokens"] * price["output_usd_per_mtok"]) / 1_000_000
            return round(cost, 8), "catalog"
    configured = ((policy.get("runner_costs") or {}).get(profile.name) or {}).get(capability_class)
    if isinstance(configured, (int, float)) and not isinstance(configured, bool):
        return float(configured), "config"
    return None, "unknown"


def select_runner(tier: int, certified_runners: Any, catalog: Mapping | None,
                  policy: Mapping | None, exclude_providers: Iterable[str] = (), *,
                  role: str = "author", risk: str = "low", breaker=None,
                  now: float | None = None, large_context: bool = False,
                  exclude_runners: Iterable[str] = (),
                  exclude_families: Iterable[str] = ()) -> Selection:
    """Cheapest eligible certified runner for ``tier`` and ``role``."""
    result = Selection(ok=False, tier=None, role=role)
    if not (isinstance(tier, int) and not isinstance(tier, bool) and tier in TIERS):
        result.reasons.append("invalid tier %r" % (tier,))
        return result
    if role not in ROLES:
        result.reasons.append("invalid role %r" % (role,))
        return result
    result.tier = tier
    policy = policy or {}
    capability_class = TIER_CLASS[tier]
    allowed = {normalize_provider(p) for p in policy.get("allowed_providers") or PROFILES}
    excluded_providers = {normalize_provider(p) for p in exclude_providers}
    excluded_runners = {normalize_provider(r) for r in exclude_runners}
    excluded_families = set(exclude_families)

    primary: list[dict] = []
    fallback: list[dict] = []
    for raw in _certified_names(certified_runners):
        name = normalize_provider(raw)

        def reject(reason: str) -> None:
            result.rejected.append({"runner": name, "reason": reason})

        if is_retired(name):
            reject("retired runner")
            continue
        profile = PROFILES.get(name)
        if profile is None:
            reject("unknown runner")
            continue
        if profile.provider not in allowed:
            reject("provider not allowed by policy")
            continue
        if profile.provider in excluded_providers or name in excluded_runners:
            reject("excluded")
            continue
        if breaker is not None and breaker.is_open(profile.provider):
            reject("circuit breaker open")
            continue
        is_fallback = False
        if role == "author":
            if "author" in profile.roles:
                classes = profile.classes
            elif "fallback" in profile.roles:
                classes, is_fallback = profile.classes, True
            else:
                reject("role author not permitted")
                continue
        elif role == "review":
            if "review" not in profile.roles:
                reject("role review not permitted")
                continue
            classes = profile.review_classes
        else:
            if "coordination" not in profile.roles:
                reject("role coordination not permitted")
                continue
            classes = tuple(profile.default_cli_models)
        if capability_class not in classes:
            reject("does not serve %s for %s" % (capability_class, role))
            continue
        if risk == "high" and not profile.high_risk_ok:
            reject("not permitted for high-risk work")
            continue
        if profile.conditional == "large_context" and not large_context:
            reject("conditional runner: requires large_context")
            continue
        model, why = resolve_model(profile, capability_class, catalog, policy, now)
        if model is None:
            reject(why)
            continue
        family = model_family(name, model)
        if family in excluded_families:
            reject("same model family as author")
            continue
        cost, source = estimate_cost(profile, model, capability_class, catalog, policy)
        entry = {"runner": name, "provider": profile.provider, "family": family,
                 "model": model, "estimated_cost_usd": cost, "cost_source": source,
                 "fallback": is_fallback}
        (fallback if is_fallback else primary).append(entry)

    def key(c: dict):
        cost = c["estimated_cost_usd"]
        return (cost is None, cost if cost is not None else 0.0, c["runner"], c["model"])

    primary.sort(key=key)
    fallback.sort(key=key)
    result.candidates = primary + fallback
    pool = primary or fallback
    if not pool:
        result.reasons.append("no eligible certified runner for tier %d (%s) as %s"
                              % (tier, capability_class, role))
        return result
    best = pool[0]
    result.ok = True
    result.runner, result.provider, result.family = best["runner"], best["provider"], best["family"]
    result.model, result.capability_class = best["model"], capability_class
    result.estimated_cost_usd, result.cost_source = best["estimated_cost_usd"], best["cost_source"]
    if best["fallback"]:
        result.role = "fallback"
        result.reasons.append("no primary candidate; using fallback runner %s" % best["runner"])
    result.reasons.append("cheapest eligible candidate for tier %d: %s (%s)"
                          % (tier, best["runner"], best["model"]))
    return result


def select_reviewer(author: Selection | Mapping, classification, certified_runners: Any,
                    catalog: Mapping | None, policy: Mapping | None, *, breaker=None,
                    now: float | None = None) -> Selection:
    """Independent reviewer for ``author``'s work on a classified unit.

    Review tier: ``classification.review_tier`` (2 for security, migration,
    auth, architecture or high risk; 0 for small deterministic diffs; else
    1), raised to 2 when the author worked at tier 2. The author runner is
    always excluded when independence is required. A different model family
    is preferred; for tier 2 reviews it is mandatory. When no qualified
    reviewer exists for tier 2 or high-risk work, ``blocking`` is True and
    the unit cannot complete.
    """
    result = Selection(ok=False, tier=None, role="review")
    if classification is None or not getattr(classification, "ok", False):
        result.reasons.append("classification is invalid; no review can be routed")
        result.blocking = True
        return result
    author_runner, author_family = _author_identity(author)
    review_tier, strict = required_review(author, classification)
    exclude_runners = [author_runner] if (classification.requires_independent_review or strict) \
        else []
    common = dict(role="review", risk=classification.risk, breaker=breaker, now=now,
                  exclude_runners=exclude_runners)

    chosen = select_runner(review_tier, certified_runners, catalog, policy,
                           exclude_families=[author_family] if author_family else [], **common)
    if chosen.ok:
        chosen.independent_family = True
    elif not strict:
        chosen = select_runner(review_tier, certified_runners, catalog, policy, **common)
        if chosen.ok:
            chosen.independent_family = False
            chosen.warnings.append("reviewer shares the author's model family %s" % author_family)
    if not chosen.ok:
        chosen.blocking = strict
        chosen.reasons.append("no qualified independent reviewer at tier %d%s" % (
            review_tier, "; high-risk work cannot complete" if strict else ""))
    else:
        chosen.reasons.append("review tier %d for author %s (%s)"
                              % (review_tier, author_runner, author_family))
    return chosen


def _getter(obj: Any):
    if isinstance(obj, Mapping):
        return lambda k: obj.get(k)
    return lambda k: getattr(obj, k, None)


def _family_of(runner: str | None, model: str | None) -> str | None:
    if not runner or runner not in PROFILES:
        return None
    try:
        return model_family(runner, model)
    except (KeyError, ValueError):
        return None


def _author_identity(author: Any) -> tuple[str, str | None]:
    get = _getter(author or {})
    runner = normalize_provider(get("runner")) if get("runner") else ""
    family = get("family") or _family_of(runner, get("model"))
    return runner, family


def required_review(author: Any, classification) -> tuple[int, bool]:
    """(review tier, strict) for ``author``'s work on ``classification``.

    The tier is ``classification.review_tier`` raised to 2 when the author
    worked at tier 2. Strict (tier 2 or high risk) reviews must come from a
    different model family and a different runner.
    """
    get = _getter(author or {})
    tier = classification.review_tier if isinstance(classification.review_tier, int) else 2
    author_tier = get("tier")
    if isinstance(author_tier, int) and not isinstance(author_tier, bool) and author_tier == 2:
        tier = 2
    return tier, (tier == 2 or classification.risk == "high")


def review_satisfies(review: Mapping | None, author: Any, classification, certified_runners: Any = None,
                     catalog: Mapping | None = None, policy: Mapping | None = None, *, breaker=None,
                     now: float | None = None) -> tuple[bool, list[str]]:
    """Does a recorded ``review`` meet what :func:`select_reviewer` requires?

    ``review`` must name the ``runner`` (and ``model``) that produced it and the
    ``tier`` it ran at. Checks: the classification is valid; the review tier is
    at least the required tier (:func:`required_review`, the same rule
    :func:`select_reviewer` uses); the reviewer runner differs from the author
    runner when independence is required; for strict reviews the author's
    model family is known and differs from the reviewer's. When
    ``certified_runners`` is given, the reviewer runner must also be eligible
    for review at the review's tier under the same rules ``select_reviewer``
    applies (certified, allowed, serving the tier, permitted for the risk,
    independent).
    Returns ``(ok, reasons)``; reasons explain every failure.
    """
    reasons: list[str] = []
    if classification is None or not getattr(classification, "ok", False):
        return False, ["classification is invalid; no review can satisfy it"]
    if not isinstance(review, Mapping):
        return False, ["no review record"]
    tier, strict = required_review(author, classification)
    author_runner, author_family = _author_identity(author)
    runner = normalize_provider(review.get("runner")) if review.get("runner") else ""
    rtier = review.get("tier")
    if not runner:
        reasons.append("review does not name the runner that produced it")
    if not (isinstance(rtier, int) and not isinstance(rtier, bool)) or rtier < tier:
        reasons.append("review tier %r is below the required tier %d" % (rtier, tier))
    if runner and author_runner and runner == author_runner and (
            strict or classification.requires_independent_review):
        reasons.append("reviewer runner %s is the author runner" % runner)
    if strict:
        family = _family_of(runner, review.get("model"))
        if not author_family:
            reasons.append("author model family is unknown; tier %d review independence cannot be shown"
                           % tier)
        elif not family:
            reasons.append("reviewer model family is unknown")
        elif family == author_family:
            reasons.append("reviewer shares the author's model family %s" % family)
    if certified_runners is not None and not reasons:
        # The reviewer must be one select_reviewer could have picked at the
        # tier the review ran at (at least the required tier): certified,
        # allowed, serving that tier for review, permitted for the risk, not
        # the author runner, and for strict reviews outside the author family.
        independent = strict or classification.requires_independent_review
        eligible = select_runner(rtier, certified_runners, catalog, policy, role="review",
                                 risk=classification.risk, breaker=breaker, now=now,
                                 exclude_runners=[author_runner] if independent and author_runner else [],
                                 exclude_families=[author_family] if strict and author_family else [])
        if runner not in {c["runner"] for c in eligible.candidates}:
            why = next((r["reason"] for r in eligible.rejected if r["runner"] == runner), "not certified")
            reasons.append("reviewer runner %s is not an eligible reviewer at tier %d: %s" % (runner, rtier, why))
    return not reasons, reasons
