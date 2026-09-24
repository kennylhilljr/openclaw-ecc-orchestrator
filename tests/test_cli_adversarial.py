"""Adversarial tests for the operator CLI's confirmation and approval paths."""

import json
import os
import unittest

try:
    from . import test_cli_support as S
except ImportError:  # discovered as a top level module
    import test_cli_support as S


def failed(doc):
    return [c["name"] for c in doc["checks"] if not c["ok"]]


class YesFlagTest(S.CliTestBase):
    def create_args(self):
        return ("create-run", "--run-id", self.RUN_ID, "--plan", self.plan_path)

    def record_path(self, review_id):
        return os.path.join(self.state, "reviews", review_id + ".json")

    def tamper(self, review_id, **changes):
        path = self.record_path(review_id)
        with open(path) as fh:
            record = json.load(fh)
        record.update(changes)
        with open(path, "w") as fh:
            json.dump(record, fh)

    def test_yes_without_review_id_is_usage_error(self):
        before = self.snapshot()
        code, doc = self.cli(*self.create_args(), "--yes")
        self.assertEqual(code, 2)
        self.assertFalse(doc["ok"])
        self.assertEqual(before, self.snapshot())

    def test_unknown_review_id(self):
        code, doc = self.cli(*self.create_args(), "--yes", "--review-id", "rev-missing")
        self.assertEqual(code, 3)
        self.assertIn("review_record_found", failed(doc))

    def test_mismatched_plan_hash_refused(self):
        review_id = self.ok(self.review_plan(*self.create_args()))["data"]["review_id"]
        plan = dict(self.plan, units=self.plan["units"][:1])
        self.write_json("plan.json", plan)  # the plan the record approved no longer matches
        before = self.snapshot()
        code, doc = self.cli(*self.create_args(), "--yes", "--review-id", review_id)
        self.assertEqual(code, 3)
        self.assertIn("review_plan_hash_matches", failed(doc))
        self.assertTrue(doc["required_user_actions"])
        self.assertEqual(before, self.snapshot())

    def test_forged_hash_in_record_refused(self):
        review_id = self.ok(self.review_plan(*self.create_args()))["data"]["review_id"]
        self.tamper(review_id, plan_sha256="0" * 64)
        code, doc = self.cli(*self.create_args(), "--yes", "--review-id", review_id)
        self.assertEqual(code, 3)
        self.assertIn("review_plan_hash_matches", failed(doc))

    def test_record_marked_destructive_refused(self):
        review_id = self.ok(self.review_plan(*self.create_args()))["data"]["review_id"]
        self.tamper(review_id, destructive=True)
        code, doc = self.cli(*self.create_args(), "--yes", "--review-id", review_id)
        self.assertEqual(code, 3)
        self.assertIn("review_not_destructive", failed(doc))
        self.assertFalse(os.path.exists(os.path.join(self.state, "runs", self.RUN_ID)))

    def test_record_without_operator_refused(self):
        review_id = self.ok(self.review_plan(*self.create_args()))["data"]["review_id"]
        self.tamper(review_id, approved_by="")
        code, doc = self.cli(*self.create_args(), "--yes", "--review-id", review_id)
        self.assertEqual(code, 3)
        self.assertIn("review_operator_present", failed(doc))

    def test_expired_record_refused(self):
        review_id = self.ok(self.review_plan(*self.create_args()))["data"]["review_id"]
        self.tamper(review_id, expires_at="2000-01-01T00:00:00+00:00")
        code, doc = self.cli(*self.create_args(), "--yes", "--review-id", review_id)
        self.assertEqual(code, 3)
        self.assertIn("review_not_expired", failed(doc))

    def test_record_for_another_operation_refused(self):
        self.create_run()
        review_id = self.ok(self.review_plan(*self.dispatch_args("alpha", "alpha.txt=a")))["data"]["review_id"]
        code, doc = self.cli("create-run", "--run-id", "run2", "--plan", self.plan_path, "--yes",
                             "--review-id", review_id)
        self.assertEqual(code, 3)
        self.assertIn("review_operation_matches", failed(doc))

    def test_review_record_is_single_use(self):
        self.create_run()
        self.ok(self.dispatch("alpha", "alpha.txt=a"))
        args = ("verify", "--run-id", self.RUN_ID, "--unit", "alpha")
        review_id = self.ok(self.review_plan(*args))["data"]["review_id"]
        self.ok(self.cli(*args, "--yes", "--review-id", review_id))
        code, doc = self.cli(*args, "--yes", "--review-id", review_id)
        self.assertEqual(code, 3)
        self.assertIn("review_not_consumed", failed(doc))

    def test_review_plan_refuses_destructive_plan(self):
        head, request_id = self.to_review()
        code, doc = self.review_plan("merge", "--run-id", self.RUN_ID, "--unit", "alpha")
        self.assertEqual(code, 1)
        self.assertIn("plan_not_destructive", failed(doc))
        reviews = os.path.join(self.state, "reviews")
        for name in os.listdir(reviews):
            if name.endswith(".json"):
                with open(os.path.join(reviews, name)) as fh:
                    self.assertNotEqual(json.load(fh)["operation"], "merge")

    def test_merge_ignores_yes_even_with_a_forged_record(self):
        head, request_id = self.to_review()
        args = ("merge", "--run-id", self.RUN_ID, "--unit", "alpha")
        digest = self.ok(self.review_plan(*args, approve=False))["data"]["plan_sha256"]
        forged = {"schema_version": "1.0", "review_id": "rev-forged", "operation": "merge",
                  "plan_sha256": digest, "destructive": False, "approved_by": S.OPERATOR,
                  "session": S.SESSION, "approved_at": "2026-01-01T00:00:00+00:00",
                  "expires_at": "2099-01-01T00:00:00+00:00"}
        with open(os.path.join(self.state, "reviews", "rev-forged.json"), "w") as fh:
            json.dump(forged, fh)
        main_before = S.git(self.repo, "rev-parse", "main")
        code, doc = self.cli(*args, "--yes", "--review-id", "rev-forged")
        self.assertEqual(code, 3)
        self.assertTrue(any("--yes" in w for w in doc["warnings"]))
        self.assertEqual([a["kind"] for a in doc["required_user_actions"]], ["approve"])
        self.assertEqual(main_before, S.git(self.repo, "rev-parse", "main"))
        code, doc = self.cli(*args, tty=True, stdin="yes\n")
        self.assertEqual(code, 3, "an interactive yes cannot replace the broker approval for a merge")
        self.assertEqual(main_before, S.git(self.repo, "rev-parse", "main"))


