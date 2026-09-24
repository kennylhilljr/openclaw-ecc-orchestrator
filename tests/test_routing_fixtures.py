"""End to end routing over fixtures: discovery -> classify -> select -> review -> escalate."""

import json
import pathlib
import unittest

from openclaw_ecc_orchestrator import schemas
from openclaw_ecc_orchestrator.routing import classify as C
from openclaw_ecc_orchestrator.routing.breaker import CircuitBreaker
from openclaw_ecc_orchestrator.routing.escalation import AttemptOutcome, EscalationController
from openclaw_ecc_orchestrator.routing.selection import select_reviewer, select_runner
from openclaw_ecc_orchestrator.runners import discovery

ROOT = pathlib.Path(__file__).resolve().parents[1] / "fixtures"
NOW = 1_790_000_000.0
ENV = {"GROQ_API_KEY": "placeholder-groq", "OPENROUTER_API_KEY": "placeholder-or",
       "GEMINI_API_KEY": "placeholder-gemini"}


def fixture(*parts):
    return json.loads(ROOT.joinpath(*parts).read_text(encoding="utf-8"))


def fake_http(url, headers, timeout):
    for provider, name in (("groq", "groq_models.json"), ("openrouter", "openrouter_models.json"),
                           ("gemini", "gemini_models.json")):
        if url.startswith(discovery.PROVIDER_ENDPOINTS[provider]["url"]):
            return discovery.HttpResponse(200, ROOT.joinpath("catalogs", name).read_text())
    return discovery.HttpResponse(404, "")


class FixtureRoutingTests(unittest.TestCase):
    def setUp(self):
        self.policy = fixture("policy", "config.json")
        self.catalog = discovery.fetch_catalog(["groq", "openrouter", "gemini"],
                                               http_get=fake_http, env=ENV, clock=lambda: NOW)

    def test_high_risk_unit_routes_to_tier2_with_independent_review(self):
        unit = fixture("work_units", "valid", "p1-03-downloader.json")
        classification = C.classify(unit, self.policy)
        self.assertTrue(classification.ok, classification.errors)
        self.assertEqual(classification.chosen_tier, 2)
        self.assertEqual(classification.review_tier, 2)
        author = select_runner(2, ["codex", "gemini", "groq", "openrouter"], self.catalog,
                               self.policy, risk=classification.risk, now=NOW + 60)
        self.assertTrue(author.ok, author.reasons)
        self.assertEqual((author.runner, author.model), ("gemini", "example-pro-2"))
        review = select_reviewer(author, classification, ["codex", "gemini", "groq", "openrouter"],
                                 self.catalog, self.policy, now=NOW + 60)
        self.assertTrue(review.ok, review.reasons)
        self.assertNotEqual(review.family, "google")
        self.assertEqual(review.tier, 2)
        decision = classification.to_routing_decision("2026-09-24T00:00:00Z", author.runner,
                                                      author.provider, author.model,
                                                      author.estimated_cost_usd)
        self.assertTrue(schemas.validate_routing_decision(decision).ok)

    def test_low_risk_unit_uses_economical_tier(self):
        unit = fixture("work_units", "valid", "docs-typo.json")
        classification = C.classify(unit, self.policy)
        self.assertEqual(classification.chosen_tier, 0)
        author = select_runner(0, ["codex", "gemini", "groq"], self.catalog, self.policy,
                               now=NOW + 60)
        self.assertIn(author.runner, ("gemini", "groq"))
        self.assertNotEqual(author.model, "example-open-small-legacy")

    def test_escalation_with_breaker(self):
        unit = fixture("work_units", "valid", "docs-typo.json")
        breaker = CircuitBreaker.from_policy({"circuit_breaker": {"failure_threshold": 2,
                                                                  "cooldown_seconds": 60}},
                                             clock=lambda: 0.0)
        ctl = EscalationController(unit, C.classify(unit, self.policy),
                                   tier_costs={0: 0.001, 1: 0.01}, breaker=breaker,
                                   clock=lambda: 0.0)
        ctl.record(AttemptOutcome(False, True, 0.001, runner="groq", provider="groq"))
        action = ctl.record(AttemptOutcome(False, True, 0.001, runner="groq", provider="groq"))
        self.assertEqual(action["action"], "escalate")
        self.assertTrue(breaker.is_open("groq"))
        nxt = select_runner(action["tier"], ["codex", "gemini", "groq"], self.catalog,
                            self.policy, breaker=breaker, now=NOW + 60)
        self.assertNotEqual(nxt.provider, "groq")


if __name__ == "__main__":
    unittest.main()
