"""Slice 4c: escalation state machine and provider circuit breaker."""

import unittest

from openclaw_ecc_orchestrator.routing import classify as C
from openclaw_ecc_orchestrator.routing.breaker import CircuitBreaker
from openclaw_ecc_orchestrator.routing.escalation import AttemptOutcome, EscalationController


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def unit(attempts=2, minutes=60, cost=10.0, initial=0, maximum=2, risk="low", traits=None):
    doc = {
        "schema_version": "1.0", "id": "U-3", "title": "t", "depends_on": [],
        "scope": {"files": ["src/pkg/a.py"]}, "acceptance": {"commands": ["make test"]},
        "risk": risk, "capabilities": {"network": False, "secrets": []},
        "routing": {"initial_tier": initial, "maximum_tier": maximum,
                    "reviewer_must_differ_from_author": True},
        "budget": {"attempts": attempts, "minutes": minutes, "maximum_cost_usd": cost},
        "rollback": "revert",
    }
    if traits:
        doc["traits"] = traits
    return doc


COSTS = {0: 0.1, 1: 0.5, 2: 2.0}
FAIL = AttemptOutcome(passed=False, objective_failure=True, cost_usd=0.1,
                      reason="unit tests failed")
SOFT = AttemptOutcome(passed=False, objective_failure=False, cost_usd=0.1,
                      reason="model says it looks wrong")
PASS = AttemptOutcome(passed=True, cost_usd=0.1)


def controller(u=None, costs=COSTS, clock=None, policy=None):
    u = u or unit()
    classification = C.classify(u, policy)
    assert classification.ok, classification.errors
    return EscalationController(u, classification, tier_costs=costs, clock=clock or FakeClock(),
                                policy=policy)


class EscalationTests(unittest.TestCase):
    def test_first_action_is_attempt_at_lowest_allowed_tier(self):
        ctl = controller(unit(initial=1))
        action = ctl.next_action()
        self.assertEqual((action["action"], action["tier"], action["kind"]),
                         ("attempt", 1, "implementation"))

    def test_sequences(self):
        cases = [
            ("pass first", [PASS], ["done"], 0),
            ("repair then pass", [FAIL, PASS], ["repair", "done"], 0),
            ("fail twice escalates", [FAIL, FAIL], ["repair", "escalate"], 1),
            ("full ladder then stop", [FAIL] * 6,
             ["repair", "escalate", "repair", "escalate", "repair", "stop"], 2),
            ("soft failures never escalate", [SOFT, SOFT], ["repair", "stop"], 0),
            ("soft then objective escalates", [SOFT, FAIL], ["repair", "escalate"], 1),
        ]
        for label, outcomes, actions, final_tier in cases:
            with self.subTest(label):
                ctl = controller()
                ctl.next_action()
                got = [ctl.record(o)["action"] for o in outcomes]
                self.assertEqual(got, actions)
                self.assertEqual(ctl.tier, final_tier)

    def test_stop_reasons(self):
        ctl = controller()
        for _ in range(5):
            ctl.record(FAIL)
        final = ctl.record(FAIL)
        self.assertEqual(final["action"], "stop")
        self.assertIn("maximum tier", final["reason"])
        ctl = controller()
        ctl.record(SOFT)
        self.assertIn("objective", ctl.record(SOFT)["reason"])

    def test_never_exceeds_maximum_tier(self):
        ctl = controller(unit(maximum=1))
        actions = [ctl.record(FAIL)["action"] for _ in range(4)]
        self.assertEqual(actions, ["repair", "escalate", "repair", "stop"])
        self.assertEqual(ctl.tier, 1)

    def test_single_attempt_budget_escalates_without_repair(self):
        ctl = controller(unit(attempts=1))
        self.assertEqual(ctl.record(FAIL)["action"], "escalate")

    def test_projected_cost_rule_skips_cheap_retry(self):
        costs = {0: 0.6, 1: 1.0, 2: 3.0}
        ctl = controller(costs=costs)
        action = ctl.record(AttemptOutcome(passed=False, objective_failure=True, cost_usd=0.6))
        self.assertEqual(action["action"], "escalate")
        self.assertIn("projected", action["reason"])

    def test_projected_cost_rule_needs_objective_failure(self):
        costs = {0: 0.6, 1: 1.0, 2: 3.0}
        ctl = controller(costs=costs)
        action = ctl.record(AttemptOutcome(passed=False, objective_failure=False, cost_usd=0.6))
        self.assertEqual(action["action"], "repair")

    def test_cost_budget_stops(self):
        ctl = controller(unit(cost=1.0), costs={0: 0.4, 1: 2.0, 2: 5.0})
        ctl.record(AttemptOutcome(passed=False, objective_failure=True, cost_usd=0.4))
        action = ctl.record(AttemptOutcome(passed=False, objective_failure=True, cost_usd=0.4))
        self.assertEqual(action["action"], "stop")
        self.assertIn("budget", action["reason"])

    def test_spent_budget_stops(self):
        ctl = controller(unit(cost=0.5))
        action = ctl.record(AttemptOutcome(passed=False, objective_failure=True, cost_usd=0.6))
        self.assertEqual(action["action"], "stop")
        self.assertIn("cost budget", action["reason"])

    def test_policy_ceiling_tightens_unit_budget(self):
        policy = {"schema_version": "1.0", "high_risk_paths": [], "required_checks": [],
                  "protected_commands": [], "allowed_providers": ["codex"],
                  "budget": {"max_cost_usd_per_unit": 0.15, "max_cost_usd_total": 10,
                             "max_minutes_per_unit": 60}}
        ctl = controller(policy=policy)
        action = ctl.record(AttemptOutcome(passed=False, objective_failure=True, cost_usd=0.1))
        self.assertEqual(action["action"], "stop")

    def test_time_budget_stops(self):
        clock = FakeClock()
        ctl = controller(unit(minutes=10), clock=clock)
        ctl.next_action()
        clock.t = 11 * 60
        action = ctl.record(FAIL)
        self.assertEqual(action["action"], "stop")
        self.assertIn("time budget", action["reason"])

    def test_elapsed_measured_by_clock(self):
        clock = FakeClock(100.0)
        ctl = controller(clock=clock)
        ctl.next_action()
        clock.t = 130.0
        ctl.record(FAIL)
        self.assertEqual(ctl.history[0]["elapsed_seconds"], 30.0)

    def test_record_shape(self):
        ctl = controller()
        ctl.record(AttemptOutcome(passed=False, objective_failure=True, cost_usd=0.1,
                                  runner="groq", model="m0", usage={"input_tokens": 5},
                                  reason="tests failed"))
        ctl.record(AttemptOutcome(passed=False, objective_failure=True, cost_usd=0.1,
                                  runner="groq", model="m0", reason="tests failed"))
        ctl.record(AttemptOutcome(passed=True, cost_usd=0.4, runner="gemini", model="m1"))
        rec = ctl.to_record()
        self.assertEqual(rec["final"]["action"], "done")
        self.assertEqual(len(rec["attempts"]), 3)
        esc = rec["escalations"][0]
        for key in ("from_tier", "to_tier", "reason", "attempts", "elapsed_seconds",
                    "spent_usd", "usage", "target_runner", "target_model"):
            self.assertIn(key, esc)
        self.assertEqual((esc["target_runner"], esc["target_model"]), ("gemini", "m1"))
        self.assertAlmostEqual(rec["spent_usd"], 0.6)

    def test_record_after_terminal_raises(self):
        ctl = controller()
        ctl.record(PASS)
        with self.assertRaises(RuntimeError):
            ctl.record(PASS)

    def test_negative_cost_rejected(self):
        ctl = controller()
        with self.assertRaises(ValueError):
            ctl.record(AttemptOutcome(passed=False, cost_usd=-1.0))

    def test_invalid_classification_rejected(self):
        bad = C.classify(unit(risk="high", maximum=1), None)
        with self.assertRaises(ValueError):
            EscalationController(unit(risk="high", maximum=1), bad, tier_costs=COSTS)


