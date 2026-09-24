"""Deterministic end-to-end drive of a 3-unit plan through the runtime.

alpha: success, reviewed, approved, merged.
beta:  fails, retried, killed by a simulated machine restart, resumed by a
       new conductor, explicitly reassigned with a handoff, merged.
gamma: deferred by the dispatcher while alpha is active, then conflicts at
       merge, is cancelled mid-run, exhausts its attempt budget, and is
       finally cancelled by the operator.
"""

import json
import os
import signal
import threading
import time
import unittest

from openclaw_ecc_orchestrator.merge_queue.conflicts import predict_changed_conflicts
from openclaw_ecc_orchestrator.process.supervisor import pid_gone

try:
    from . import _integration as H
    from ._units import work_unit
except ImportError:  # discovered as a top level module
    import _integration as H
    from _units import work_unit

PY = H.PY
SECRET = "runner-secret-value-5f3a9c"
git = H.git
exists_cmd = H.exists_cmd
FakeClock = H.FakeClock
# The shared runner, plus a line echoing a credential so the test can prove
# it is redacted everywhere the runtime writes.
RUNNER = H.RUNNER.replace('print("start", mode, flush=True)',
                          'print("start", mode, flush=True)\n'
                          'print("using token " + os.environ.get("RUNNER_API_TOKEN", ""), flush=True)', 1)
assert RUNNER != H.RUNNER

# `budget.attempts` is per tier (schema field); these units stay on one tier.
PLAN = {"units": [
    work_unit("alpha", files=["alpha.txt", "README.md"], commands=[exists_cmd("alpha.txt")], risk="low"),
    work_unit("beta", files=["beta.txt"], commands=[exists_cmd("beta.txt")], risk="medium",
              budget={"attempts": 3}),
    work_unit("gamma", files=["README.md", "docs/gamma.md"], commands=[exists_cmd("docs/gamma.md")], risk="low",
              budget={"attempts": 2}),
]}


