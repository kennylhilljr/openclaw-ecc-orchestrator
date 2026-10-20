"""`ecc resume`: a stopped unit goes back to ready with its history kept
(live finding, rmcp-m0f: the drive stopped on review feedback and had no
operator path back except a new run)."""

import os
import unittest

try:
    from . import test_cli_support as S
except ImportError:  # discovered as a top level module
    import test_cli_support as S

from openclaw_ecc_orchestrator.runs.manager import RunManager
from openclaw_ecc_orchestrator.runs.store import RunStore


class ResumeTest(S.CliTestBase):
    def stop(self, unit_id):
        mgr = RunManager(RunStore(os.path.join(self.state, "runs")))
        run = mgr.load(self.RUN_ID)
        mgr.transition(self.RUN_ID, run["lease"]["conductor_id"], unit_id, "needs_user", reason="escalation_stopped")

    def test_resume_returns_a_stopped_unit_to_ready(self):
        self.create_run()
        self.dispatch("alpha", "alpha.txt=a", mode="fail")
        self.assertEqual(self.unit("alpha")["state"], "ready")
        self.stop("alpha")
        self.assertEqual(self.unit("alpha")["state"], "needs_user")
        doc = self.ok(self.reviewed("resume", "--run-id", self.RUN_ID, "--unit", "alpha"))
        self.assertEqual(doc["data"]["state"], "ready")
        self.assertEqual(self.unit("alpha")["attempts"], 1)
        self.ok(self.dispatch("alpha", "alpha.txt=a"))
        self.assertEqual(self.unit("alpha")["state"], "reviewing")

    def test_resume_refuses_a_ready_unit(self):
        self.create_run()
        code, doc = self.review_plan("resume", "--run-id", self.RUN_ID, "--unit", "alpha")
        self.assertNotEqual(code, 0)
        self.assertIn("unit_stopped", [c["name"] for c in doc["checks"] if not c["ok"]])

    def test_resume_refuses_when_no_attempt_is_left(self):
        self.create_run()
        for _ in range(12):
            if self.unit("alpha")["state"] != "ready":
                break
            self.dispatch("alpha", "alpha.txt=a", mode="fail")
        self.assertEqual(self.unit("alpha")["state"], "needs_user")
        code, doc = self.review_plan("resume", "--run-id", self.RUN_ID, "--unit", "alpha")
        self.assertNotEqual(code, 0)
        self.assertIn("attempts_left", [c["name"] for c in doc["checks"] if not c["ok"]])


if __name__ == "__main__":
    unittest.main()