class ApprovalTest(S.CliTestBase):
    def test_replayed_approval_refused(self):
        head, request_id = self.to_review()
        self.ok(self.reviewed(*self.approve_args(request_id)))
        code, doc = self.reviewed(*self.approve_args(request_id))
        self.assertEqual(code, 1)
        self.assertIn("request_pending", failed(doc))
        resolved = [e for e in self.events() if e["type"] == "approval.resolved"]
        self.assertEqual(len(resolved), 1)

    def test_approve_with_a_different_session_refused(self):
        head, request_id = self.to_review()
        code, doc = self.reviewed(*self.approve_args(request_id, session="sess-B"))
        self.assertEqual(code, 1)
        self.assertIn("binding_matches", failed(doc))
        pending = self.ok(self.cli("approvals", "list"))["data"]["pending"]
        self.assertEqual([p["request_id"] for p in pending], [request_id])
        self.ok(self.reviewed(*self.approve_args(request_id)))
        self.assertEqual(self.ok(self.cli("approvals", "list"))["data"]["pending"], [])

    def test_rejected_approval_blocks_merge(self):
        head, request_id = self.to_review()
        self.ok(self.reviewed(*self.approve_args(request_id, verb="reject")))
        main_before = S.git(self.repo, "rev-parse", "main")
        code, doc = self.cli("merge", "--run-id", self.RUN_ID, "--unit", "alpha")
        self.assertNotEqual(code, 0)
        self.assertEqual(main_before, S.git(self.repo, "rev-parse", "main"))

    def test_operator_identity_is_never_inferred(self):
        head, request_id = self.to_review()
        env = dict(self.env(), USER="alice", LOGNAME="alice", GIT_AUTHOR_NAME="alice")
        out, err = S.io.StringIO(), S.io.StringIO()
        code = S.main(["approvals", "approve", request_id, "--session", S.SESSION, "--config", self.config_path,
                       "--json", "--dry-run"], stdout=out, stderr=err, env=env, is_tty=lambda: True,
                      stdin=S.io.StringIO("y\n"), cwd=self.tmp)
        self.assertEqual(code, 2)
        code, doc = self.cli("approvals", "approve", request_id, "--operator", S.OPERATOR, "--dry-run")
        self.assertEqual(code, 2, "--session is required as well")
        argv = ["review-plan", "--json", "--approve", "--session", S.SESSION, "--",
                "verify", "--run-id", self.RUN_ID, "--unit", "alpha", "--config", self.config_path]
        code, out, err = self.run_cli(argv)
        self.assertEqual(code, 2)

    def test_operator_handle_must_not_be_an_email_or_secret(self):
        head, request_id = self.to_review()
        for bad in ("alice@example.com", "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"):
            code, doc = self.cli(*self.approve_args(request_id, operator=bad), "--dry-run")
            self.assertEqual(code, 2, bad)


