"""Operator CLI: entry points, configuration, output envelope, exit codes,
confirmation gate, dry runs, status, events, doctor and backups."""

import json
import os
import subprocess
import sys
import unittest

try:
    from . import test_cli_support as S
except ImportError:  # discovered as a top level module
    import test_cli_support as S

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
ENVELOPE_KEYS = {"ok", "operation", "changed", "checks", "warnings", "required_user_actions", "rollback_checkpoint"}


def failed(doc):
    return [c["name"] for c in doc["checks"] if not c["ok"]]


class EntryPointTest(S.CliTestBase):
    def module(self, *args):
        env = dict(os.environ, PYTHONPATH=SRC, HOME=self.home)
        return subprocess.run([sys.executable, "-m", "openclaw_ecc_orchestrator", *args], cwd=self.tmp, env=env,
                              capture_output=True, text=True, timeout=60)

    def test_module_help_lists_subcommands(self):
        proc = self.module("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for name in ("create-run", "status", "dispatch", "verify", "review", "merge", "cleanup", "approvals",
                     "events", "doctor", "review-plan"):
            self.assertIn(name, proc.stdout)

    def test_module_doctor_json(self):
        proc = self.module("doctor", "--config", self.config_path, "--json")
        doc = json.loads(proc.stdout)
        self.assertEqual(set(doc) - {"data"}, ENVELOPE_KEYS)
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_console_script_declared(self):
        with open(os.path.join(os.path.dirname(SRC), "pyproject.toml")) as fh:
            text = fh.read()
        self.assertIn("[project.scripts]", text)
        self.assertIn('ecc-orchestrator = "openclaw_ecc_orchestrator.cli:main"', text)


