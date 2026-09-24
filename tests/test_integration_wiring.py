"""Regression tests: schema validation, handoffs, routing, escalation and the
shared git state guard are wired into the run lifecycle."""

import copy
import glob
import json
import os
import unittest

try:
    from ._integration import IntegrationBase, exists_cmd, git
    from ._units import policy, work_unit
except ImportError:  # discovered as a top level module
    from _integration import IntegrationBase, exists_cmd, git
    from _units import policy, work_unit

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")


def plan(*units):
    return {"units": list(units)}


def load(path):
    with open(path) as fh:
        return json.load(fh)


class CreateRunValidationTests(IntegrationBase):
    def test_invalid_fixture_units_are_rejected(self):
        c, m, _ = self.conductor("c1")
        paths = sorted(glob.glob(os.path.join(FIXTURES, "work_units", "invalid", "*.json")))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(os.path.basename(path)):
                res = c.create_run(plan(load(path)), run_id="r-" + os.path.basename(path)[:-5])
                self.assertFalse(res["ok"])
                self.assertIn("work_units_valid", self.failed_checks(res))
        self.assertFalse(os.path.isdir(os.path.join(self.state_root, "runs")) and
                         os.listdir(os.path.join(self.state_root, "runs")))

    def test_path_traversal_scope_rejected(self):
        c, _, _ = self.conductor("c1")
        res = c.create_run(plan(work_unit("u1", files=["../outside/secrets.py"])), run_id="r1")
        self.assertFalse(res["ok"])
        self.assertIn("traversal", json.dumps(res["checks"]))

    def test_valid_fixture_units_accepted(self):
        c, _, _ = self.conductor("c1")
        units = [load(p) for p in sorted(glob.glob(os.path.join(FIXTURES, "work_units", "valid", "*.json")))]
        self.ok(c.create_run(plan(*units), run_id="r1"))

    def test_invalid_policy_rejected(self):
        bad = policy(allowed_providers=["windsurf"])
        c, _, _ = self.conductor("c1", policy=bad)
        res = c.create_run(plan(work_unit("u1")), run_id="r1")
        self.assertFalse(res["ok"])
        self.assertIn("repository_policy_valid", self.failed_checks(res))

    def test_legacy_budget_names_are_not_accepted_for_new_plans(self):
        c, _, _ = self.conductor("c1")
        unit = work_unit("u1")
        unit["budget"] = {"max_attempts": 3, "max_minutes": 10}
        self.assertFalse(c.create_run(plan(unit), run_id="r1")["ok"])


class HandoffGateTests(IntegrationBase):
    def setUp(self):
        super().setUp()
        self.c, self.m, _ = self.conductor("c1")
        unit = work_unit("u1", files=["docs/readme.md"], commands=[exists_cmd("docs/readme.md")])
        self.ok(self.c.create_run(plan(unit), run_id="r1"))
        self.ok(self.c.assign_unit("r1", "u1"))

    def finish(self, *argv):
        self.ok(self.c.start_unit("r1", "u1", self.argv(*argv)))
        return self.c.wait_unit("r1", "u1")

    def test_truthful_handoff_reaches_review(self):
        self.ok(self.finish("write", "docs/readme.md=hi"))
        u = self.m.load("r1")["units"]["u1"]
        self.assertEqual(u["state"], "reviewing")
        self.assertTrue(u["annotations"]["handoff"]["valid"])
        self.assertTrue(u["annotations"]["verification"]["handoff_valid"])

    def test_forged_success_with_failing_command_rejected(self):
        res = self.finish("forged", "docs/readme.md=hi")
        self.assertFalse(res["ok"])
        u = self.m.load("r1")["units"]["u1"]
        self.assertEqual(u["state"], "failed")
        self.assertEqual(u["last_reason"], "handoff_invalid")
        self.assertIn("success_with_failures", json.dumps(u["annotations"]["handoff"]))
        self.assertNotIn("verifying", [h["to"] for h in u["history"]])

    def test_out_of_scope_handoff_rejected(self):
        res = self.finish("claim=docs/readme.md,src/server.py", "docs/readme.md=hi")
        self.assertFalse(res["ok"])
        u = self.m.load("r1")["units"]["u1"]
        self.assertEqual(u["last_reason"], "handoff_invalid")
        self.assertIn("file_outside_scope", json.dumps(u["annotations"]["handoff"]))

    def test_missing_handoff_rejected(self):
        res = self.finish("nohandoff", "docs/readme.md=hi")
        self.assertFalse(res["ok"])
        self.assertEqual(self.m.load("r1")["units"]["u1"]["last_reason"], "handoff_missing")

    def test_manager_refuses_verifying_without_valid_handoff(self):
        self.m.transition("r1", "c1", "u1", "running")
        res = self.m.transition("r1", "c1", "u1", "verifying")
        self.assertFalse(res["ok"])
        self.assertIn("handoff", json.dumps(res["checks"]))


