"""Adversarial inputs against discovery, the registry, probes and routing."""

import copy
import json
import unittest

from openclaw_ecc_orchestrator.routing import classify as C
from openclaw_ecc_orchestrator.routing.breaker import CircuitBreaker
from openclaw_ecc_orchestrator.routing.escalation import AttemptOutcome, EscalationController
from openclaw_ecc_orchestrator.routing.selection import select_reviewer, select_runner
from openclaw_ecc_orchestrator.runners import discovery, registry
from openclaw_ecc_orchestrator.schemas import CERTIFICATION_CHECKS

NOW = 1_790_000_000.0
RETIRED = ["windsurf", "Windsurf", " pi ", "PI", "openai-api", "openai_api", "openai",
           "openai-api-coding"]


def m(model_id, pin=0.1, pout=0.1, deprecated=False):
    return {"id": model_id, "deprecated": deprecated, "input_usd_per_mtok": pin,
            "output_usd_per_mtok": pout, "context_length": None}


CATALOG = {"schema_version": "1.0", "fetched_at": "x", "fetched_at_epoch": NOW, "providers": {
    "groq": {"status": "ok", "models": [m("small-live"), m("small-old", deprecated=True)]},
    "gemini": {"status": "ok", "models": [m("gemini-a"), m("gemini-b")]},
    "openrouter": {"status": "ok", "models": [m("free/any-model:free", 0, 0),
                                               m("vendor/pinned")]},
}}

POLICY = {
    "schema_version": "1.0", "high_risk_paths": [], "required_checks": [],
    "protected_commands": [], "allowed_providers": ["codex", "gemini", "groq", "openrouter"],
    "budget": {"max_cost_usd_per_unit": 10, "max_cost_usd_total": 10, "max_minutes_per_unit": 60},
    "model_preferences": {"groq": {"economical": ["small-gone", "small-old", "small-*"]},
                          "gemini": {"economical": ["gemini-*"], "standard": ["gemini-*"],
                                     "advanced": ["gemini-*"]},
                          "openrouter": {"economical": ["*"], "standard": ["*"],
                                         "advanced": ["*"]}},
    "cli_models": {"codex": {"economical": "c0", "standard": "c1", "advanced": "c2"}},
    "runner_costs": {"codex": {"economical": 1.0, "standard": 2.0, "advanced": 3.0}},
    "openrouter": {"approved_models": ["vendor/pinned"], "data_policy": "no-training"},
}


def unit(**overrides):
    doc = {
        "schema_version": "1.0", "id": "ADV-R", "title": "t", "depends_on": [],
        "scope": {"files": ["src/pkg/a.py"]}, "acceptance": {"commands": ["make test"]},
        "risk": "low", "capabilities": {"network": False, "secrets": []},
        "routing": {"initial_tier": 0, "maximum_tier": 2, "reviewer_must_differ_from_author": True},
        "budget": {"attempts": 2, "minutes": 60, "maximum_cost_usd": 10.0}, "rollback": "revert",
    }
    doc.update(overrides)
    return doc


class RetiredRunnerTests(unittest.TestCase):
    def test_retired_names_in_certified_list_never_selected(self):
        for tier in (0, 1, 2):
            for role in ("author", "review"):
                result = select_runner(tier, RETIRED, CATALOG, POLICY, role=role, now=NOW)
                self.assertFalse(result.ok)
                self.assertTrue(all(r["reason"] == "retired runner" for r in result.rejected))

    def test_retired_names_mixed_with_real_runners(self):
        result = select_runner(0, RETIRED + ["codex"], CATALOG, POLICY, now=NOW)
        self.assertEqual(result.runner, "codex")

    def test_forged_certification_records(self):
        forged = [{"schema_version": "1.0", "runner": name, "provider": name,
                   "status": "certified", "certified_at": "2026-09-01T00:00:00Z",
                   "expires_at": None, "version": "1", "models": [],
                   "checks": [{"name": n, "status": "pass", "reason": ""}
                              for n in CERTIFICATION_CHECKS]} for name in RETIRED]
        certified, excluded = registry.certified_runners(forged, now=NOW)
        self.assertEqual(certified, {})
        self.assertEqual(len(excluded), len(RETIRED))
        self.assertFalse(select_runner(0, forged, CATALOG, POLICY, now=NOW).ok)

    def test_retired_provider_in_unvalidated_policy_still_blocked(self):
        policy = copy.deepcopy(POLICY)
        policy["allowed_providers"] = ["windsurf", "pi", "openai-api"]
        result = select_runner(0, ["windsurf", "pi", "openai-api"], CATALOG, policy, now=NOW)
        self.assertFalse(result.ok)
        self.assertFalse(C.classify(unit(), policy).ok)

    def test_retired_provider_cannot_be_queried(self):
        for name in RETIRED:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    discovery.fetch_catalog([name], http_get=lambda *a: None, env={},
                                            clock=lambda: NOW)
                self.assertEqual(discovery.live_models(
                    {"providers": {name: {"status": "ok", "models": [m("x")]}}}, name), [])