class ResumeIntegrationTest(H.IntegrationBase):
    def setUp(self):
        super().setUp()
        with open(self.runner, "w") as fh:
            fh.write(RUNNER)
        self.remote = os.path.join(self.tmp, "remote.git")
        git(self.tmp, "init", "-q", "--bare", self.remote)
        git(self.repo, "remote", "add", "origin", self.remote)
        git(self.repo, "push", "-q", "origin", "main")

    def write(self, *specs):
        return self.argv("write", *specs)

    def approve(self, c, run_id, unit_id, reviewer="rev-1", session="sess-A"):
        res = self.ok(self.review(c, run_id, unit_id, reviewer=reviewer, session=session))
        request = res["data"]["approval_request"]
        decision = {k: request[k] for k in ("request_id", "run_id", "unit_id", "action", "plan_sha256", "session_id")}
        decision.update(decision="approved", decided_by="operator")
        return decision

    def start_and_wait_ready(self, c, run_id, unit_id, argv):
        ready = threading.Event()
        res = self.ok(c.start_unit(run_id, unit_id, argv, on_line=lambda s, l: ready.set() if l == "ready" else None))
        self.assertTrue(ready.wait(10))
        return res

    def test_full_lifecycle(self):
        c1, m1, d1 = self.conductor("conductor-1")
        run_id = self.ok(c1.create_run(PLAN, run_id="run1"))["data"]["run_id"]
        base = m1.load(run_id)["metadata"]["base_commit"]

        # Dispatch: gamma overlaps alpha on README.md, so it is deferred.
        res = self.ok(d1.dispatch(run_id, "conductor-1", ["w1", "w2", "w3"]))
        self.assertEqual({a["unit_id"]: a["worker_id"] for a in res["data"]["assigned"]}, {"alpha": "w1", "beta": "w2"})
        self.assertEqual(res["data"]["deferred"][0]["unit_id"], "gamma")
        self.assertFalse(d1.dispatch_unit(run_id, "conductor-1", "gamma", "w3")["ok"])

        # alpha succeeds, beta fails, in parallel worktrees.
        self.ok(c1.start_unit(run_id, "alpha", self.write("alpha.txt=a", "README.md=alpha"),
                              extra_env={"RUNNER_API_TOKEN": SECRET}))
        self.ok(c1.start_unit(run_id, "beta", [PY, self.runner, "fail"]))
        wa = m1.load(run_id)["units"]["alpha"]["workspace"]["path"]
        wb = m1.load(run_id)["units"]["beta"]["workspace"]["path"]
        self.assertNotEqual(wa, wb)
        self.ok(c1.wait_unit(run_id, "alpha"))
        failed = c1.wait_unit(run_id, "beta")
        self.assertFalse(failed["ok"])
        run = m1.load(run_id)
        self.assertEqual(run["units"]["alpha"]["state"], "reviewing")
        self.assertTrue(run["units"]["alpha"]["annotations"]["verification"]["passed"])
        self.assertEqual(run["units"]["beta"]["state"], "failed")

        # alpha: the author cannot review itself; independent review, bound approval, merge.
        self.assertFalse(self.review(c1, run_id, "alpha", reviewer="w1")["ok"])
        decision = self.approve(c1, run_id, "alpha")
        self.assertFalse(c1.broker.resolve(dict(decision, session_id="sess-B"))["ok"])
        approval = self.ok(c1.broker.resolve(decision))["data"]
        self.assertFalse(c1.broker.resolve(decision)["ok"])  # replay
        self.ok(c1.enqueue_merge(run_id, "alpha", approval))
        self.assertEqual(self.ok(c1.process_merge_queue())["data"]["status"], "merged")
        self.assertEqual(m1.load(run_id)["units"]["alpha"]["state"], "merged")
        self.assertEqual(git(self.repo, "show", "main:README.md"), "alpha")

        # gamma can run now; it branched from the run base, so it conflicts at merge.
        res = self.ok(d1.dispatch(run_id, "conductor-1", ["w1"]))
        self.assertEqual(res["data"]["assigned"], [{"unit_id": "gamma", "worker_id": "w1"}])
        self.ok(c1.start_unit(run_id, "gamma", self.write("README.md=gamma", "docs/gamma.md=g")))
        self.ok(c1.wait_unit(run_id, "gamma"))
        pairs = predict_changed_conflicts(self.repo, base, {"alpha": "ecc/run1/alpha", "gamma": "ecc/run1/gamma"})
        self.assertEqual(pairs[0]["files"], ["README.md"])
        decision = self.approve(c1, run_id, "gamma")
        self.ok(c1.enqueue_merge(run_id, "gamma", self.ok(c1.broker.resolve(decision))["data"]))
        before = git(self.repo, "rev-parse", "main")
        out = c1.process_merge_queue()
        self.assertFalse(out["ok"])
        self.assertEqual(out["data"]["status"], "conflict")
        self.assertEqual(git(self.repo, "rev-parse", "main"), before)
        self.assertEqual(m1.load(run_id)["units"]["gamma"]["state"], "blocked")
        kinds = {(a["kind"], a["unit_id"]) for a in c1.required_user_actions(run_id)}
        self.assertIn(("resolve_conflict", "gamma"), kinds)
        self.assertIn(("retry_or_reassign_failed_unit", "beta"), kinds)

        # beta retry by the same owner, then the machine "restarts" mid-run.
        self.ok(m1.transition(run_id, "conductor-1", "beta", "ready", reason="retry"))
        self.ok(m1.assign_unit(run_id, "conductor-1", "beta", "w2"))
        started = self.start_and_wait_ready(c1, run_id, "beta", [PY, self.runner, "block"])
        pid = started["data"]["pid"]
        os.killpg(pid, signal.SIGKILL)
        deadline = time.monotonic() + 10
        while not pid_gone(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        c1.abandon()  # the old conductor process is gone; nothing it held is trusted
        self.clock.t += 120  # lease expires

        c2, m2, d2 = self.conductor("conductor-2")
        res = self.ok(c2.resume(run_id))
        self.assertEqual(res["data"]["interrupted"], ["beta"])
        self.assertIn("reassign_interrupted_unit", [a["kind"] for a in res["required_user_actions"]])
        run = m2.load(run_id)
        self.assertEqual(run["lease"]["conductor_id"], "conductor-2")
        self.assertIn(("conductor", "conductor-1", "conductor-2"),
                      [(h["kind"], h["from"], h["to"]) for h in run["handoffs"]])
        self.assertFalse(m1.transition(run_id, "conductor-1", "beta", "cancelled")["ok"])  # stale conductor

        # Explicit reassignment with a handoff record; the old owner may not review.
        res = self.ok(m2.reassign_unit(run_id, "conductor-2", "beta", "w3", reason="worker lost in restart"))
        handoff_id = res["data"]["handoff"]["id"]
        self.assertEqual(m2.load(run_id)["units"]["beta"]["handoff_ref"], handoff_id)
        self.ok(c2.start_unit(run_id, "beta", self.write("beta.txt=b")))
        self.ok(c2.wait_unit(run_id, "beta"))
        self.assertFalse(self.review(c2, run_id, "beta", reviewer="w2")["ok"])
        decision = self.approve(c2, run_id, "beta")
        self.ok(c2.enqueue_merge(run_id, "beta", self.ok(c2.broker.resolve(decision))["data"]))
        self.assertEqual(self.ok(c2.process_merge_queue())["data"]["status"], "merged")

        # gamma: retry, operator cancels the running attempt, budget then runs out.
        self.ok(m2.transition(run_id, "conductor-2", "gamma", "ready", reason="rebase requested"))
        self.ok(m2.assign_unit(run_id, "conductor-2", "gamma", "w1"))
        self.start_and_wait_ready(c2, run_id, "gamma", [PY, self.runner, "block"])
        res = c2.cancel_unit(run_id, "gamma", reason="operator", terminal=False)
        self.assertEqual(res["data"]["process"]["exit_code"], 143)
        self.assertTrue(res["data"]["process"]["cancelled"])
        self.assertEqual(m2.load(run_id)["units"]["gamma"]["state"], "failed")
        self.ok(m2.transition(run_id, "conductor-2", "gamma", "ready"))
        res = m2.assign_unit(run_id, "conductor-2", "gamma", "w1")
        self.assertFalse(res["ok"])
        self.assertEqual(m2.load(run_id)["units"]["gamma"]["state"], "needs_user")
        self.assertIn(("budget_exhausted", "gamma"), {(a["kind"], a["unit_id"]) for a in c2.required_user_actions(run_id)})
        self.ok(m2.transition(run_id, "conductor-2", "gamma", "cancelled", reason="operator"))

        # Final state.
        run = m2.load(run_id)
        self.assertEqual({u: run["units"][u]["state"] for u in run["units"]},
                         {"alpha": "merged", "beta": "merged", "gamma": "cancelled"})
        self.assertEqual(run["units"]["beta"]["attempts"], 3)
        files = git(self.repo, "ls-tree", "-r", "--name-only", "main").split()
        self.assertEqual(sorted(files), ["README.md", "alpha.txt", "beta.txt"])
        self.assertEqual(git(self.remote, "rev-parse", "main"), base)  # never pushed

        # Durable log rebuilds the same state as the snapshot.
        os.unlink(m2.store.snapshot_path(run_id))
        rebuilt = self.conductor("observer")[1].load(run_id)
        self.assertEqual({u: rebuilt["units"][u]["state"] for u in rebuilt["units"]},
                         {"alpha": "merged", "beta": "merged", "gamma": "cancelled"})
        seqs = [e["seq"] for e in m2.store.read_events(run_id)]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))

        # Events: every contract type, no secrets anywhere we wrote.
        with open(self.events_path) as fh:
            events = [json.loads(line) for line in fh]
        self.assertTrue({"run.created", "unit.state_changed", "unit.progress", "attention.required",
                         "approval.requested", "approval.resolved", "merge.completed"} <= {e["type"] for e in events})
        for root, _dirs, names in os.walk(self.state_root):
            for name in names:
                with open(os.path.join(root, name), errors="replace") as fh:
                    self.assertNotIn(SECRET, fh.read(), name)

        # Cleanup: merged work goes; gamma's unique commits must be archived first.
        wt = c2.worktrees
        self.ok(wt.cleanup(run_id, "alpha", target_branch="main"))
        self.ok(wt.cleanup(run_id, "beta", target_branch="main"))
        self.assertFalse(wt.cleanup(run_id, "gamma", target_branch="main")["ok"])
        self.assertTrue(os.path.isfile(self.ok(wt.cleanup(run_id, "gamma", target_branch="main", archive=True))
                                       ["rollback_checkpoint"]["bundle"]))


if __name__ == "__main__":
    unittest.main()