class RoutingWiringTests(IntegrationBase):
    def test_assignment_records_routing_decision(self):
        c, m, _ = self.conductor("c1")
        self.ok(c.create_run(plan(work_unit("u1", files=["docs/readme.md"])), run_id="r1"))
        res = self.ok(c.assign_unit("r1", "u1"))
        routing = m.load("r1")["units"]["u1"]["routing"]
        self.assertEqual(routing["runner"], "codex")
        self.assertEqual(routing["family"], "openai")
        self.assertEqual(routing["tier"], routing["decision"]["chosen_tier"])
        self.assertEqual(routing["decision"]["unit_id"], "u1")
        self.assertEqual(res["data"]["owner"], "codex")

    def test_dispatcher_assignment_records_classification(self):
        c, m, d = self.conductor("c1")
        self.ok(c.create_run(plan(work_unit("u1", files=["src/auth/login.py"])), run_id="r1"))
        self.ok(d.dispatch_unit("r1", "c1", "u1", "w1"))
        routing = m.load("r1")["units"]["u1"]["routing"]
        self.assertIsNone(routing["runner"])
        self.assertEqual(routing["decision"]["review_tier"], 2)

    def test_no_runner_moves_unit_to_needs_user(self):
        c, m, _ = self.conductor("c1", certified_runners=[])
        self.ok(c.create_run(plan(work_unit("u1")), run_id="r1"))
        res = c.assign_unit("r1", "u1")
        self.assertFalse(res["ok"])
        u = m.load("r1")["units"]["u1"]
        self.assertEqual((u["state"], u["last_reason"]), ("needs_user", "no_eligible_runner"))

    def test_objective_failure_escalates_tier(self):
        c, m, _ = self.conductor("c1")
        unit = work_unit("u1", files=["docs/readme.md"], commands=[exists_cmd("docs/never.md")],
                         budget={"attempts": 1}, routing={"initial_tier": 0, "maximum_tier": 1})
        self.ok(c.create_run(plan(unit), run_id="r1"))
        self.ok(c.assign_unit("r1", "u1"))
        self.assertEqual(m.load("r1")["units"]["u1"]["routing"]["tier"], 0)
        self.ok(c.start_unit("r1", "u1", self.argv("write", "docs/readme.md=a")))
        self.assertFalse(c.wait_unit("r1", "u1")["ok"])  # gates fail: objective failure
        u = m.load("r1")["units"]["u1"]
        self.assertEqual(u["annotations"]["escalation"]["current_tier"], 1)
        self.ok(m.transition("r1", "c1", "u1", "ready", reason="retry"))
        self.ok(c.assign_unit("r1", "u1"))
        self.assertEqual(m.load("r1")["units"]["u1"]["routing"]["tier"], 1)
        self.ok(c.start_unit("r1", "u1", self.argv("write", "docs/readme.md=b")))
        self.assertFalse(c.wait_unit("r1", "u1")["ok"])
        u = m.load("r1")["units"]["u1"]
        self.assertEqual((u["state"], u["last_reason"]), ("needs_user", "escalation_stopped"))

    def test_escalation_state_survives_conductor_restart(self):
        c, m, _ = self.conductor("c1")
        unit = work_unit("u1", files=["docs/readme.md"], commands=[exists_cmd("docs/never.md")],
                         budget={"attempts": 1}, routing={"initial_tier": 0, "maximum_tier": 1})
        self.ok(c.create_run(plan(unit), run_id="r1"))
        self.ok(c.assign_unit("r1", "u1"))
        self.ok(c.start_unit("r1", "u1", self.argv("write", "docs/readme.md=a")))
        c.wait_unit("r1", "u1")
        c2, m2, _ = self.conductor("c1")
        self.ok(m2.transition("r1", "c1", "u1", "ready", reason="retry"))
        self.ok(c2.assign_unit("r1", "u1"))
        self.assertEqual(m2.load("r1")["units"]["u1"]["routing"]["tier"], 1)


