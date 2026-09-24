"""Versioned document validators (authoritative).

The JSON Schema files in ``schemas/`` describe the same documents for
tooling; ``tests/test_schemas_json.py`` keeps their ``required`` lists and
enum values in sync with ``DOCUMENT_SPECS`` below. Where the two disagree,
this module wins.

Loading: only JSON is parsed here. ``.orchestration/config.yaml`` must be
written as JSON compatible YAML (JSON is valid YAML 1.2) or parsed by the
caller into a dict before validation.

Every ``validate_*`` function returns a :class:`ValidationReport`. Error
strings are passed through secret redaction before they are stored, so a
report never echoes a credential that was present in the input.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from .handoffs.redaction import contains_secret, redact_text, scan_strings
from .tasks.scope import path_in_scope, unsafe_path_reason

SCHEMA_VERSION = "1.0"
ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
_ID_RE = re.compile(ID_PATTERN)
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")

RISKS = ("low", "medium", "high")
TIERS = (0, 1, 2)
CAPABILITY_CLASSES = ("economical", "standard", "advanced")
TRAITS = ("mechanical", "fixture_generation", "database", "security", "concurrency",
          "architecture", "previous_attempt_failed")
HANDOFF_STATUSES = ("succeeded", "failed", "partial", "blocked")
RUN_STATES = ("pending", "running", "verifying", "succeeded", "failed", "escalated",
              "blocked", "cancelled")
CERTIFICATION_STATUSES = ("certified", "failed", "not_configured", "retired")
CHECK_STATUSES = ("pass", "fail", "skip")
CERTIFICATION_CHECKS = ("installed", "authentication", "live_inference", "repo_exercise",
                        "cancellation_timeout", "log_inspection", "metadata_capture")
USAGE_KINDS = ("implementation", "repair", "review", "probe")
RUNNER_KINDS = ("coding_cli", "api")

KNOWN_PROVIDERS = ("claude", "codex", "gemini", "groq", "openrouter", "kimi")
# Retired or disabled for routine coding. Matching is case and space insensitive.
RETIRED_PROVIDERS = ("windsurf", "pi", "openai-api", "openai_api", "openai-api-coding",
                     "openai")


def normalize_provider(name: object) -> str:
    return str(name).strip().lower() if isinstance(name, str) else ""


def is_retired(name: object) -> bool:
    return normalize_provider(name) in RETIRED_PROVIDERS


class DocumentError(ValueError):
    """Raised by loaders; ``errors`` holds redacted validation messages."""

    def __init__(self, message: str, errors: list[str] | None = None):
        super().__init__(redact_text(message))
        self.errors = [redact_text(e) for e in (errors or [])]


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    violations: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.violations

    def error(self, message: str) -> None:
        self.errors.append(redact_text(message))

    def violation(self, kind: str, **details: Any) -> None:
        safe = {k: (redact_text(v) if isinstance(v, str) else v) for k, v in details.items()}
        self.violations.append({"kind": kind, **safe})

    def to_envelope(self, operation: str) -> dict:
        checks = [{"name": "schema", "ok": not self.errors, "errors": list(self.errors)}]
        if self.violations or operation.endswith("handoff"):
            checks.append({"name": "policy", "ok": not self.violations,
                           "violations": [dict(v) for v in self.violations]})
        return {
            "ok": self.ok,
            "operation": operation,
            "changed": False,
            "checks": checks,
            "warnings": list(self.warnings),
            "required_user_actions": [],
            "rollback_checkpoint": None,
        }


# ---------------------------------------------------------------- specs

DOCUMENT_SPECS: dict[str, dict] = {
    "work_unit": {
        "file": "work_unit.schema.json",
        "required": ["schema_version", "id", "title", "depends_on", "scope", "acceptance",
                     "risk", "capabilities", "routing", "budget", "rollback"],
        "optional": ["traits", "description"],
        "nested_required": {
            "scope": ["files"],
            "acceptance": ["commands"],
            "capabilities": ["network", "secrets"],
            "routing": ["initial_tier", "maximum_tier", "reviewer_must_differ_from_author"],
            "budget": ["attempts", "minutes", "maximum_cost_usd"],
        },
        "enums": {"risk": list(RISKS), "traits[]": list(TRAITS),
                  "routing.initial_tier": list(TIERS), "routing.maximum_tier": list(TIERS)},
    },
    "handoff": {
        "file": "handoff.schema.json",
        "required": ["schema_version", "unit_id", "status", "outcome", "files_changed",
                     "behavior", "commands", "unresolved_failures", "assumptions", "risks",
                     "next_action", "commit", "usage", "user_input_required"],
        "optional": ["notes"],
        "nested_required": {"commit": ["sha", "branch", "worktree"],
                            "commands[]": ["command", "exit_code", "result"]},
        "enums": {"status": list(HANDOFF_STATUSES)},
    },
    "repository_policy": {
        "file": "repository_policy.schema.json",
        "required": ["schema_version", "high_risk_paths", "required_checks",
                     "protected_commands", "allowed_providers", "budget"],
        "optional": ["model_preferences", "retired_model_ids", "cli_models", "runner_costs",
                     "cost_estimation", "circuit_breaker", "openrouter",
                     "catalog_max_age_seconds"],
        "nested_required": {"budget": ["max_cost_usd_per_unit", "max_cost_usd_total",
                                       "max_minutes_per_unit"]},
        "enums": {"allowed_providers[]": list(KNOWN_PROVIDERS)},
    },
    "routing_decision": {
        "file": "routing_decision.schema.json",
        "required": ["schema_version", "unit_id", "score", "signals", "risk", "minimum_tier",
                     "chosen_tier", "maximum_tier", "reasons", "decided_at"],
        "optional": ["runner", "provider", "model", "review_tier", "estimated_cost_usd"],
        "nested_required": {"signals[]": ["name", "points", "reason"]},
        "enums": {"risk": list(RISKS), "minimum_tier": list(TIERS),
                  "chosen_tier": list(TIERS), "maximum_tier": list(TIERS)},
    },
    "usage_record": {
        "file": "usage_record.schema.json",
        "required": ["schema_version", "unit_id", "runner", "tier", "attempt", "cost_usd",
                     "elapsed_seconds"],
        "optional": ["provider", "model", "input_tokens", "output_tokens", "recorded_at",
                     "kind"],
        "enums": {"tier": list(TIERS), "kind": list(USAGE_KINDS)},
    },
    "run_status": {
        "file": "run_status.schema.json",
        "required": ["schema_version", "unit_id", "state", "tier", "attempt", "updated_at"],
        "optional": ["reason", "runner"],
        "enums": {"state": list(RUN_STATES), "tier": list(TIERS)},
    },
    "verification_result": {
        "file": "verification_result.schema.json",
        "required": ["schema_version", "unit_id", "passed", "checks", "verified_at"],
        "optional": ["output_excerpt"],
        "nested_required": {"checks[]": ["name", "command", "exit_code", "passed"]},
    },
    "runner_certification": {
        "file": "runner_certification.schema.json",
        "required": ["schema_version", "runner", "provider", "status", "checks",
                     "certified_at", "version", "models"],
        "optional": ["family", "kind", "expires_at", "notes", "usage"],
        "nested_required": {"checks[]": ["name", "status", "reason"]},
        "enums": {"status": list(CERTIFICATION_STATUSES), "kind": list(RUNNER_KINDS),
                  "checks[].name": list(CERTIFICATION_CHECKS),
                  "checks[].status": list(CHECK_STATUSES)},
    },
}


# ---------------------------------------------------------------- primitives

def _show(value: Any) -> str:
    text = redact_text(repr(value))
    return text if len(text) <= 80 else text[:77] + "..."


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _is_tier(value: Any) -> bool:
    return _is_int(value) and value in TIERS


def _is_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _base(doc: Any, doc_type: str, report: ValidationReport) -> bool:
    """Type, version, required and unknown field checks. False if not a dict."""
    if not isinstance(doc, dict):
        report.error("%s must be a JSON object" % doc_type)
        return False
    spec = DOCUMENT_SPECS[doc_type]
    version = doc.get("schema_version")
    if not (isinstance(version, str) and version == SCHEMA_VERSION):
        report.error("unsupported schema_version %s (expected %s)"
                     % (_show(version), SCHEMA_VERSION))
    for name in spec["required"]:
        if name not in doc:
            report.error("missing required field: %s" % name)
    allowed = set(spec["required"]) | set(spec.get("optional", []))
    for name in doc:
        if name not in allowed:
            report.error("unknown field: %s" % _show(name))
    return True


def _check_id(value: Any, label: str, report: ValidationReport) -> None:
    if not (isinstance(value, str) and _ID_RE.match(value)):
        report.error("%s does not match %s: %s" % (label, ID_PATTERN, _show(value)))
    elif contains_secret(value):
        report.error("%s looks like a credential and was rejected (value redacted)" % label)


def _check_str_list(value: Any, label: str, report: ValidationReport,
                    allow_empty: bool = True) -> bool:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        report.error("%s must be a list of strings" % label)
        return False
    if not allow_empty and not value:
        report.error("%s must not be empty" % label)
        return False
    return True


def _obj(doc: dict, name: str, report: ValidationReport) -> dict:
    value = doc.get(name)
    if name in doc and not isinstance(value, dict):
        report.error("%s must be an object" % name)
        return {}
    return value if isinstance(value, dict) else {}


def _require_keys(obj: dict, parent: str, keys: list[str], report: ValidationReport) -> None:
    for key in keys:
        if key not in obj:
            report.error("missing required field: %s.%s" % (parent, key))


# ---------------------------------------------------------------- work unit

def validate_work_unit(doc: Any) -> ValidationReport:
    report = ValidationReport()
    if not _base(doc, "work_unit", report):
        return report
    spec = DOCUMENT_SPECS["work_unit"]
    _check_id(doc.get("id"), "id", report)
    if "title" in doc and not _nonempty_str(doc.get("title")):
        report.error("title must be a non-empty string")
    if "rollback" in doc and not _nonempty_str(doc.get("rollback")):
        report.error("rollback must be a non-empty string")

    deps = doc.get("depends_on")
    if "depends_on" in doc:
        if not isinstance(deps, list):
            report.error("depends_on must be a list of ids")
        else:
            for dep in deps:
                _check_id(dep, "depends_on entry", report)
            if doc.get("id") in deps:
                report.error("a unit cannot depend on itself")

    scope = _obj(doc, "scope", report)
    _require_keys(scope, "scope", spec["nested_required"]["scope"], report)
    files = scope.get("files")
    if "files" in scope and _check_str_list(files, "scope.files", report, allow_empty=False):
        for entry in files:
            reason = unsafe_path_reason(entry)
            if reason:
                report.error("scope.files entry rejected (%s): %s" % (reason, _show(entry)))

    acceptance = _obj(doc, "acceptance", report)
    _require_keys(acceptance, "acceptance", ["commands"], report)
    if "commands" in acceptance:
        cmds = acceptance.get("commands")
        if _check_str_list(cmds, "acceptance.commands", report):
            if any(not c.strip() for c in cmds):
                report.error("acceptance.commands entries must be non-empty")

    if "risk" in doc and doc.get("risk") not in RISKS:
        report.error("risk must be one of %s: %s" % (list(RISKS), _show(doc.get("risk"))))

    caps = _obj(doc, "capabilities", report)
    _require_keys(caps, "capabilities", ["network", "secrets"], report)
    if "network" in caps and not isinstance(caps.get("network"), bool):
        report.error("capabilities.network must be a boolean")
    if "secrets" in caps and _check_str_list(caps.get("secrets"), "capabilities.secrets", report):
        for name in caps["secrets"]:
            if not _ENV_NAME_RE.match(name):
                report.error("capabilities.secrets must list environment variable names, "
                             "got %s" % _show(name))

    routing = _obj(doc, "routing", report)
    _require_keys(routing, "routing", spec["nested_required"]["routing"], report)
    init, maxi = routing.get("initial_tier"), routing.get("maximum_tier")
    for name, value in (("initial_tier", init), ("maximum_tier", maxi)):
        if name in routing and not _is_tier(value):
            report.error("routing.%s must be an integer tier in 0..2: %s" % (name, _show(value)))
    if _is_tier(init) and _is_tier(maxi) and init > maxi:
        report.error("routing.initial_tier (%d) exceeds routing.maximum_tier (%d)" % (init, maxi))
    if ("reviewer_must_differ_from_author" in routing
            and not isinstance(routing.get("reviewer_must_differ_from_author"), bool)):
        report.error("routing.reviewer_must_differ_from_author must be a boolean")

    budget = _obj(doc, "budget", report)
    _require_keys(budget, "budget", spec["nested_required"]["budget"], report)
    if "attempts" in budget and not (_is_int(budget["attempts"]) and budget["attempts"] >= 1):
        report.error("budget.attempts must be an integer >= 1")
    for name in ("minutes", "maximum_cost_usd"):
        if name in budget and not (_is_num(budget[name]) and budget[name] >= 0):
            report.error("budget.%s must be a non-negative number" % name)

    if "traits" in doc:
        traits = doc.get("traits")
        if _check_str_list(traits, "traits", report):
            for trait in traits:
                if trait not in TRAITS:
                    report.error("unknown trait: %s" % _show(trait))
    if "description" in doc and not isinstance(doc.get("description"), str):
        report.error("description must be a string")
    for hit in scan_strings({k: v for k, v in doc.items() if k not in ("schema_version", "id")}):
        report.violation("secret_detected", field=hit)
    return report


# ---------------------------------------------------------------- handoff

def validate_handoff(doc: Any, unit: dict | None = None) -> ValidationReport:
    """Validate a handoff; with ``unit`` also check id and scope agreement."""
    report = ValidationReport()
    if not _base(doc, "handoff", report):
        return report
    _check_id(doc.get("unit_id"), "unit_id", report)
    status = doc.get("status")
    if "status" in doc and status not in HANDOFF_STATUSES:
        report.error("status must be one of %s" % list(HANDOFF_STATUSES))
    for name in ("outcome", "behavior", "next_action"):
        if name in doc and not isinstance(doc.get(name), str):
            report.error("%s must be a string" % name)
    for name in ("unresolved_failures", "assumptions", "risks"):
        if name in doc:
            _check_str_list(doc.get(name), name, report)
    if "user_input_required" in doc and not isinstance(doc.get("user_input_required"), bool):
        report.error("user_input_required must be a boolean")

    files = doc.get("files_changed")
    files_ok = "files_changed" in doc and _check_str_list(files, "files_changed", report)
    if files_ok:
        for path in files:
            reason = unsafe_path_reason(path)
            if reason:
                report.violation("unsafe_path", path=_show(path), reason=reason)

    commands = doc.get("commands")
    command_failures = 0
    if "commands" in doc:
        if not isinstance(commands, list):
            report.error("commands must be a list")
            commands = []
        for index, cmd in enumerate(commands):
            if not isinstance(cmd, dict):
                report.error("commands[%d] must be an object" % index)
                continue
            _require_keys(cmd, "commands[%d]" % index, ["command", "exit_code", "result"], report)
            if "command" in cmd and not _nonempty_str(cmd.get("command")):
                report.error("commands[%d].command must be a non-empty string" % index)
            code = cmd.get("exit_code")
            if "exit_code" in cmd and not _is_int(code):
                report.error("commands[%d].exit_code must be an integer" % index)
            elif _is_int(code) and code != 0:
                command_failures += 1
            if "result" in cmd and not isinstance(cmd.get("result"), str):
                report.error("commands[%d].result must be a string" % index)
    else:
        commands = []

    commit = _obj(doc, "commit", report)
    if "commit" in doc:
        _require_keys(commit, "commit", ["sha", "branch", "worktree"], report)
        sha = commit.get("sha")
        if "sha" in commit and not (isinstance(sha, str) and _SHA_RE.match(sha)):
            report.error("commit.sha must be a lowercase hex commit id")
        for name in ("branch", "worktree"):
            if name in commit and not _nonempty_str(commit.get(name)):
                report.error("commit.%s must be a non-empty string" % name)

    usage = doc.get("usage")
    if "usage" in doc and usage is not None:
        if not isinstance(usage, dict):
            report.error("usage must be an object or null")
        else:
            for name in ("input_tokens", "output_tokens"):
                if name in usage and not (_is_int(usage[name]) and usage[name] >= 0):
                    report.error("usage.%s must be a non-negative integer" % name)
            if "cost_usd" in usage and usage["cost_usd"] is not None and not (
                    _is_num(usage["cost_usd"]) and usage["cost_usd"] >= 0):
                report.error("usage.cost_usd must be a non-negative number")

    if status == "succeeded":
        if not commands:
            report.error("status succeeded requires at least one executed command result")
        if command_failures:
            report.violation("success_with_failures",
                             detail="%d command(s) exited non-zero" % command_failures)
        if isinstance(doc.get("unresolved_failures"), list) and doc["unresolved_failures"]:
            report.violation("success_with_failures", detail="unresolved_failures is not empty")

    for hit in scan_strings({k: v for k, v in doc.items() if k != "schema_version"}):
        report.violation("secret_detected", field=hit)

    if unit is not None and isinstance(unit, dict):
        if unit.get("id") is not None and doc.get("unit_id") != unit.get("id"):
            report.error("unit_id does not match the work unit id")
        scope = (unit.get("scope") or {}).get("files") or []
        if files_ok:
            for path in files:
                if unsafe_path_reason(path) is None and not path_in_scope(path, scope):
                    report.violation("file_outside_scope", path=path)
    return report


# ---------------------------------------------------------------- policy

def validate_repository_policy(doc: Any) -> ValidationReport:
    report = ValidationReport()
    if not _base(doc, "repository_policy", report):
        return report
    if "high_risk_paths" in doc and _check_str_list(doc["high_risk_paths"], "high_risk_paths",
                                                    report):
        for pattern in doc["high_risk_paths"]:
            reason = unsafe_path_reason(pattern)
            if reason:
                report.error("high_risk_paths entry rejected (%s): %s" % (reason, _show(pattern)))
    for name in ("required_checks", "protected_commands"):
        if name in doc and _check_str_list(doc[name], name, report):
            if any(not v.strip() for v in doc[name]):
                report.error("%s entries must be non-empty" % name)

    providers = doc.get("allowed_providers")
    allowed: list[str] = []
    if "allowed_providers" in doc and _check_str_list(providers, "allowed_providers", report,
                                                      allow_empty=False):
        for name in providers:
            norm = normalize_provider(name)
            if norm in RETIRED_PROVIDERS:
                report.error("allowed_providers contains retired provider %s" % _show(name))
            elif norm not in KNOWN_PROVIDERS or norm != name:
                report.error("allowed_providers contains unknown provider %s" % _show(name))
            else:
                allowed.append(norm)

    budget = _obj(doc, "budget", report)
    _require_keys(budget, "budget", DOCUMENT_SPECS["repository_policy"]["nested_required"]
                  ["budget"], report)
    for name, value in budget.items():
        if not (_is_num(value) and value >= 0):
            report.error("budget.%s must be a non-negative number" % _show(name))

    prefs = doc.get("model_preferences")
    if "model_preferences" in doc:
        if not isinstance(prefs, dict):
            report.error("model_preferences must be an object")
        else:
            for provider, classes in prefs.items():
                _check_provider_key(provider, "model_preferences", report)
                if not isinstance(classes, dict):
                    report.error("model_preferences.%s must be an object" % _show(provider))
                    continue
                for cls, patterns in classes.items():
                    if cls not in CAPABILITY_CLASSES:
                        report.error("unknown capability class %s" % _show(cls))
                    _check_str_list(patterns, "model_preferences.%s.%s"
                                    % (_show(provider), _show(cls)), report)

    cli_models = doc.get("cli_models")
    if "cli_models" in doc:
        if not isinstance(cli_models, dict):
            report.error("cli_models must be an object")
        else:
            for runner, classes in cli_models.items():
                _check_provider_key(runner, "cli_models", report)
                if not isinstance(classes, dict) or not all(
                        c in CAPABILITY_CLASSES and _nonempty_str(v) for c, v in classes.items()):
                    report.error("cli_models.%s must map capability classes to model aliases"
                                 % _show(runner))

    if "retired_model_ids" in doc:
        _check_str_list(doc["retired_model_ids"], "retired_model_ids", report)

    costs = doc.get("runner_costs")
    if "runner_costs" in doc:
        if not isinstance(costs, dict):
            report.error("runner_costs must be an object")
        else:
            for runner, classes in costs.items():
                _check_provider_key(runner, "runner_costs", report)
                if not isinstance(classes, dict) or not all(
                        c in CAPABILITY_CLASSES and _is_num(v) and v >= 0
                        for c, v in classes.items()):
                    report.error("runner_costs.%s must map capability classes to "
                                 "non-negative USD per attempt" % _show(runner))

    est = doc.get("cost_estimation")
    if "cost_estimation" in doc:
        if not isinstance(est, dict) or not all(
                k in ("input_tokens", "output_tokens") and _is_int(v) and v >= 0
                for k, v in est.items()):
            report.error("cost_estimation must hold non-negative input_tokens/output_tokens")

    breaker = doc.get("circuit_breaker")
    if "circuit_breaker" in doc:
        if not isinstance(breaker, dict):
            report.error("circuit_breaker must be an object")
        else:
            thr = breaker.get("failure_threshold")
            if not (_is_int(thr) and thr >= 1):
                report.error("circuit_breaker.failure_threshold must be an integer >= 1")
            cool = breaker.get("cooldown_seconds")
            if not (_is_num(cool) and cool >= 0):
                report.error("circuit_breaker.cooldown_seconds must be a non-negative number")

    age = doc.get("catalog_max_age_seconds")
    if "catalog_max_age_seconds" in doc and not (_is_num(age) and age > 0):
        report.error("catalog_max_age_seconds must be a positive number")

    orouter = doc.get("openrouter")
    if "openrouter" in doc:
        if not isinstance(orouter, dict):
            report.error("openrouter must be an object")
        else:
            models = orouter.get("approved_models")
            if _check_str_list(models, "openrouter.approved_models", report, allow_empty=False):
                for model in models:
                    if any(ch in model for ch in "*?[") or not model.strip():
                        report.error("openrouter.approved_models must be pinned exact ids: %s"
                                     % _show(model))
            if not _nonempty_str(orouter.get("data_policy")):
                report.error("openrouter.data_policy must be an explicit non-empty string")
    if "openrouter" in allowed and not isinstance(orouter, dict):
        report.error("openrouter is allowed but no openrouter.approved_models and "
                     "openrouter.data_policy are configured")
    return report


def _check_provider_key(name: Any, label: str, report: ValidationReport) -> None:
    if is_retired(name):
        report.error("%s references retired provider %s" % (label, _show(name)))
    elif name not in KNOWN_PROVIDERS:
        report.error("%s references unknown provider %s" % (label, _show(name)))


# ---------------------------------------------------------------- other records

def validate_routing_decision(doc: Any) -> ValidationReport:
    report = ValidationReport()
    if not _base(doc, "routing_decision", report):
        return report
    _check_id(doc.get("unit_id"), "unit_id", report)
    if "score" in doc and not _is_int(doc["score"]):
        report.error("score must be an integer")
    if "risk" in doc and doc["risk"] not in RISKS:
        report.error("risk must be one of %s" % list(RISKS))
    tiers = {}
    for name in ("minimum_tier", "chosen_tier", "maximum_tier", "review_tier"):
        if name in doc:
            if not _is_tier(doc[name]):
                report.error("%s must be an integer tier in 0..2" % name)
            else:
                tiers[name] = doc[name]
    if len(tiers) >= 3 and "chosen_tier" in tiers and not (
            tiers.get("minimum_tier", 0) <= tiers["chosen_tier"] <= tiers.get("maximum_tier", 2)):
        report.error("chosen_tier must lie within minimum_tier..maximum_tier")
    signals = doc.get("signals")
    if "signals" in doc:
        if not isinstance(signals, list):
            report.error("signals must be a list")
        else:
            for index, sig in enumerate(signals):
                if not (isinstance(sig, dict) and isinstance(sig.get("name"), str)
                        and _is_int(sig.get("points")) and isinstance(sig.get("reason"), str)):
                    report.error("signals[%d] must have name, integer points and reason" % index)
    if "reasons" in doc:
        _check_str_list(doc["reasons"], "reasons", report)
    for name in ("runner", "provider"):
        if doc.get(name) is not None:
            if is_retired(doc[name]):
                report.error("%s is retired: %s" % (name, _show(doc[name])))
    if "decided_at" in doc and not _is_timestamp(doc["decided_at"]):
        report.error("decided_at must be an ISO 8601 timestamp")
    return report


def validate_usage_record(doc: Any) -> ValidationReport:
    report = ValidationReport()
    if not _base(doc, "usage_record", report):
        return report
    _check_id(doc.get("unit_id"), "unit_id", report)
    if "runner" in doc and not _nonempty_str(doc["runner"]):
        report.error("runner must be a non-empty string")
    if "tier" in doc and not _is_tier(doc["tier"]):
        report.error("tier must be an integer tier in 0..2")
    if "attempt" in doc and not (_is_int(doc["attempt"]) and doc["attempt"] >= 1):
        report.error("attempt must be an integer >= 1")
    for name in ("cost_usd", "elapsed_seconds"):
        if name in doc and not (_is_num(doc[name]) and doc[name] >= 0):
            report.error("%s must be a non-negative number" % name)
    for name in ("input_tokens", "output_tokens"):
        if name in doc and doc[name] is not None and not (_is_int(doc[name]) and doc[name] >= 0):
            report.error("%s must be a non-negative integer" % name)
    if "kind" in doc and doc["kind"] not in USAGE_KINDS:
        report.error("kind must be one of %s" % list(USAGE_KINDS))
    if "recorded_at" in doc and not _is_timestamp(doc["recorded_at"]):
        report.error("recorded_at must be an ISO 8601 timestamp")
    return report


def validate_run_status(doc: Any) -> ValidationReport:
    report = ValidationReport()
    if not _base(doc, "run_status", report):
        return report
    _check_id(doc.get("unit_id"), "unit_id", report)
    if "state" in doc and doc["state"] not in RUN_STATES:
        report.error("state must be one of %s" % list(RUN_STATES))
    if "tier" in doc and not _is_tier(doc["tier"]):
        report.error("tier must be an integer tier in 0..2")
    if "attempt" in doc and not (_is_int(doc["attempt"]) and doc["attempt"] >= 0):
        report.error("attempt must be a non-negative integer")
    if "updated_at" in doc and not _is_timestamp(doc["updated_at"]):
        report.error("updated_at must be an ISO 8601 timestamp")
    return report


def validate_verification_result(doc: Any) -> ValidationReport:
    report = ValidationReport()
    if not _base(doc, "verification_result", report):
        return report
    _check_id(doc.get("unit_id"), "unit_id", report)
    passed = doc.get("passed")
    if "passed" in doc and not isinstance(passed, bool):
        report.error("passed must be a boolean")
    checks = doc.get("checks")
    all_passed = True
    if "checks" in doc:
        if not isinstance(checks, list):
            report.error("checks must be a list")
            checks = []
        for index, check in enumerate(checks):
            if not isinstance(check, dict):
                report.error("checks[%d] must be an object" % index)
                all_passed = False
                continue
            _require_keys(check, "checks[%d]" % index, ["name", "command", "exit_code", "passed"],
                          report)
            code, ok = check.get("exit_code"), check.get("passed")
            if not isinstance(ok, bool):
                report.error("checks[%d].passed must be a boolean" % index)
                all_passed = False
                continue
            if code is not None and not _is_int(code):
                report.error("checks[%d].exit_code must be an integer or null" % index)
            elif _is_int(code) and ok != (code == 0):
                report.violation("inconsistent_check", index=index,
                                 detail="passed disagrees with exit_code")
            all_passed = all_passed and ok
    if passed is True and (not checks or not all_passed):
        report.violation("success_with_failures",
                         detail="passed is true but a check failed or no checks ran")
    if "verified_at" in doc and not _is_timestamp(doc["verified_at"]):
        report.error("verified_at must be an ISO 8601 timestamp")
    return report


def validate_certification_record(doc: Any) -> ValidationReport:
    report = ValidationReport()
    if not _base(doc, "runner_certification", report):
        return report
    status = doc.get("status")
    if "status" in doc and status not in CERTIFICATION_STATUSES:
        report.error("status must be one of %s" % list(CERTIFICATION_STATUSES))
    for name in ("runner", "provider"):
        if name in doc:
            if not _nonempty_str(doc[name]):
                report.error("%s must be a non-empty string" % name)
            elif is_retired(doc[name]) and status != "retired":
                report.error("%s is retired and cannot hold status %s"
                             % (_show(doc[name]), _show(status)))
    checks = doc.get("checks")
    seen: dict[str, str] = {}
    if "checks" in doc:
        if not isinstance(checks, list):
            report.error("checks must be a list")
            checks = []
        for index, check in enumerate(checks):
            if not (isinstance(check, dict) and check.get("name") in CERTIFICATION_CHECKS
                    and check.get("status") in CHECK_STATUSES
                    and isinstance(check.get("reason"), str)):
                report.error("checks[%d] must have a known name, status and reason" % index)
                continue
            seen[check["name"]] = check["status"]
    if status == "certified":
        missing = [n for n in CERTIFICATION_CHECKS if n not in seen]
        if missing:
            report.error("certified record is missing checks: %s" % ", ".join(missing))
        if any(s == "fail" for s in seen.values()):
            report.error("certified record contains failing checks")
    if "kind" in doc and doc["kind"] not in RUNNER_KINDS:
        report.error("kind must be one of %s" % list(RUNNER_KINDS))
    if "version" in doc and doc["version"] is not None and not isinstance(doc["version"], str):
        report.error("version must be a string or null")
    if "models" in doc:
        _check_str_list(doc["models"], "models", report)
    if "certified_at" in doc and not _is_timestamp(doc["certified_at"]):
        report.error("certified_at must be an ISO 8601 timestamp")
    if doc.get("expires_at") is not None and not _is_timestamp(doc["expires_at"]):
        report.error("expires_at must be an ISO 8601 timestamp")
    for hit in scan_strings({k: v for k, v in doc.items() if k != "schema_version"}):
        report.violation("secret_detected", field=hit)
    return report


VALIDATORS = {
    "work_unit": validate_work_unit,
    "handoff": validate_handoff,
    "repository_policy": validate_repository_policy,
    "routing_decision": validate_routing_decision,
    "usage_record": validate_usage_record,
    "run_status": validate_run_status,
    "verification_result": validate_verification_result,
    "runner_certification": validate_certification_record,
}


# ---------------------------------------------------------------- loading

def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise DocumentError("duplicate key in document: %s" % _show(key))
        out[key] = value
    return out


def _reject_constant(name: str) -> Any:
    raise DocumentError("non-finite number %s is not allowed" % name)


def load_document(text: str) -> dict:
    """Parse a JSON document (JSON compatible YAML). Raises DocumentError."""
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicates,
                          parse_constant=_reject_constant)
    except DocumentError:
        raise
    except (ValueError, TypeError, RecursionError) as exc:
        raise DocumentError("document is not valid JSON (YAML must be JSON compatible): %s"
                            % type(exc).__name__) from None
    if not isinstance(data, dict):
        raise DocumentError("document must be a JSON object")
    return data


def load_validated(text: str, doc_type: str) -> dict:
    doc = load_document(text)
    report = VALIDATORS[doc_type](doc)
    if not report.ok:
        details = report.errors + ["%s: %s" % (v["kind"], v) for v in report.violations]
        raise DocumentError("invalid %s document" % doc_type, details)
    return doc


def load_work_unit(text: str) -> dict:
    return load_validated(text, "work_unit")


def load_repository_policy(text: str) -> dict:
    return load_validated(text, "repository_policy")
