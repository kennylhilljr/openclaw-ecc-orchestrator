"""End to end: drive a small real git repository through the operator CLI.

create-run, dispatch (fake runner), verify, review, approvals approve,
merge, status and cleanup, all non interactive (review records for the
non destructive plans, a broker approval for the merge). Asserts the event
log and the final git state.
"""

import os
import unittest

try:
    from . import test_cli_support as S
except ImportError:  # discovered as a top level module
    import test_cli_support as S


class CliEndToEndTest(S.CliTestBase):
    def test_full_lifecycle(self):
        base = S.git(self.repo, "rev-parse", "main")

        # create-run
        doc = self.create_run()
        self.assertEqual(doc["operation"], "create-run")
        self.assertEqual(self.unit("alpha")["state"], "ready")
        self.assertEqual(self.unit("beta")["state"], "pending")

        # dispatch with the injected fake runner
        doc = self.ok(self.dispatch("alpha", "alpha.txt=a"))
        self.assertEqual(doc["data"]["state"], "reviewing")
        alpha = self.unit("alpha")
        self.assertEqual((alpha["runner"], alpha["tier"], alpha["attempts"]), ("codex", 0, 1))
        head = alpha["verified_head"]
        self.assertEqual(S.git(self.repo, "rev-parse", "ecc/run1/alpha"), head)

        # verify (re-runs gates, changes no state)
        self.ok(self.reviewed("verify", "--run-id", self.RUN_ID, "--unit", "alpha"))

        # review requests a merge approval bound to the verified head
        doc = self.ok(self.reviewed(*self.review_args("alpha", head)))
        request_id = doc["data"]["approval_request"]["request_id"]
        pending = self.ok(self.cli("approvals", "list", "--run-id", self.RUN_ID))["data"]["pending"]
        self.assertEqual([(p["request_id"], p["action"], p["head_sha"]) for p in pending],
                         [(request_id, "merge", head)])

        # merge refuses before the approval exists
        code, doc = self.cli("merge", "--run-id", self.RUN_ID, "--unit", "alpha")
        self.assertEqual(code, 3)
        self.assertEqual(S.git(self.repo, "rev-parse", "main"), base)

        # approvals approve through the decision inbox
        doc = self.ok(self.reviewed(*self.approve_args(request_id)))
        self.assertEqual(doc["data"]["decision"]["decision"], "approved")
        processed = os.path.join(self.state, "decisions", "processed")
        self.assertTrue(any(n.endswith(".result.json") for n in os.listdir(processed)))

        # merge (destructive: admitted by the broker approval, no --yes)
        doc = self.ok(self.cli("merge", "--run-id", self.RUN_ID, "--unit", "alpha"))
        merged = doc["data"]["result"]["merged_commit"]
        self.assertEqual(S.git(self.repo, "rev-parse", "main"), merged)
        self.assertEqual(S.git(self.repo, "show", "main:alpha.txt"), "a")
        self.assertEqual(doc["rollback_checkpoint"]["previous_commit"], base)

        # status after merge: alpha merged, beta promoted to ready
        doc = self.ok(self.cli("status", "--run-id", self.RUN_ID))
        states = {u["unit_id"]: u["state"] for u in doc["data"]["units"]}
        self.assertEqual(states, {"alpha": "merged", "beta": "ready"})
        self.assertEqual(doc["required_user_actions"], [])

        # cleanup of the merged unit is not destructive
        wt_path = os.path.join(self.worktree_root)
        doc = self.ok(self.reviewed("cleanup", "--run-id", self.RUN_ID, "--unit", "alpha"))
        self.assertFalse(doc["data"]["plan"]["destructive"])
        self.assertEqual(S.git(self.repo, "branch", "--list", "ecc/run1/alpha"), "")
        worktrees = S.git(self.repo, "worktree", "list", "--porcelain")
        self.assertEqual(worktrees.count("worktree "), 1)
        self.assertTrue(os.path.isdir(wt_path))

        # final git state: main moved exactly once, nothing pushed, repo clean
        self.assertEqual(S.git(self.repo, "rev-list", "--count", f"{base}..main"), "1")
        self.assertEqual(S.git(self.repo, "remote"), "")
        self.assertEqual(S.git(self.repo, "status", "--porcelain"), "")

        # event log
        events = self.events()
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        types = [e["type"] for e in events]
        self.assertEqual(types[0], "run.created")
        transitions = [(e["unit_id"], e["data"]["to"]) for e in events if e["type"] == "unit.state_changed"]
        alpha_path = [to for uid, to in transitions if uid == "alpha"]
        self.assertEqual(alpha_path, ["ready", "assigned", "running", "verifying", "reviewing", "queued_for_merge",
                                      "merged"])
        self.assertIn(("beta", "ready"), transitions)
        for kind in ("approval.requested", "approval.resolved", "merge.completed"):
            self.assertEqual(types.count(kind), 1, kind)
        self.assertLess(types.index("approval.requested"), types.index("approval.resolved"))
        self.assertLess(types.index("approval.resolved"), types.index("merge.completed"))
        completed = next(e for e in events if e["type"] == "merge.completed")
        self.assertEqual(completed["data"]["merged_commit"], merged)
        self.assertIs(completed["data"]["pushed"], False)
        resolved = next(e for e in events if e["type"] == "approval.resolved")
        self.assertEqual(resolved["data"]["decided_by"], S.OPERATOR)

        # events tail sees the same log
        tail = self.ok(self.cli("events", "tail", "--run-id", self.RUN_ID))["data"]["events"]
        self.assertEqual([e["seq"] for e in tail], [e["seq"] for e in events])


if __name__ == "__main__":
    unittest.main()
