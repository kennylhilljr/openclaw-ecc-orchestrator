"""Slice 4b: cost-aware runner and reviewer selection."""

import copy
import unittest

from openclaw_ecc_orchestrator.routing import classify as C
from openclaw_ecc_orchestrator.routing.breaker import CircuitBreaker
from openclaw_ecc_orchestrator.routing.selection import select_reviewer, select_runner

NOW = 1_790_000_000.0


def m(model_id, price_in, price_out, deprecated=False):
    return {"id": model_id, "deprecated": deprecated, "input_usd_per_mtok": price_in,
            "output_usd_per_mtok": price_out, "context_length": None}


CATALOG = {
    "schema_version": "1.0", "fetched_at": "2026-09-21T00:00:00Z", "fetched_at_epoch": NOW,
    "providers": {
        "gemini": {"status": "ok", "error": None, "models": [
            m("gemini-flash-next", 0.1, 0.4), m("gemini-mid-next", 0.3, 2.5),
            m("gemini-pro-next", 1.25, 10.0)]},
        "groq": {"status": "ok", "error": None, "models": [
            m("open-small-9b-instant", 0.05, 0.08), m("retired-small-1b", 0.01, 0.01)]},
        "openrouter": {"status": "ok", "error": None, "models": [
            m("vendor-b/cheap", 0.01, 0.01), m("anthropic/reviewer-model", 0.2, 1.0),
            m("vendor-z/unapproved", 0.001, 0.001)]},
        "kimi": {"status": "ok", "error": None, "models": [m("kimi-long-context", 0.6, 2.5)]},
    },
}

POLICY = {
    "schema_version": "1.0", "high_risk_paths": ["**/security/**"],
    "required_checks": ["unit-tests"], "protected_commands": ["git push*"],
    "allowed_providers": ["claude", "codex", "gemini", "groq", "openrouter", "kimi"],
    "budget": {"max_cost_usd_per_unit": 5, "max_cost_usd_total": 50, "max_minutes_per_unit": 60},
    "model_preferences": {
        "gemini": {"economical": ["gemini-flash-*"], "standard": ["gemini-mid-*"],
                   "advanced": ["gemini-pro-*"]},
        "groq": {"economical": ["retired-small-*", "open-small-*"]},
        "openrouter": {"economical": ["vendor-b/*", "vendor-z/*"],
                       "standard": ["anthropic/*"], "advanced": ["anthropic/*"]},
        "kimi": {"standard": ["kimi-*"]},
    },
    "retired_model_ids": ["retired-small-1b"],
    "cli_models": {"codex": {"economical": "codex-econ", "standard": "codex-std",
                             "advanced": "codex-adv"}},
    "runner_costs": {"codex": {"economical": 0.05, "standard": 0.4, "advanced": 1.5},
                     "claude": {"economical": 0.02, "standard": 0.5, "advanced": 2.0}},
    "cost_estimation": {"input_tokens": 20000, "output_tokens": 4000},
    "openrouter": {"approved_models": ["vendor-b/cheap", "anthropic/reviewer-model"],
                   "data_policy": "no-training"},
    "catalog_max_age_seconds": 86400,
}

ALL = ["claude", "codex", "gemini", "groq", "openrouter", "kimi"]


def sel(tier, runners=ALL, **kw):
    kw.setdefault("now", NOW + 10)
    return select_runner(tier, runners, CATALOG, POLICY, **kw)