class BudgetTests(IntegrationBase):
    def test_schema_attempt_budget_enforced(self):
        c, m, d = self.conductor("c1")
        unit = load(os.path.join(FIXTURES, "work_units", "valid", "p1-03-downloader.json"))
        unit["budget"]["attempts"] = 1
        self.ok(c.create_run(plan(unit), run_id="r1"))
        self.ok(m.assign_unit("r1", "c1", unit["id"], "w"))
        self.ok(m.transition("r1", "c1", unit["id"], "failed", reason="x"))
        res = m.reassign_unit("r1", "c1", unit["id"], "w2", "retry")
        self.assertFalse(res["ok"])
        u = m.load("r1")["units"][unit["id"]]
        self.assertEqual((u["state"], u["attempts"]), ("needs_user", 1))

    def test_cost_budget_enforced(self):
        c, m, _ = self.conductor("c1")
        self.ok(c.create_run(plan(work_unit("u1", budget={"maximum_cost_usd": 1.0})), run_id="r1"))
        self.ok(m.assign_unit("r1", "c1", "u1", "w"))
        self.ok(m.transition("r1", "c1", "u1", "running"))
        self.ok(m.record_usage("r1", "c1", "u1", minutes=1, cost_usd=0.6))
        res = m.record_usage("r1", "c1", "u1", minutes=1, cost_usd=0.6)
        self.assertFalse(res["ok"])
        self.assertEqual(m.load("r1")["units"]["u1"]["state"], "needs_user")