class PathTraversalTest(S.CliTestBase):
    def test_run_and_unit_ids(self):
        for args in (("status", "--run-id", "../x"),
                     ("create-run", "--run-id", "../../etc", "--plan", self.plan_path),
                     ("dispatch", "--run-id", "run1", "--unit", "../alpha"),
                     ("cleanup", "--run-id", "run1/..", "--unit", "alpha"),
                     ("verify", "--run-id", ".hidden", "--unit", "alpha"),
                     ("create-run", "--run-id", "r", "--plan", self.plan_path, "--yes", "--review-id", "../rev")):
            code, doc = self.cli(*args)
            self.assertEqual(code, 2, args)
            self.assertFalse(doc["ok"])
        self.assertFalse(os.path.exists(self.state))

    def test_config_paths(self):
        for key, value in (("state_dir", "../outside/state"), ("worktree_root", "wt/../../x"),
                           ("policy_file", "../policy.json"), ("event_log", "state/../../events.jsonl"),
                           ("decision_inbox", "a/../../inbox")):
            self.config_path = self.write_json("traversal.json", dict(self.config, **{key: value}))
            code, doc = self.cli("status")
            self.assertEqual(code, 2, key)
            self.assertIn("config", doc["operation"] + json.dumps(doc["checks"]))

    def test_flag_paths_and_config_file_path(self):
        code, doc = self.cli("status", "--state-dir", self.tmp + "/state/../../escape")
        self.assertEqual(code, 2)
        code, out, err = self.run_cli(["status", "--config", self.tmp + "/x/../ecc.json", "--json"])
        self.assertEqual(code, 2)
        code, doc = self.cli("create-run", "--run-id", "r1", "--plan", self.tmp + "/../plan.json", "--dry-run")
        self.assertEqual(code, 2)
        code, doc = self.cli("create-run", "--run-id", "r1", "--plan", self.plan_path, "--dry-run",
                             "--backup-dir", self.tmp + "/b/../../b")
        self.assertEqual(code, 2)

    def test_state_dir_inside_openclaw_or_repo_refused(self):
        for state in (os.path.join(self.home, ".openclaw", "state"), os.path.join(self.repo, "state")):
            code, doc = self.cli("create-run", "--run-id", "r1", "--plan", self.plan_path, "--dry-run",
                                 "--state-dir", state)
            self.assertEqual(code, 2, state)


class NonTtyRefusalTest(S.CliTestBase):
    def test_every_gated_command_refuses_without_tty(self):
        code, doc = self.cli("create-run", "--run-id", self.RUN_ID, "--plan", self.plan_path)
        self.assertEqual(code, 3)
        self.create_run()
        checks = [self.dispatch_args("alpha", "alpha.txt=a")]
        before = self.snapshot()
        for args in checks:
            code, doc = self.cli(*args)
            self.assertEqual(code, 3, args)
            self.assertFalse(doc["ok"])
            self.assertTrue(doc["required_user_actions"])
        self.assertEqual(before, self.snapshot())
        self.ok(self.dispatch("alpha", "alpha.txt=a"))
        head = self.unit("alpha")["verified_head"]
        before = self.snapshot()
        for args in (("verify", "--run-id", self.RUN_ID, "--unit", "alpha"), self.review_args("alpha", head)):
            code, doc = self.cli(*args)
            self.assertEqual(code, 3, args)
            self.assertEqual(doc["required_user_actions"][0]["kind"], "confirm_plan")
        self.assertEqual(before, self.snapshot())
        request_id = self.ok(self.reviewed(*self.review_args("alpha", head)))["data"]["approval_request"]["request_id"]
        before = self.snapshot()
        code, doc = self.cli(*self.approve_args(request_id))
        self.assertEqual(code, 3)
        self.assertEqual(before, self.snapshot())

    def test_test_runner_override_needs_explicit_flag(self):
        self.create_run()
        code, doc = self.cli("dispatch", "--run-id", self.RUN_ID, "--unit", "alpha", "--runner-command-json",
                             self.runner_json("alpha.txt=a"), "--dry-run")
        self.assertEqual(code, 2)
        code, doc = self.cli("dispatch", "--run-id", self.RUN_ID, "--unit", "alpha", "--allow-test-runner",
                             "--runner-command-json", '"python3 -c pass"', "--dry-run")
        self.assertEqual(code, 2, "the override must be a JSON list of strings")


if __name__ == "__main__":
    unittest.main()
