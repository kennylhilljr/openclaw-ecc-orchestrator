"""Findings of the rm-m0f retrospective and product review (research-mcp M0, 2026-10-25):

* M0-22, a security unit, merged on an inconclusive verifier verdict;
* M0-20 took seven attempts on repeated pr_reviewer rejections with no cap;
* four retries (M0-15, M0-16, M0-19, M0-21) had no cause in the retrospective, because the
  shared_git_state_changed stops never reached the escalation record;
* every unit reported cost_usd 0.0: the agents wrote no usage in their handoff, while Claude's own
  result line carried total_cost_usd.
"""

import json
import unittest
from types import SimpleNamespace

try:
    from .test_team_repair import RUNNER, RepairBase
    from .test_team_review import TeamBase
except ImportError:  # discovered as a top level module
    from test_team_repair import RUNNER, RepairBase
    from test_team_review import TeamBase

from openclaw_ecc_orchestrator.runs import state as S
from openclaw_ecc_orchestrator.team import personas as P
from openclaw_ecc_orchestrator.team.drive import Drive


def _blocked(head):
    doc = {"verdict": "pass", "head": head, "findings": [],
           "checks": [{"what": "sdd check", "how": "node tools/sdd/cli.mjs check", "expected": "pass",
                       "actual": "sandbox refused node", "ok": False, "blocked": "sandbox refused node"}]}
    return f"{P.VERIFY_MARK} {json.dumps(doc)}"


class InconclusiveOnHighRiskTests(TeamBase):
    def test_high_risk_unit_does_not_merge_on_an_inconclusive_verdict(self):
        self.setup_unit(high_risk=True)
        self.replies["verifier"] = [_blocked]
        self.step.run("r1", "u1")
        u = self.unit()
        self.assertEqual(u["state"], "needs_user")
        self.assertEqual(u["last_reason"], "verifier_inconclusive")


class RepeatedRejectionTests(RepairBase):
    def test_third_consecutive_rejection_by_one_role_stops_for_the_owner(self):
        self.make(attempts=10)
        for i in range(3):
            self.ok(self.run_attempt("write", f"a.txt={i}"))
            self.assertEqual(self.unit()["state"], "reviewing")
            self.c.return_for_changes("r1", "u1", "pr_reviewer_changes_requested",
                                      {"reason": "pr_reviewer_changes_requested"})
            if i < 2:
                self.assertEqual(self.unit()["state"], "ready")
        u = self.unit()
        self.assertEqual(u["state"], "needs_user")
        self.assertEqual(u["last_reason"], "repeated_rejections")


class CostFromRunnerOutputTests(RepairBase):
    def setUp(self):
        super().setUp()
        with open(self.script, "w") as fh:
            fh.write(RUNNER + '\nprint(json.dumps({"type": "result", "total_cost_usd": 1.25}))\n')

    def test_cost_comes_from_the_runner_result_line_when_the_handoff_has_none(self):
        self.make()
        self.ok(self.run_attempt("write", "a.txt=1"))
        self.assertAlmostEqual(self.unit()["cost_usd"], 1.25)


class RetroCausesTests(RepairBase):
    def test_every_stop_appears_in_the_retrospective(self):
        self.make()
        self.ok(self.c.assign_unit("r1", "u1"))
        self.m.transition("r1", self.c.id, "u1", S.RUNNING, reason="attempt 1")
        self.m.transition("r1", self.c.id, "u1", S.NEEDS_USER, reason="shared_git_state_changed")
        fake = SimpleNamespace(c=self.c, run_id="r1",
                               step=SimpleNamespace(rotation=lambda _r: SimpleNamespace(state={})))
        table = Drive.retro_table(fake, self.m.load("r1"))
        self.assertEqual(table["units"]["u1"]["failures"], ["shared_git_state_changed"])


if __name__ == "__main__":
    unittest.main()
