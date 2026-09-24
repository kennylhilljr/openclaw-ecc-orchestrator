"""Runner registry: the only place runner names become routable profiles.

Retired runners (windsurf, pi, direct OpenAI API coding) have no profile and
are rejected with :class:`RetiredRunnerError` wherever a name is resolved,
so they cannot reach an active route through configuration or a forged
certification record. Uncertified runners are excluded, never shown as
degraded but available.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from ..schemas import (RETIRED_PROVIDERS, is_retired, normalize_provider,
                       validate_certification_record)


class RetiredRunnerError(ValueError):
    """A retired runner or provider was requested."""


@dataclass(frozen=True)
class RunnerProfile:
    name: str
    provider: str
    family: str | None          # None: derive from the model id (hosting providers)
    kind: str                   # "coding_cli" | "api"
    classes: tuple[str, ...]    # capability classes this runner may author at
    roles: frozenset = field(default_factory=frozenset)
    review_classes: tuple[str, ...] = ()
    high_risk_ok: bool = True   # may author or review high-risk work
    model_source: str = "catalog"   # "catalog" (live discovery) | "cli" (policy aliases)
    conditional: str | None = None  # e.g. "large_context"
    default_cli_models: Mapping[str, str] = field(default_factory=dict)
    notes: str = ""


PROFILES: dict[str, RunnerProfile] = {
    "claude": RunnerProfile(
        name="claude", provider="claude", family="anthropic", kind="coding_cli",
        classes=("standard", "advanced"), review_classes=("standard", "advanced"),
        roles=frozenset({"author", "review", "coordination"}), model_source="cli",
        default_cli_models={"economical": "haiku", "standard": "sonnet", "advanced": "opus"},
        notes="haiku is for cheap coordination only; production enablement requires the "
              "CLI hang to be resolved, which the cancellation_timeout check evidences."),
    "codex": RunnerProfile(
        name="codex", provider="codex", family="openai", kind="coding_cli",
        classes=("economical", "standard", "advanced"),
        review_classes=("economical", "standard", "advanced"),
        roles=frozenset({"author", "review"}), model_source="cli",
        notes="subscription backed CLI; model aliases come from policy cli_models."),
    "gemini": RunnerProfile(
        name="gemini", provider="gemini", family="google", kind="api",
        classes=("economical", "standard", "advanced"),
        review_classes=("economical", "standard", "advanced"),
        roles=frozenset({"author", "review", "validation"}),
        notes="Gemini Flash class is the principal low-cost worker."),
    "groq": RunnerProfile(
        name="groq", provider="groq", family=None, kind="api",
        classes=("economical",), review_classes=("economical",),
        roles=frozenset({"author", "review", "validation"}), high_risk_ok=False,
        notes="low-risk tasks, test creation and validation; never alone for high-risk code."),
    "openrouter": RunnerProfile(
        name="openrouter", provider="openrouter", family=None, kind="api",
        classes=("economical", "standard", "advanced"),
        review_classes=("economical", "standard", "advanced"),
        roles=frozenset({"fallback", "review"}),
        notes="fallback and review only, pinned approved models and explicit data policy."),
    "kimi": RunnerProfile(
        name="kimi", provider="kimi", family="moonshot", kind="api",
        classes=("standard",), review_classes=("standard",),
        roles=frozenset({"author", "review"}), conditional="large_context",
        notes="optional; only when certified and the context size warrants it."),
}

_FAMILY_HINTS = (
    ("anthropic/", "anthropic"), ("claude", "anthropic"),
    ("openai/", "openai"), ("gpt", "openai"), ("o1", "openai"), ("o3", "openai"),
    ("o4", "openai"), ("codex", "openai"),
    ("google/", "google"), ("gemini", "google"), ("gemma", "google"),
    ("meta-llama/", "meta"), ("llama", "meta"),
    ("moonshotai/", "moonshot"), ("kimi", "moonshot"), ("moonshot", "moonshot"),
    ("qwen/", "alibaba"), ("qwen", "alibaba"),
    ("deepseek", "deepseek"), ("mistralai/", "mistral"), ("mistral", "mistral"),
    ("mixtral", "mistral"), ("x-ai/", "xai"), ("grok", "xai"),
)


def _reject_retired(name: object) -> str:
    norm = normalize_provider(name)
    if is_retired(norm):
        raise RetiredRunnerError("runner is retired and must never be routed: %s" % norm)
    return norm


def get_profile(name: str) -> RunnerProfile:
    norm = _reject_retired(name)
    if norm not in PROFILES:
        raise KeyError("unknown runner: %s" % norm)
    return PROFILES[norm]


def build_registry(names: Iterable[str]) -> list[RunnerProfile]:
    """Resolve configured runner names; any retired name aborts the whole build."""
    return [get_profile(name) for name in names]


def model_family(runner: str, model_id: str | None) -> str:
    """Model family used for reviewer independence."""
    profile = get_profile(runner)
    if profile.family:
        return profile.family
    text = (model_id or "").strip().lower()
    for hint, family in _FAMILY_HINTS:
        if text.startswith(hint):
            return family
    if "/" in text:
        vendor = text.split("/", 1)[0]
        for hint, family in _FAMILY_HINTS:
            if vendor.startswith(hint.rstrip("/")):
                return family
    return "unknown:%s:%s" % (runner, text or "none")


def _parse_ts(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.timestamp()


def certified_runners(records: Iterable[Mapping], now: float) -> tuple[dict, list[dict]]:
    """Split certification records into ``{runner: record}`` and exclusions.

    Only valid, unexpired records with status ``certified`` for a known,
    non-retired runner survive. Everything else is excluded with a reason.
    """
    certified: dict[str, Mapping] = {}
    excluded: list[dict] = []
    for rec in records:
        runner = normalize_provider(rec.get("runner") if isinstance(rec, Mapping) else None)
        if not isinstance(rec, Mapping):
            excluded.append({"runner": "", "reason": "invalid record"})
            continue
        if runner in RETIRED_PROVIDERS or is_retired(rec.get("provider")):
            excluded.append({"runner": runner, "reason": "retired runner"})
            continue
        if runner not in PROFILES:
            excluded.append({"runner": runner, "reason": "unknown runner"})
            continue
        status = rec.get("status")
        if status != "certified":
            excluded.append({"runner": runner, "reason": "status %s" % status})
            continue
        report = validate_certification_record(dict(rec))
        if not report.ok:
            excluded.append({"runner": runner, "reason": "invalid record: %s"
                             % "; ".join(report.errors[:3])})
            continue
        expires = _parse_ts(rec.get("expires_at"))
        if expires is not None and expires <= now:
            excluded.append({"runner": runner, "reason": "certification expired"})
            continue
        certified[runner] = rec
    return certified, excluded