class SharedGitStateGuardTests(IntegrationBase):
    def test_default_guard_is_wired_when_available(self):
        from openclaw_ecc_orchestrator.runs import conductor as C
        try:
            from openclaw_ecc_orchestrator.worktrees import guard
        except ImportError:
            self.assertIsNone(C.DEFAULT_GIT_STATE_GUARD)
            return
        self.assertEqual(C.DEFAULT_GIT_STATE_GUARD,
                         (guard.snapshot_shared_git_state, guard.diff_shared_git_state))

    def test_real_guard_flags_target_branch_move(self):
        from openclaw_ecc_orchestrator.runs import conductor as C
        if C.DEFAULT_GIT_STATE_GUARD is None:
            self.skipTest("worktrees.guard not available")
        c, m, _ = self.conductor("c1")
        self.ok(c.create_run(plan(work_unit("u1", files=["a.txt"], commands=[exists_cmd("a.txt")])), run_id="r1"))
        self.ok(c.assign_unit("r1", "u1"))
        evil = ("import subprocess\n"
                "open('a.txt','w').write('x\\n')\n"
                "subprocess.run(['git','add','a.txt']); subprocess.run(['git','commit','-qm','w'])\n"
                "head = subprocess.run(['git','rev-parse','HEAD'],capture_output=True,text=True).stdout.strip()\n"
                "subprocess.run(['git','update-ref','refs/heads/main',head])\n")
        self.ok(c.start_unit("r1", "u1", [self.argv("x")[0], "-c", evil]))
        self.assertFalse(c.wait_unit("r1", "u1")["ok"])
        u = m.load("r1")["units"]["u1"]
        self.assertEqual((u["state"], u["last_reason"]), ("needs_user", "shared_git_state_changed"))

    def test_runner_changing_shared_git_state_blocks_unit(self):
        calls = []

        def snapshot(repo):
            calls.append(("snapshot", repo))
            return {"refs/heads/main": git(repo, "rev-parse", "refs/heads/main")}

        def diff(before, after, allow_refs=()):
            calls.append(("diff", tuple(allow_refs)))
            return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k)
                          and k not in allow_refs)

        c, m, _ = self.conductor("c1", git_state_guard=(snapshot, diff))
        self.ok(c.create_run(plan(work_unit("u1", files=["a.txt"], commands=[exists_cmd("a.txt")])), run_id="r1"))
        self.ok(c.assign_unit("r1", "u1"))
        evil = ("import subprocess, os, json\n"
                "open('a.txt','w').write('x\\n')\n"
                "subprocess.run(['git','add','a.txt']); subprocess.run(['git','commit','-qm','w'])\n"
                "head = subprocess.run(['git','rev-parse','HEAD'],capture_output=True,text=True).stdout.strip()\n"
                "subprocess.run(['git','update-ref','refs/heads/main',head])\n")
        self.ok(c.start_unit("r1", "u1", [self.argv("x")[0], "-c", evil]))
        res = c.wait_unit("r1", "u1")
        self.assertFalse(res["ok"])
        u = m.load("r1")["units"]["u1"]
        self.assertEqual((u["state"], u["last_reason"]), ("needs_user", "shared_git_state_changed"))
        self.assertEqual([c[0] for c in calls], ["snapshot", "snapshot", "diff"])
        self.assertEqual(calls[-1][1], ("refs/heads/ecc/r1/u1",))
        attention = [e for e in self.events() if e["type"] == "attention.required" and e["unit_id"] == "u1"]
        self.assertIn("shared_git_state_changed", [e["data"]["reason"] for e in attention])

    def gated_argv(self, flag, *specs):
        """Runner that waits for `flag` to exist, then does the normal write."""
        script = ("import os, runpy, sys, time\n"
                  f"flag, runner = {flag!r}, {self.runner!r}\n"
                  "deadline = time.time() + 20\n"
                  "while not os.path.exists(flag) and time.time() < deadline:\n"
                  "    time.sleep(0.02)\n"
                  f"sys.argv = [runner, 'write', *{list(specs)!r}]\n"
                  "runpy.run_path(runner, run_name='__main__')\n")
        return [self.argv("x")[0], "-c", script]

    def test_parallel_unit_branch_creation_is_not_a_violation(self):
        from openclaw_ecc_orchestrator.runs import conductor as C
        if C.DEFAULT_GIT_STATE_GUARD is None:
            self.skipTest("worktrees.guard not available")
        c, m, _ = self.conductor("c1")
        self.ok(c.create_run(plan(work_unit("a", files=["a.txt"], commands=[exists_cmd("a.txt")]),
                                  work_unit("b", files=["b.txt"], commands=[exists_cmd("b.txt")])), run_id="r1"))
        flag = os.path.join(self.tmp, "go")
        self.ok(c.assign_unit("r1", "a"))
        self.ok(c.assign_unit("r1", "b"))
        self.ok(c.start_unit("r1", "a", self.gated_argv(flag, "a.txt=a")))
        # The conductor creates b's branch while a's runner is working.
        self.ok(c.start_unit("r1", "b", self.argv("write", "b.txt=b")))
        self.ok(c.wait_unit("r1", "b"))
        open(flag, "w").close()
        self.ok(c.wait_unit("r1", "a"))
        units = m.load("r1")["units"]
        self.assertEqual((units["a"]["state"], units["b"]["state"]), ("reviewing", "reviewing"))

    def test_merge_queue_moving_target_during_run_is_not_a_violation(self):
        from openclaw_ecc_orchestrator.runs import conductor as C
        if C.DEFAULT_GIT_STATE_GUARD is None:
            self.skipTest("worktrees.guard not available")
        c, m, _ = self.conductor("c1")
        self.ok(c.create_run(plan(work_unit("a", files=["a.txt"], commands=[exists_cmd("a.txt")]),
                                  work_unit("b", files=["b.txt"], commands=[exists_cmd("b.txt")])), run_id="r1"))
        flag = os.path.join(self.tmp, "go")
        self.ok(self.drive_to_review(c, "r1", "b", self.argv("write", "b.txt=b")))
        self.ok(c.assign_unit("r1", "a"))
        self.ok(c.start_unit("r1", "a", self.gated_argv(flag, "a.txt=a")))
        # b merges (the conductor moves main) while a's runner is working.
        request = self.ok(self.review(c, "r1", "b"))["data"]["approval_request"]
        self.ok(c.enqueue_merge("r1", "b", self.decide(c, request)))
        self.assertEqual(self.ok(c.process_merge_queue())["data"]["status"], "merged")
        open(flag, "w").close()
        self.ok(c.wait_unit("r1", "a"))
        self.assertEqual(m.load("r1")["units"]["a"]["state"], "reviewing")

    def test_runner_moving_target_after_own_merge_is_still_flagged(self):
        from openclaw_ecc_orchestrator.runs import conductor as C
        if C.DEFAULT_GIT_STATE_GUARD is None:
            self.skipTest("worktrees.guard not available")
        c, m, _ = self.conductor("c1")
        self.ok(c.create_run(plan(work_unit("a", files=["a.txt"], commands=[exists_cmd("a.txt")]),
                                  work_unit("b", files=["b.txt"], commands=[exists_cmd("b.txt")])), run_id="r1"))
        flag = os.path.join(self.tmp, "go")
        self.ok(self.drive_to_review(c, "r1", "b", self.argv("write", "b.txt=b")))
        self.ok(c.assign_unit("r1", "a"))
        evil = ("import os, subprocess, time\n"
                f"flag = {flag!r}\n"
                "while not os.path.exists(flag):\n"
                "    time.sleep(0.02)\n"
                "open('a.txt','w').write('x\\n')\n"
                "subprocess.run(['git','add','a.txt']); subprocess.run(['git','commit','-qm','w'])\n"
                "head = subprocess.run(['git','rev-parse','HEAD'],capture_output=True,text=True).stdout.strip()\n"
                "subprocess.run(['git','update-ref','refs/heads/main',head])\n")
        self.ok(c.start_unit("r1", "a", [self.argv("x")[0], "-c", evil]))
        request = self.ok(self.review(c, "r1", "b"))["data"]["approval_request"]
        self.ok(c.enqueue_merge("r1", "b", self.decide(c, request)))
        self.assertEqual(self.ok(c.process_merge_queue())["data"]["status"], "merged")
        open(flag, "w").close()
        self.assertFalse(c.wait_unit("r1", "a")["ok"])
        u = m.load("r1")["units"]["a"]
        self.assertEqual((u["state"], u["last_reason"]), ("needs_user", "shared_git_state_changed"))


if __name__ == "__main__":
    unittest.main()
