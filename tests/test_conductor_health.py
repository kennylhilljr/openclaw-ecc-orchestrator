"""Conductor: no wall clock kill; hung runners are cancelled, slow ones never."""

import time
import unittest

from openclaw_ecc_orchestrator.process.health import HealthSettings

try:
    from ._integration import IntegrationBase
    from ._units import work_unit
except ImportError:  # discovered as a top level module
    from _integration import IntegrationBase
    from _units import work_unit


def plan(*units):
    return {"units": list(units)}


class ConductorHealthTests(IntegrationBase):
    def conductor_with_health(self, interval=0.2, checks=3, **kw):
        return self.conductor("c1", health_override=HealthSettings.for_tests(interval, checks), **kw)

    def by_type(self, event_type, unit_id="u1"):
        return [e for e in self.events() if e["type"] == event_type and e["unit_id"] == unit_id]

    def attention(self, reason, unit_id="u1"):
        return [e for e in self.by_type("attention.required", unit_id) if e["data"]["reason"] == reason]

    def test_defaults_have_no_wall_clock_limit(self):
        c, _, _ = self.conductor("c1")
        self.assertIsNone(c.runner_timeout)
        self.assertIsNone(c.gate_timeout)
        self.assertEqual(c.health_settings(), HealthSettings())

    def test_long_but_active_runner_is_never_cancelled_and_completion_is_announced(self):
        c, m, _ = self.conductor_with_health()
        self.ok(c.create_run(plan(work_unit("u1", files=["a.txt"])), run_id="r1"))
        self.ok(c.assign_unit("r1", "u1"))
        started = self.ok(c.start_unit("r1", "u1", self.argv("slow:2.5", "a.txt=a")))
        self.assertEqual(started["data"]["health"], {"interval_seconds": 0.2, "flat_checks_for_hang": 3})
        res = self.ok(c.wait_unit("r1", "u1"))
        self.assertFalse(res["data"]["process"]["hung"])
        self.assertFalse(res["data"]["process"]["cancelled"])
        self.assertGreaterEqual(res["data"]["process"]["health"]["checks"], 5)
        self.assertEqual(m.load("r1")["units"]["u1"]["state"], "reviewing")
        self.assertEqual(self.attention("hung_cancelled"), [])
        completed = self.by_type("unit.completed")
        self.assertEqual(len(completed), 1)
        data = completed[0]["data"]
        self.assertEqual((data["exit_code"], data["attempts"], data["hung"]), (0, 1, False))
        self.assertGreaterEqual(data["duration_seconds"], 2.5)
        self.assertIn("runner", data)
        self.assertIn("tier", data)

    def test_hung_runner_is_cancelled_and_failed_with_reason_hung(self):
        c, m, _ = self.conductor_with_health()
        self.ok(c.create_run(plan(work_unit("u1", files=["a.txt"])), run_id="r1"))
        self.ok(c.assign_unit("r1", "u1"))
        self.ok(c.start_unit("r1", "u1", self.argv("sleep:60")))
        start = time.monotonic()
        res = c.wait_unit("r1", "u1")
        self.assertLess(time.monotonic() - start, 20)
        self.assertFalse(res["ok"])
        proc = res["data"]["process"]
        self.assertTrue(proc["hung"])
        self.assertEqual(proc["cancel_reason"], "hung")
        unit = m.load("r1")["units"]["u1"]
        self.assertEqual((unit["state"], unit["last_reason"]), ("failed", "hung"))
        self.assertEqual(unit["annotations"]["health"]["state"], "hung")
        self.assertEqual([e["data"]["state"] for e in self.by_type("unit.health")], ["suspected_hang", "hung"])
        suspected = self.attention("suspected_hang")
        self.assertEqual(len(suspected), 1)
        self.assertEqual(suspected[0]["data"]["detail"]["evidence"]["flat_checks"], 1)
        cancelled = self.attention("hung_cancelled")
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0]["data"]["detail"]["evidence"]["flat_checks"], 3)
        self.assertTrue(self.by_type("unit.completed")[0]["data"]["hung"])
        # Normal escalation: the failed unit can be retried or reassigned.
        attempts = unit["annotations"]["escalation"]["attempts"]
        self.assertEqual(attempts[-1]["reason"], "hung")

    def test_minutes_budget_emits_long_running_and_never_cancels(self):
        c, m, _ = self.conductor_with_health()
        unit = work_unit("u1", files=["a.txt"], budget={"minutes": 0.01})  # 0.6 seconds
        self.ok(c.create_run(plan(unit), run_id="r1"))
        self.ok(c.assign_unit("r1", "u1"))
        self.ok(c.start_unit("r1", "u1", self.argv("slow:2.2", "a.txt=a")))
        res = self.ok(c.wait_unit("r1", "u1"))
        self.assertFalse(res["data"]["process"]["cancelled"])
        self.assertEqual(m.load("r1")["units"]["u1"]["state"], "reviewing")
        multiples = [e["data"]["detail"]["multiple"] for e in self.attention("long_running")]
        self.assertGreaterEqual(len(multiples), 2)
        self.assertEqual(multiples, sorted(set(multiples)))
        self.assertEqual(multiples[0], 1)
        self.assertEqual(self.attention("budget_exhausted"), [])

    def test_hung_gate_fails_verification_with_events(self):
        c, m, _ = self.conductor_with_health(checks=2)
        c.repo_checks = {"required": ["stuck"],
                         "commands": {"stuck": f"{self.py_quoted()} -c 'import time; time.sleep(60)'"}}
        self.ok(c.create_run(plan(work_unit("u1", files=["a.txt"])), run_id="r1"))
        self.ok(c.assign_unit("r1", "u1"))
        self.ok(c.start_unit("r1", "u1", self.argv("write", "a.txt=a")))
        res = c.wait_unit("r1", "u1")
        self.assertFalse(res["ok"])
        gates = res["data"]["verification"]["gates"]
        self.assertEqual([g["status"] for g in gates if g["name"] == "stuck"], ["hung"])
        self.assertEqual(m.load("r1")["units"]["u1"]["state"], "failed")
        hung = self.attention("hung_cancelled")
        self.assertEqual(hung[0]["data"]["detail"]["process"], "gate")
        self.assertEqual(hung[0]["data"]["detail"]["gate"], "stuck")

    def py_quoted(self):
        import shlex
        import sys
        return shlex.quote(sys.executable)


if __name__ == "__main__":
    unittest.main()