class SelectRunnerTests(unittest.TestCase):
    def test_cheapest_per_tier(self):
        cases = [(0, "groq", "open-small-9b-instant"), (1, "gemini", "gemini-mid-next"),
                 (2, "gemini", "gemini-pro-next")]
        for tier, runner, model in cases:
            with self.subTest(tier=tier):
                result = sel(tier)
                self.assertTrue(result.ok, result.reasons)
                self.assertEqual((result.runner, result.model), (runner, model))
                self.assertEqual(result.tier, tier)
                self.assertEqual(result.role, "author")

    def test_cost_from_catalog_metadata(self):
        result = sel(0)
        self.assertAlmostEqual(result.estimated_cost_usd, 20000 * 0.05 / 1e6 + 4000 * 0.08 / 1e6)
        self.assertEqual(result.cost_source, "catalog")

    def test_cost_from_config(self):
        result = sel(1, runners=["codex", "claude"])
        self.assertEqual(result.runner, "codex")
        self.assertEqual(result.cost_source, "config")
        self.assertAlmostEqual(result.estimated_cost_usd, 0.4)

    def test_deterministic_tie_break(self):
        policy = copy.deepcopy(POLICY)
        policy["runner_costs"]["claude"]["standard"] = 0.4
        for order in (["codex", "claude"], ["claude", "codex"]):
            result = select_runner(1, order, CATALOG, policy, now=NOW)
            self.assertEqual(result.runner, "claude")  # equal cost: alphabetical

    def test_only_certified_runners_considered(self):
        self.assertEqual(sel(0, runners=["codex"]).runner, "codex")
        self.assertFalse(sel(0, runners=[]).ok)

    def test_certification_records_accepted(self):
        records = {"codex": {"status": "certified"}, "groq": {"status": "failed"}}
        self.assertEqual(sel(0, runners=records).runner, "codex")

    def test_exclude_providers(self):
        self.assertEqual(sel(0, exclude_providers=["groq"]).runner, "gemini")

    def test_policy_disallowed_provider(self):
        policy = copy.deepcopy(POLICY)
        policy["allowed_providers"] = ["codex", "gemini"]
        result = select_runner(0, ALL, CATALOG, policy, now=NOW)
        self.assertEqual(result.runner, "gemini")
        rejected = {r["runner"]: r["reason"] for r in result.rejected}
        self.assertIn("not allowed", rejected["groq"])

    def test_open_breaker_excludes_provider(self):
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60, clock=lambda: 0.0)
        breaker.record_failure("groq")
        self.assertEqual(sel(0, breaker=breaker).runner, "gemini")

    def test_openrouter_is_fallback_only(self):
        self.assertNotEqual(sel(0).runner, "openrouter")  # cheapest but fallback only
        result = sel(0, runners=["openrouter", "claude"])
        self.assertEqual(result.runner, "openrouter")
        self.assertEqual(result.role, "fallback")
        self.assertEqual(result.model, "vendor-b/cheap")

    def test_openrouter_unapproved_model_never_selected(self):
        result = sel(0, runners=["openrouter"])
        self.assertNotEqual(result.model, "vendor-z/unapproved")

    def test_retired_model_id_never_selected(self):
        self.assertNotEqual(sel(0, runners=["groq"]).model, "retired-small-1b")

    def test_groq_never_for_high_risk(self):
        result = sel(0, risk="high")
        self.assertNotEqual(result.runner, "groq")
        self.assertEqual(result.runner, "gemini")

    def test_kimi_only_with_large_context(self):
        result = sel(1, runners=["kimi", "codex"])
        self.assertEqual(result.runner, "codex")
        self.assertEqual(sel(1, runners=["kimi", "codex"], large_context=True).runner, "kimi")

    def test_claude_not_a_tier0_author_but_coordinates(self):
        self.assertFalse(sel(0, runners=["claude"]).ok)
        coord = sel(0, runners=["claude"], role="coordination")
        self.assertTrue(coord.ok)
        self.assertEqual(coord.model, "haiku")

    def test_stale_catalog_excludes_api_runners(self):
        result = sel(0, now=NOW + 10 * 86400)
        self.assertEqual(result.runner, "codex")
        rejected = {r["runner"]: r["reason"] for r in result.rejected}
        self.assertIn("stale", rejected["groq"])

    def test_unknown_cost_sorts_last(self):
        policy = copy.deepcopy(POLICY)
        del policy["runner_costs"]["codex"]
        result = select_runner(0, ["codex", "gemini"], CATALOG, policy, now=NOW)
        self.assertEqual(result.runner, "gemini")
        only = select_runner(0, ["codex"], CATALOG, policy, now=NOW)
        self.assertEqual(only.runner, "codex")
        self.assertIsNone(only.estimated_cost_usd)

    def test_invalid_tier(self):
        for tier in (-1, 3, True, "1"):
            with self.subTest(tier=tier):
                self.assertFalse(sel(tier).ok)