class UsageAndConfigTest(S.CliTestBase):
    def test_unknown_subcommand_is_usage_error(self):
        code, doc = self.cli("frobnicate", config=False)
        self.assertEqual(code, 2)
        self.assertFalse(doc["ok"])
        self.assertEqual(doc["operation"], "usage")
        self.assertEqual(set(doc) - {"data"}, ENVELOPE_KEYS)

    def test_human_usage_error_goes_to_stderr(self):
        code, out, err = self.run_cli(["status", "--bogus"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("usage", err.lower())

    def test_unknown_config_key_is_usage_error(self):
        self.config_path = self.write_json("bad.json", dict(self.config, surprise=1))
        code, doc = self.cli("status")
        self.assertEqual(code, 2)
        self.assertIn("surprise", json.dumps(doc))

    def test_flags_override_config_file(self):
        other = os.path.join(self.tmp, "other-state")
        doc = self.ok(self.cli("doctor", "--state-dir", other))
        self.assertEqual(doc["data"]["config"]["state_dir"], other)

    def test_defaults_derive_from_xdg_dirs_at_runtime(self):
        env = {"HOME": self.home, "PATH": os.environ.get("PATH", ""),
               "XDG_STATE_HOME": os.path.join(self.tmp, "xdg-state")}
        out, err = S.io.StringIO(), S.io.StringIO()
        S.main(["doctor", "--repo", self.repo, "--json"], stdout=out, stderr=err, env=env, is_tty=lambda: False,
               cwd=self.tmp, stdin=S.io.StringIO())
        cfg = json.loads(out.getvalue())["data"]["config"]
        self.assertEqual(cfg["state_dir"], os.path.join(self.tmp, "xdg-state", "openclaw-ecc-orchestrator"))
        self.assertEqual(cfg["worktree_root"],
                         os.path.join(self.home, ".local", "share", "openclaw-ecc-orchestrator", "worktrees"))
        self.assertEqual(cfg["event_log"], os.path.join(cfg["state_dir"], "openclaw-events.jsonl"))
        self.assertEqual(cfg["decision_inbox"], os.path.join(cfg["state_dir"], "decisions"))

    def test_policy_defaults_to_orchestration_config_yaml(self):
        cfg = dict(self.config)
        cfg.pop("policy_file")
        self.config_path = self.write_json("ecc2.json", cfg)
        os.makedirs(os.path.join(self.repo, ".orchestration"))
        with open(os.path.join(self.repo, ".orchestration", "config.yaml"), "w") as fh:
            json.dump(S.make_policy(), fh)
        doc = self.ok(self.cli("doctor"))
        self.assertTrue(doc["data"]["config"]["policy_file"].endswith(os.path.join(".orchestration", "config.yaml")))
        self.assertIn("policy_valid", [c["name"] for c in doc["checks"] if c["ok"]])

    def test_non_json_policy_yaml_is_reported(self):
        with open(os.path.join(self.tmp, "policy.json"), "w") as fh:
            fh.write("schema_version: '1.0'\n")
        code, doc = self.cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("policy_valid", failed(doc))


class DoctorTest(S.CliTestBase):
    def test_doctor_passes(self):
        doc = self.ok(self.cli("doctor"))
        names = {c["name"] for c in doc["checks"]}
        for name in ("python_version", "git_version", "state_dir_writable", "state_dir_location",
                     "worktree_root_valid", "repo_is_git"):
            self.assertIn(name, names)
        self.assertFalse(doc["changed"])
        self.assertFalse(os.path.exists(self.state), "doctor must not create the state dir")

    def test_doctor_rejects_worktree_root_inside_repo(self):
        code, doc = self.cli("doctor", "--worktree-root", os.path.join(self.repo, "wt"))
        self.assertEqual(code, 1)
        self.assertIn("worktree_root_valid", failed(doc))

    def test_doctor_rejects_openclaw_locations(self):
        claw = os.path.join(self.home, ".openclaw", "worktrees")
        code, doc = self.cli("doctor", "--worktree-root", claw, "--state-dir", os.path.join(self.home, ".openclaw"))
        self.assertEqual(code, 1)
        self.assertIn("worktree_root_valid", failed(doc))
        self.assertIn("state_dir_location", failed(doc))

    def test_doctor_rejects_state_dir_under_a_file(self):
        blocker = os.path.join(self.tmp, "blocker")
        with open(blocker, "w") as fh:
            fh.write("x")
        code, doc = self.cli("doctor", "--state-dir", os.path.join(blocker, "state"))
        self.assertEqual(code, 1)
        self.assertIn("state_dir_writable", failed(doc))

    def test_doctor_rejects_unwritable_state_dir(self):
        locked = os.path.join(self.tmp, "locked")
        os.makedirs(locked)
        os.chmod(locked, 0o500)
        try:
            if os.access(locked, os.W_OK):
                self.skipTest("running with privileges that ignore permissions")
            code, doc = self.cli("doctor", "--state-dir", os.path.join(locked, "state"))
            self.assertEqual(code, 1)
            self.assertIn("state_dir_writable", failed(doc))
        finally:
            os.chmod(locked, 0o700)


class GateTest(S.CliTestBase):
    def create_args(self):
        return ("create-run", "--run-id", self.RUN_ID, "--plan", self.plan_path)

    def test_non_tty_without_yes_refuses(self):
        code, doc = self.cli(*self.create_args())
        self.assertEqual(code, 3)
        self.assertFalse(doc["ok"])
        self.assertFalse(doc["changed"])
        self.assertTrue(doc["required_user_actions"])
        self.assertFalse(os.path.exists(os.path.join(self.state, "runs", self.RUN_ID)))

    def test_tty_prompt_accepts(self):
        doc = self.ok(self.cli(*self.create_args(), tty=True, stdin="y\n"))
        self.assertTrue(doc["changed"])
        self.assertEqual(self.unit("alpha")["state"], "ready")

    def test_tty_prompt_declined(self):
        code, doc = self.cli(*self.create_args(), tty=True, stdin="n\n")
        self.assertEqual(code, 1)
        self.assertIn("operator_confirmed", failed(doc))
        self.assertFalse(os.path.exists(os.path.join(self.state, "runs", self.RUN_ID)))

    def test_review_record_allows_yes(self):
        doc = self.create_run()
        names = [c["name"] for c in doc["checks"] if c["ok"]]
        self.assertIn("review_plan_hash_matches", names)
        self.assertIn("plan_hash_stable", names)

    def test_review_plan_prints_stable_hash_and_writes_nothing(self):
        before = self.snapshot()
        first = self.ok(self.review_plan(*self.create_args(), approve=False))
        second = self.ok(self.review_plan(*self.create_args(), approve=False))
        self.assertEqual(before, self.snapshot())
        self.assertEqual(first["data"]["plan_sha256"], second["data"]["plan_sha256"])
        self.assertEqual(len(first["data"]["plan_sha256"]), 64)
        self.assertEqual(first["data"]["plan"]["operation"], "create-run")
        self.assertFalse(first["data"]["destructive"])

    def test_review_plan_without_end_of_options_marker(self):
        argv = ["review-plan", "--json", "--approve", "--operator", S.OPERATOR, "--session", S.SESSION,
                *self.create_args(), "--config", self.config_path]
        code, out, err = self.run_cli(argv)
        self.assertEqual(code, 0, out + err)
        review_id = json.loads(out)["data"]["review_id"]
        self.ok(self.cli(*self.create_args(), "--yes", "--review-id", review_id))

    def test_review_plan_refuses_read_only_subcommands(self):
        code, out, err = self.run_cli(["review-plan", "--json", "status", "--config", self.config_path])
        self.assertEqual(code, 2)

    def test_review_record_contents(self):
        doc = self.ok(self.review_plan(*self.create_args()))
        path = os.path.join(self.state, "reviews", doc["data"]["review_id"] + ".json")
        with open(path) as fh:
            record = json.load(fh)
        self.assertEqual(record["schema_version"], "1.0")
        self.assertEqual(record["plan_sha256"], doc["data"]["plan_sha256"])
        self.assertEqual(record["approved_by"], S.OPERATOR)
        self.assertEqual(record["session"], S.SESSION)
        self.assertIs(record["destructive"], False)
        self.assertIn("approved_at", record)


class DryRunTest(S.CliTestBase):
    """Every mutating subcommand with --dry-run prints its plan and changes
    neither the state dir, the worktree root nor any git ref."""

    def assert_dry(self, *args):
        before = self.snapshot()
        code, doc = self.cli(*args, "--dry-run")
        self.assertEqual(code, 0, json.dumps(doc, indent=1)[:3000])
        self.assertFalse(doc["changed"])
        self.assertIn("plan", doc["data"])
        self.assertEqual(len(doc["data"]["plan_sha256"]), 64)
        self.assertEqual(before, self.snapshot(), f"dry run changed state: {args}")
        return doc

    def test_dry_run_every_mutating_command(self):
        self.assert_dry("create-run", "--run-id", self.RUN_ID, "--plan", self.plan_path)
        self.create_run()
        self.assert_dry(*self.dispatch_args("alpha", "alpha.txt=a"))
        self.ok(self.dispatch("alpha", "alpha.txt=a"))
        head = self.unit("alpha")["verified_head"]
        self.assert_dry("verify", "--run-id", self.RUN_ID, "--unit", "alpha")
        self.assert_dry(*self.review_args("alpha", head))
        request_id = self.ok(self.reviewed(*self.review_args("alpha", head)))["data"]["approval_request"]["request_id"]
        self.assert_dry(*self.approve_args(request_id))
        self.assert_dry(*self.approve_args(request_id, verb="reject"))
        self.ok(self.reviewed(*self.approve_args(request_id)))
        doc = self.assert_dry("merge", "--run-id", self.RUN_ID, "--unit", "alpha")
        self.assertTrue(doc["data"]["destructive"])
        self.ok(self.cli("merge", "--run-id", self.RUN_ID, "--unit", "alpha"))
        self.assert_dry("cleanup", "--run-id", self.RUN_ID, "--unit", "alpha")
        self.assert_dry("cleanup", "--run-id", self.RUN_ID, "--unit", "alpha", "--backup-dir",
                        os.path.join(self.tmp, "backups"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "backups")))


class StatusAndEventsTest(S.CliTestBase):
    def test_status_lists_runs_and_units(self):
        self.create_run()
        doc = self.ok(self.cli("status"))
        self.assertEqual([r["run_id"] for r in doc["data"]["runs"]], [self.RUN_ID])
        units = {u["unit_id"]: u for u in self.ok(self.cli("status", "--run-id", self.RUN_ID))["data"]["units"]}
        self.assertEqual(units["alpha"]["state"], "ready")
        self.assertEqual(units["beta"]["state"], "pending")
        for key in ("tier", "runner", "attempts", "budget", "used"):
            self.assertIn(key, units["alpha"])

    def test_status_shows_pending_approvals_and_actions(self):
        head, request_id = self.to_review()
        doc = self.ok(self.cli("status", "--run-id", self.RUN_ID))
        alpha = {u["unit_id"]: u for u in doc["data"]["units"]}["alpha"]
        self.assertEqual(alpha["state"], "reviewing")
        self.assertEqual(alpha["runner"], "codex")
        self.assertEqual(alpha["attempts"], 1)
        self.assertEqual(alpha["verified_head"], head)
        self.assertEqual([a["request_id"] for a in doc["data"]["pending_approvals"]], [request_id])
        self.assertEqual([a["kind"] for a in doc["required_user_actions"]], ["approve"])

    def test_status_human_summary(self):
        self.create_run()
        code, out, _ = self.run_cli(["status", "--run-id", self.RUN_ID, "--config", self.config_path])
        self.assertEqual(code, 0)
        self.assertIn("alpha", out)
        self.assertIn("ready", out)
        self.assertNotIn("{", out.splitlines()[0])

    def test_status_unknown_run(self):
        code, doc = self.cli("status", "--run-id", "nope")
        self.assertEqual(code, 1)
        self.assertIn("run_exists", failed(doc))

    def test_events_tail_since_seq_and_follow(self):
        self.create_run()
        events = self.ok(self.cli("events", "tail"))["data"]["events"]
        self.assertEqual(events[0]["type"], "run.created")
        seqs = [e["seq"] for e in events]
        later = self.ok(self.cli("events", "tail", "--since-seq", str(seqs[0])))["data"]["events"]
        self.assertEqual([e["seq"] for e in later], seqs[1:])
        code, out, _ = self.run_cli(["events", "tail", "--follow", "--interval", "0", "--max-polls", "2",
                                     "--since-seq", str(seqs[0]), "--config", self.config_path, "--json"])
        self.assertEqual(code, 0)
        lines = [json.loads(line) for line in out.splitlines() if line.strip()]
        self.assertEqual([e["seq"] for e in lines], seqs[1:])
        code, out, _ = self.run_cli(["events", "tail", "--config", self.config_path])
        self.assertIn("run.created", out)


class DispatchVerifyTest(S.CliTestBase):
    def test_dispatch_reaches_review_and_verify_passes(self):
        self.create_run()
        doc = self.ok(self.dispatch("alpha", "alpha.txt=a"))
        self.assertEqual(doc["data"]["state"], "reviewing")
        self.assertEqual(doc["data"]["plan"]["runner_command"]["source"], "test_override")
        doc = self.ok(self.reviewed("verify", "--run-id", self.RUN_ID, "--unit", "alpha"))
        self.assertFalse(doc["changed"])
        for name in ("branch_matches_verified_head", "gates_passed", "changes_within_scope"):
            self.assertIn(name, [c["name"] for c in doc["checks"] if c["ok"]])
        self.assertEqual(self.unit("alpha")["state"], "reviewing")

    def test_dispatch_uses_registry_command_by_default(self):
        self.create_run()
        doc = self.ok(self.cli("dispatch", "--run-id", self.RUN_ID, "--unit", "alpha", "--dry-run"))
        command = doc["data"]["plan"]["runner_command"]
        self.assertEqual(command["source"], "registry")
        self.assertEqual(command["argv"][0], "codex")
        self.assertIn("--", command["argv"])

    def test_failed_runner_exits_nonzero(self):
        self.create_run()
        code, doc = self.dispatch("alpha", mode="fail")
        self.assertEqual(code, 1)
        self.assertFalse(doc["ok"])
        self.assertEqual(self.unit("alpha")["state"], "failed")

    def test_dispatch_refuses_pending_unit(self):
        self.create_run()
        code, doc = self.cli(*self.dispatch_args("beta", "beta.txt=b"), "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("unit_ready", failed(doc))


class BackupTest(S.CliTestBase):
    def test_backup_dir_copies_state_and_refs(self):
        self.create_run()
        backups = os.path.join(self.tmp, "backups")
        doc = self.ok(self.reviewed(*self.dispatch_args("alpha", "alpha.txt=a"), "--backup-dir", backups))
        backup = doc["rollback_checkpoint"]["backup"]["path"]
        self.assertTrue(backup.startswith(backups + os.sep))
        with open(os.path.join(backup, "manifest.json")) as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest["operation"], "dispatch")
        self.assertIn("refs/heads/main", manifest["refs"])
        with open(os.path.join(backup, "state", "runs", self.RUN_ID, "run.json")) as fh:
            self.assertEqual(json.load(fh)["units"]["alpha"]["state"], "ready")


class RedactionTest(S.CliTestBase):
    SECRET = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"

    def test_printed_strings_are_redacted(self):
        self.create_run()
        self.ok(self.dispatch("alpha", "alpha.txt=a"))
        head = self.unit("alpha")["verified_head"]
        args = list(self.review_args("alpha", head)) + ["--notes", "reviewed with " + self.SECRET]
        code, out, err = self.run_cli(args + ["--dry-run", "--verbose", "--config", self.config_path])
        self.assertEqual(code, 0, out + err)
        self.assertNotIn(self.SECRET, out + err)
        self.assertIn("[REDACTED]", out)
        code, out, err = self.run_cli(args + ["--dry-run", "--json", "--config", self.config_path])
        self.assertNotIn(self.SECRET, out + err)


class DestructiveCleanupTest(S.CliTestBase):
    def failed_unit_with_commits(self):
        self.create_run()
        code, _ = self.dispatch("alpha", "alpha.txt=a", mode="forged")
        self.assertEqual(code, 1)
        self.assertEqual(self.unit("alpha")["state"], "failed")

    def test_cleanup_without_archive_refuses_unique_work(self):
        self.failed_unit_with_commits()
        code, doc = self.cli("cleanup", "--run-id", self.RUN_ID, "--unit", "alpha", "--dry-run")
        self.assertEqual(code, 3)
        self.assertEqual([a["kind"] for a in doc["required_user_actions"]], ["unmerged_work"])

    def test_archiving_cleanup_needs_broker_approval_when_not_on_tty(self):
        self.failed_unit_with_commits()
        args = ("cleanup", "--run-id", self.RUN_ID, "--unit", "alpha", "--archive")
        self.assertTrue(self.cli(*args, "--dry-run")[1]["data"]["destructive"])
        code, doc = self.review_plan(*args)
        self.assertEqual(code, 1)
        self.assertIn("plan_not_destructive", failed(doc))
        code, doc = self.cli(*args)
        self.assertEqual(code, 2, "a cleanup approval request needs --session")
        code, doc = self.cli(*args, "--session", S.SESSION)
        self.assertEqual(code, 3)
        action = doc["required_user_actions"][0]
        self.assertEqual(action["kind"], "approve")
        request_id = action["request_id"]
        self.assertTrue(os.path.isdir(os.path.join(self.worktree_root)))
        self.ok(self.reviewed(*self.approve_args(request_id)))
        doc = self.ok(self.cli(*args, "--session", S.SESSION, "--approval-request", request_id))
        bundle = doc["rollback_checkpoint"]["bundle"]
        self.assertTrue(os.path.isfile(bundle))
        self.assertEqual(S.git(self.repo, "branch", "--list", "ecc/run1/alpha"), "")
        code, doc = self.cli(*args, "--session", S.SESSION, "--approval-request", request_id)
        self.assertNotEqual(code, 0)

    def test_archiving_cleanup_on_tty(self):
        self.failed_unit_with_commits()
        args = ("cleanup", "--run-id", self.RUN_ID, "--unit", "alpha", "--archive")
        code, doc = self.cli(*args, tty=True, stdin="y\n")
        self.assertEqual(code, 1, "destructive plans need the word yes")
        doc = self.ok(self.cli(*args, tty=True, stdin="yes\n"))
        self.assertTrue(os.path.isfile(doc["rollback_checkpoint"]["bundle"]))


class DocsTest(unittest.TestCase):
    def test_cli_doc_punctuation(self):
        path = os.path.join(os.path.dirname(SRC), "docs", "cli.md")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("--", text)
        self.assertNotIn("\u2014", text)
        self.assertNotIn("\u2013", text)
        for name in ("create-run", "dispatch", "review-plan", "doctor", "events tail", "approvals approve"):
            self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main()