class StaleModelTests(unittest.TestCase):
    def test_removed_and_deprecated_preferences_never_selected(self):
        result = select_runner(0, ["groq"], CATALOG, POLICY, now=NOW)
        self.assertEqual(result.model, "small-live")

    def test_all_preferences_stale_means_no_route(self):
        policy = copy.deepcopy(POLICY)
        policy["model_preferences"]["groq"]["economical"] = ["small-gone", "small-old"]
        result = select_runner(0, ["groq"], CATALOG, policy, now=NOW)
        self.assertFalse(result.ok)

    def test_retired_model_ids_denylist_beats_preferences(self):
        policy = copy.deepcopy(POLICY)
        policy["retired_model_ids"] = ["small-live", "c0"]
        self.assertFalse(select_runner(0, ["groq", "codex"], CATALOG, policy, now=NOW).ok)

    def test_provider_error_in_catalog(self):
        catalog = copy.deepcopy(CATALOG)
        catalog["providers"]["groq"] = {"status": "error", "models": [m("small-live")]}
        self.assertFalse(select_runner(0, ["groq"], catalog, POLICY, now=NOW).ok)

    def test_stale_catalog_blocks_api_runners(self):
        policy = dict(POLICY, catalog_max_age_seconds=60)
        result = select_runner(0, ["groq", "gemini"], CATALOG, policy, now=NOW + 3600)
        self.assertFalse(result.ok)

    def test_future_dated_catalog_is_suspect(self):
        policy = dict(POLICY, catalog_max_age_seconds=60)
        result = select_runner(0, ["groq"], CATALOG, policy, now=NOW - 86400)
        self.assertFalse(result.ok)

    def test_arbitrary_free_openrouter_model_never_selected(self):
        for role in ("author", "review"):
            result = select_runner(0, ["openrouter"], CATALOG, POLICY, role=role, now=NOW)
            self.assertEqual(result.model, "vendor/pinned")
        policy = copy.deepcopy(POLICY)
        policy["openrouter"]["approved_models"] = ["free/other:free"]
        self.assertFalse(select_runner(0, ["openrouter"], CATALOG, policy, now=NOW).ok)

    def test_openrouter_without_policy_block_never_selected(self):
        policy = copy.deepcopy(POLICY)
        del policy["openrouter"]
        self.assertFalse(select_runner(0, ["openrouter"], CATALOG, policy, now=NOW).ok)


class HighRiskGuardTests(unittest.TestCase):
    def test_cheap_runner_cannot_author_high_risk(self):
        classification = C.classify(unit(risk="high"), POLICY)
        self.assertEqual(classification.chosen_tier, 2)
        self.assertFalse(select_runner(classification.chosen_tier, ["groq"], CATALOG, POLICY,
                                       risk="high", now=NOW).ok)
        self.assertFalse(select_runner(0, ["groq"], CATALOG, POLICY, risk="high", now=NOW).ok)

    def test_high_risk_without_qualified_reviewer_blocks(self):
        classification = C.classify(unit(risk="high"), POLICY)
        author = select_runner(2, ["gemini"], CATALOG, POLICY, now=NOW)
        review = select_reviewer(author, classification, ["gemini", "groq"], CATALOG, POLICY,
                                 now=NOW)
        self.assertFalse(review.ok)
        self.assertTrue(review.blocking)

    def test_forged_author_tier_cannot_lower_review_tier(self):
        classification = C.classify(unit(traits=["security"]), POLICY)
        forged_author = {"runner": "codex", "tier": 0, "model": "c0"}
        review = select_reviewer(forged_author, classification, ["codex", "gemini", "groq"],
                                 CATALOG, POLICY, now=NOW)
        self.assertEqual(review.tier, 2)
        self.assertNotEqual(review.runner, "groq")

    def test_opinions_never_escalate(self):
        u = unit(budget={"attempts": 2, "minutes": 600, "maximum_cost_usd": 100.0})
        ctl = EscalationController(u, C.classify(u, POLICY), tier_costs={0: 0.1, 1: 1, 2: 2})
        ctl.record(AttemptOutcome(passed=False, reason="reviewer model dislikes it"))
        final = ctl.record(AttemptOutcome(passed=False, reason="reviewer model dislikes it"))
        self.assertEqual(final["action"], "stop")
        self.assertEqual(ctl.tier, 0)
        self.assertEqual(ctl.escalations, [])

    def test_breaker_names_are_normalized(self):
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60, clock=lambda: 0.0)
        breaker.record_failure(" GROQ ")
        self.assertTrue(breaker.is_open("groq"))
        result = select_runner(0, ["groq", "codex"], CATALOG, POLICY, breaker=breaker, now=NOW)
        self.assertEqual(result.runner, "codex")


class SecretLeakTests(unittest.TestCase):
    def test_selection_output_carries_no_env_values(self):
        result = select_runner(0, ["groq", "codex"], CATALOG, POLICY, now=NOW)
        self.assertNotIn("API_KEY", json.dumps(result.to_dict()))

    def test_discovery_error_redacts_key_fragments(self):
        key = "gsk_" + "Rb1Nc2Md3Le4Kf5Jg6Ih7Hi8Gj9Fk0Za"

        def http(url, headers, timeout):
            raise OSError("proxy rejected header Authorization: Bearer %s" % key)
        snap = discovery.fetch_catalog(["groq"], http_get=http, env={"GROQ_API_KEY": key},
                                       clock=lambda: NOW)
        text = json.dumps(snap)
        self.assertNotIn(key, text)
        self.assertNotIn(key[4:20], text)


if __name__ == "__main__":
    unittest.main()