def unit(**overrides):
    doc = {
        "schema_version": "1.0", "id": "U-2", "title": "t", "depends_on": [],
        "scope": {"files": ["src/pkg/a.py"]},
        "acceptance": {"commands": ["python -m unittest"]}, "risk": "low",
        "capabilities": {"network": False, "secrets": []},
        "routing": {"initial_tier": 0, "maximum_tier": 2, "reviewer_must_differ_from_author": True},
        "budget": {"attempts": 2, "minutes": 30, "maximum_cost_usd": 3.0}, "rollback": "revert",
    }
    doc.update(overrides)
    return doc


class SelectReviewerTests(unittest.TestCase):
    def rev(self, author_runner, author_model, tier, unit_doc, runners=ALL, diff=None):
        classification = C.classify(unit_doc, POLICY, diff)
        author = sel(tier, runners=[author_runner])
        self.assertTrue(author.ok, author.reasons)
        return select_reviewer(author, classification, runners, CATALOG, POLICY, now=NOW)

    def test_security_change_needs_tier2_independent_review(self):
        result = self.rev("codex", "codex-adv", 2, unit(traits=["security"]))
        self.assertTrue(result.ok, result.reasons)
        self.assertEqual(result.tier, 2)
        self.assertNotEqual(result.runner, "codex")
        self.assertNotEqual(result.family, "openai")
        self.assertEqual(result.role, "review")

    def test_security_change_with_tier1_author_still_tier2_review(self):
        doc = unit(traits=["security"], routing={"initial_tier": 0, "maximum_tier": 1,
                                                 "reviewer_must_differ_from_author": True})
        result = self.rev("codex", "codex-std", 1, doc)
        self.assertEqual(result.tier, 2)

    def test_prefers_different_family(self):
        # claude authors; openrouter anthropic model is cheaper but same family.
        result = self.rev("claude", "sonnet", 1, unit(), runners=["claude", "openrouter", "codex"])
        self.assertTrue(result.ok)
        self.assertEqual(result.runner, "codex")
        self.assertTrue(result.independent_family)

    def test_same_family_allowed_with_warning_below_tier2(self):
        result = self.rev("claude", "sonnet", 1, unit(), runners=["claude", "openrouter"])
        self.assertTrue(result.ok)
        self.assertEqual(result.runner, "openrouter")
        self.assertFalse(result.independent_family)
        self.assertTrue(result.warnings)

    def test_tier2_review_requires_different_family(self):
        result = self.rev("claude", "opus", 2, unit(risk="high"), runners=["claude", "openrouter"])
        self.assertFalse(result.ok)
        self.assertTrue(result.blocking)

    def test_small_deterministic_diff_allows_tier0_reviewer(self):
        result = self.rev("gemini", "gemini-flash-next", 0, unit(), diff={"lines_changed": 20},
                          runners=["gemini", "groq", "codex"])
        self.assertTrue(result.ok)
        self.assertEqual(result.tier, 0)
        self.assertEqual(result.runner, "groq")

    def test_openrouter_may_review(self):
        result = self.rev("gemini", "gemini-flash-next", 0, unit(), diff={"lines_changed": 20})
        self.assertEqual((result.runner, result.model), ("openrouter", "vendor-b/cheap"))

    def test_groq_never_reviews_high_risk(self):
        result = self.rev("codex", "codex-adv", 2, unit(risk="high"), runners=["codex", "groq"])
        self.assertFalse(result.ok)
        self.assertTrue(result.blocking)

    def test_reviewer_never_same_runner(self):
        result = self.rev("gemini", "gemini-mid-next", 1, unit(), runners=["gemini"])
        self.assertFalse(result.ok)

    def test_invalid_classification(self):
        bad = C.classify(unit(risk="nope"), POLICY)
        author = sel(1, runners=["codex"])
        self.assertFalse(select_reviewer(author, bad, ALL, CATALOG, POLICY, now=NOW).ok)


if __name__ == "__main__":
    unittest.main()