class BreakerTests(unittest.TestCase):
    def test_opens_after_threshold_and_cools_down(self):
        clock = FakeClock()
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=100, clock=clock)
        for _ in range(2):
            breaker.record_failure("groq")
        self.assertFalse(breaker.is_open("groq"))
        breaker.record_failure("groq")
        self.assertTrue(breaker.is_open("groq"))
        self.assertEqual(breaker.state("groq"), "open")
        self.assertEqual(breaker.open_providers(), ["groq"])
        clock.t = 99
        self.assertTrue(breaker.is_open("groq"))
        clock.t = 100
        self.assertFalse(breaker.is_open("groq"))
        self.assertEqual(breaker.state("groq"), "half_open")

    def test_half_open_failure_reopens_and_success_closes(self):
        clock = FakeClock()
        breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=10, clock=clock)
        breaker.record_failure("codex")
        breaker.record_failure("codex")
        clock.t = 10
        breaker.record_failure("codex")
        self.assertTrue(breaker.is_open("codex"))
        clock.t = 20
        breaker.record_success("codex")
        self.assertEqual(breaker.state("codex"), "closed")
        breaker.record_failure("codex")
        self.assertFalse(breaker.is_open("codex"))

    def test_success_resets_consecutive_count(self):
        breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=10, clock=FakeClock())
        breaker.record_failure("gemini")
        breaker.record_success("gemini")
        breaker.record_failure("gemini")
        self.assertFalse(breaker.is_open("gemini"))

    def test_providers_independent(self):
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=10, clock=FakeClock())
        breaker.record_failure("groq")
        self.assertFalse(breaker.is_open("gemini"))

    def test_from_policy(self):
        policy = {"circuit_breaker": {"failure_threshold": 4, "cooldown_seconds": 30}}
        breaker = CircuitBreaker.from_policy(policy, clock=FakeClock())
        self.assertEqual((breaker.failure_threshold, breaker.cooldown_seconds), (4, 30))
        default = CircuitBreaker.from_policy({}, clock=FakeClock())
        self.assertGreaterEqual(default.failure_threshold, 1)

    def test_invalid_settings(self):
        with self.assertRaises(ValueError):
            CircuitBreaker(failure_threshold=0, cooldown_seconds=1)
        with self.assertRaises(ValueError):
            CircuitBreaker(failure_threshold=1, cooldown_seconds=-1)


if __name__ == "__main__":
    unittest.main()
