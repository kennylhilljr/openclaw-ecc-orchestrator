"""Complexity scoring and tier classification.

Score signals (each counted once):

====================  ======  ===============================================
signal                points  trigger
====================  ======  ===============================================
mechanical_small        -2    trait ``mechanical`` and <= 3 files
fixture_generation      -2    trait ``fixture_generation``
many_files              +1    > 3 files (diff_stats.files_changed or scope)
many_modules            +2    >= 3 distinct directories
large_diff              +1    > 300 changed lines (diff_stats)
database                +3    trait, or migration/schema/sql path tokens
security                +4    trait, capability secrets, or auth/secret paths
                              (camelCase and compound names included)
concurrency             +3    trait, or thread/lock/scheduler path tokens
architecture            +3    trait, or contract/interface path tokens
no_acceptance_test      +2    acceptance.commands is empty
previous_attempt_failed +2    keyword argument or trait
====================  ======  ===============================================

Score <= 0 is Tier 0, 1..5 Tier 1, >= 6 Tier 2. Risk sets a floor (high 2,
medium 1, low 0); a scope path matching ``policy.high_risk_paths`` forces
risk high. ``routing.initial_tier`` raises the floor, ``routing.maximum_tier``
caps the score. Nothing lowers a floor: if the floor exceeds
``maximum_tier`` the unit is rejected as invalid.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..schemas import (SCHEMA_VERSION, validate_repository_policy, validate_work_unit)
from ..tasks.scope import glob_match, is_glob, normalize

SIGNAL_POINTS = {
    "mechanical_small": -2,
    "fixture_generation": -2,
    "many_files": 1,
    "many_modules": 2,
    "large_diff": 1,
    "database": 3,
    "security": 4,
    "concurrency": 3,
    "architecture": 3,
    "no_acceptance_test": 2,
    "previous_attempt_failed": 2,
}
RISK_FLOOR = {"low": 0, "medium": 1, "high": 2}
LARGE_DIFF_LINES = 300
SMALL_DIFF_LINES = 50
REVIEW_SENSITIVE = ("security", "database", "architecture")

# Path tokens: each path is split on ``/ _ . -``, digits and case boundaries
# (``AuthService`` -> ``auth``, ``service``; ``OAuth2Client`` -> ``o``,
# ``auth``, ``client``), and the unsplit lowercase segments are kept too, so
# compound words (``authmiddleware``) are seen whole. A signal fires when a
# token equals a keyword, starts with one of the signal's prefixes (unless it
# starts with an exclusion), or ends with one of its suffixes.
#
# Deliberate non matches: ``author``/``authority`` (different words) and
# ``tokenizer`` (text processing, not credentials). Everything else errs
# toward a false positive, which only raises the review tier; explicit
# ``policy.high_risk_paths`` globs remain the authoritative override.
_PATH_KEYWORDS = {
    "database": ({"migration", "migrations", "alembic", "schema", "schemas", "sql", "db",
                  "database", "databases", "ddl"},
                 ("migrat", "schema"), ("migration", "migrations", "schema", "schemas"), ()),
    "security": ({"auth", "authn", "authz", "oauth", "jwt", "jwk", "jwks", "rbac", "abac", "acl",
                  "acls", "iam", "sso", "saml", "mfa", "otp", "totp", "tls", "ssl", "login",
                  "logout", "signin", "security", "secure", "secret", "secrets", "credential",
                  "credentials", "token", "tokens", "crypto", "password", "passwords", "passwd",
                  "session", "sessions", "permission", "permissions", "privilege", "privileges",
                  "sandbox", "keystore", "apikey", "apikeys", "cert", "certs", "certificate",
                  "certificates"},
                 ("auth", "oauth", "jwt", "rbac", "secur", "crypt", "credential", "passw",
                  "secret", "permission", "privileg", "session"),
                 ("auth", "token", "tokens", "secret", "secrets", "password", "passwords",
                  "credential", "credentials", "session", "sessions", "permission",
                  "permissions"),
                 ("author", "sessional", "tokeniz")),
    "concurrency": ({"lock", "locks", "locking", "mutex", "queue", "queues", "pool", "workers",
                     "semaphore"},
                    ("concurren", "thread", "multiprocess", "subprocess", "scheduler", "async",
                     "parallel"), (), ()),
    "architecture": ({"contract", "contracts", "interface", "interfaces", "protocol",
                      "protocols"}, (), (), ()),
}
# Prefix exclusions that are nevertheless security words.
_EXCLUSION_OVERRIDES = ("authoriz",)

_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+")


def score_to_tier(score: int) -> int:
    if score <= 0:
        return 0
    if score <= 5:
        return 1
    return 2


def _tokens(path: str) -> list[str]:
    """Lowercase tokens: whole segments plus their case and digit split parts."""
    tokens: list[str] = []
    for segment in re.split(r"[/_.\-\s]+|[0-9]+", path):
        if not segment:
            continue
        tokens.append(segment.lower())
        parts = [p.lower() for p in _CAMEL_RE.findall(segment)]
        if len(parts) > 1:
            tokens.extend(parts)
    return tokens


def _token_hits(token: str, signal: str) -> bool:
    exact, prefixes, suffixes, exclusions = _PATH_KEYWORDS[signal]
    if token in exact:
        return True
    excluded = any(token.startswith(x) for x in exclusions) and \
        not any(token.startswith(o) for o in _EXCLUSION_OVERRIDES)
    if excluded:
        return False
    return any(token.startswith(p) for p in prefixes) or any(token.endswith(x) for x in suffixes)


def _path_signal(path: str, signal: str) -> bool:
    return any(_token_hits(token, signal) for token in _tokens(path))


def _literal_prefix(pattern: str) -> str:
    norm = normalize(pattern)
    cut = len(norm)
    for ch in "*?[":
        index = norm.find(ch)
        if index != -1:
            cut = min(cut, index)
    return norm[:cut]


def paths_may_overlap(entry: str, pattern: str) -> bool:
    """Conservative: could scope ``entry`` (path or glob) touch ``pattern``?"""
    if not is_glob(entry):
        return glob_match(entry, pattern)
    a, b = _literal_prefix(entry), _literal_prefix(pattern)
    return a.startswith(b) or b.startswith(a)


def _module(path: str) -> str:
    if is_glob(path):
        return _literal_prefix(path).rstrip("/")
    return posixpath.dirname(normalize(path))


@dataclass
class Classification:
    ok: bool
    unit_id: str | None
    score: int = 0
    signals: list[dict] = field(default_factory=list)
    risk: str | None = None
    declared_risk: str | None = None
    risk_minimum_tier: int | None = None
    minimum_tier: int | None = None
    score_tier: int | None = None
    chosen_tier: int | None = None
    maximum_tier: int | None = None
    review_tier: int | None = None
    requires_independent_review: bool = True
    small_deterministic: bool = False
    reasons: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def signal_names(self) -> set[str]:
        return {s["name"] for s in self.signals}

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    def to_routing_decision(self, decided_at: str, runner: str | None = None,
                            provider: str | None = None, model: str | None = None,
                            estimated_cost_usd: float | None = None) -> dict:
        if not self.ok:
            raise ValueError("cannot build a routing decision from an invalid classification")
        return {
            "schema_version": SCHEMA_VERSION, "unit_id": self.unit_id, "score": self.score,
            "signals": [dict(s) for s in self.signals], "risk": self.risk,
            "minimum_tier": self.minimum_tier, "chosen_tier": self.chosen_tier,
            "maximum_tier": self.maximum_tier, "review_tier": self.review_tier,
            "reasons": list(self.reasons), "runner": runner, "provider": provider,
            "model": model, "estimated_cost_usd": estimated_cost_usd, "decided_at": decided_at,
        }


def classify(unit: Mapping[str, Any], policy: Mapping[str, Any] | None,
             diff_stats: Mapping[str, Any] | None = None, *,
             previous_attempt_failed: bool = False) -> Classification:
    """Score ``unit`` and choose its starting tier.

    ``diff_stats`` (optional): ``files`` (list of changed paths),
    ``files_changed`` (int), ``lines_changed`` (int) or
    ``lines_added`` + ``lines_removed``.
    """
    report = validate_work_unit(unit)
    unit_id = unit.get("id") if isinstance(unit, Mapping) else None
    if not report.ok:
        return Classification(False, unit_id, errors=["invalid work unit: " + e
                                                      for e in report.errors])
    if policy:
        preport = validate_repository_policy(policy)
        if not preport.ok:
            return Classification(False, unit_id, errors=["invalid policy: " + e
                                                          for e in preport.errors])
    policy = policy or {}
    diff = dict(diff_stats or {})
    traits = set(unit.get("traits") or [])
    scope_files = list(unit["scope"]["files"])
    diff_files = [p for p in diff.get("files") or [] if isinstance(p, str)]
    paths = scope_files + [p for p in diff_files if p not in scope_files]
    count_basis = diff_files or scope_files
    file_count = diff.get("files_changed") if isinstance(diff.get("files_changed"), int) \
        else len(count_basis)
    lines = diff.get("lines_changed")
    if not isinstance(lines, int) and isinstance(diff.get("lines_added"), int):
        lines = diff["lines_added"] + int(diff.get("lines_removed") or 0)

    signals: list[dict] = []

    def add(name: str, reason: str) -> None:
        if name not in {s["name"] for s in signals}:
            signals.append({"name": name, "points": SIGNAL_POINTS[name], "reason": reason})

    if "mechanical" in traits and file_count <= 3:
        add("mechanical_small", "mechanical change in %d file(s)" % file_count)
    if "fixture_generation" in traits:
        add("fixture_generation", "deterministic fixture or test generation")
    if file_count > 3:
        add("many_files", "%d files changed" % file_count)
    modules = {_module(p) for p in count_basis}
    if len(modules) >= 3:
        add("many_modules", "%d modules touched" % len(modules))
    if isinstance(lines, int) and lines > LARGE_DIFF_LINES:
        add("large_diff", "%d changed lines" % lines)
    for name in ("database", "security", "concurrency", "architecture"):
        if name in traits:
            add(name, "declared trait %s" % name)
        else:
            hit = next((p for p in paths if _path_signal(p, name)), None)
            if hit:
                add(name, "path %s" % hit)
    if unit["capabilities"]["secrets"]:
        add("security", "unit requires secrets")
    if not unit["acceptance"]["commands"]:
        add("no_acceptance_test", "no deterministic acceptance command")
    if previous_attempt_failed or "previous_attempt_failed" in traits:
        add("previous_attempt_failed", "a previous verified attempt failed")

    order = list(SIGNAL_POINTS)
    signals.sort(key=lambda s: order.index(s["name"]))
    score = sum(s["points"] for s in signals)
    score_tier = score_to_tier(score)
    reasons = ["score %d maps to tier %d" % (score, score_tier)]

    declared = unit["risk"]
    risk = declared
    for pattern in policy.get("high_risk_paths") or []:
        hit = next((p for p in paths if paths_may_overlap(p, pattern)), None)
        if hit:
            if risk != "high":
                reasons.append("path %s matches high_risk_paths %s: risk forced high"
                               % (hit, pattern))
            risk = "high"
            break
    risk_floor = RISK_FLOOR[risk]
    initial = unit["routing"]["initial_tier"]
    maximum = unit["routing"]["maximum_tier"]
    minimum = max(risk_floor, initial)
    if risk_floor:
        reasons.append("risk %s forces tier >= %d" % (risk, risk_floor))
    if initial > risk_floor:
        reasons.append("initial_tier %d raises the floor" % initial)

    result = Classification(True, unit_id, score=score, signals=signals, risk=risk,
                            declared_risk=declared, risk_minimum_tier=risk_floor,
                            minimum_tier=minimum, score_tier=score_tier, maximum_tier=maximum,
                            reasons=reasons)
    if risk_floor > maximum:
        result.ok = False
        result.errors.append("risk-derived minimum tier %d exceeds unit maximum_tier %d; "
                             "the unit is invalid" % (risk_floor, maximum))
        return result
    chosen = max(score_tier, minimum)
    if chosen > maximum:
        reasons.append("capped at maximum_tier %d" % maximum)
        chosen = maximum
    result.chosen_tier = chosen

    names = {s["name"] for s in signals}
    small = (risk == "low" and bool(unit["acceptance"]["commands"]) and file_count <= 3
             and ((isinstance(lines, int) and lines <= SMALL_DIFF_LINES)
                  or bool(traits & {"mechanical", "fixture_generation"}))
             and not names & set(REVIEW_SENSITIVE))
    result.small_deterministic = small
    if names & set(REVIEW_SENSITIVE) or risk == "high" or chosen == 2:
        result.review_tier = 2
        reasons.append("tier 2 independent review required")
    elif small:
        result.review_tier = 0
    else:
        result.review_tier = 1
    result.requires_independent_review = (result.review_tier == 2 or
                                          unit["routing"]["reviewer_must_differ_from_author"])
    return result
