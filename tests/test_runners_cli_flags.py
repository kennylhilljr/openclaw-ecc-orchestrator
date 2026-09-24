"""Phase 3: Claude and Codex adapters against the flags and output shapes of the
installed CLIs (Claude Code 2.1.281, codex-cli 0.158.0-alpha.7), the
disposable repo exercise, and process-group cancellation."""

import json
import os
import sys
import time
import unittest

from openclaw_ecc_orchestrator.runners import probes
from openclaw_ecc_orchestrator.runners.base import (CancelResult, CommandResult, ProbeContext,
                                                    default_cancel_runner)
from openclaw_ecc_orchestrator.runners.cli_adapters import (CANCEL_PROMPT, EXERCISE_FILES,
                                                            ClaudeAdapter, CodexAdapter)
from tests.test_runners_probes import (CLAUDE_JSON, CODEX_JSONL, FakeCommands, UnitTestRuns,
                                       claude_rules, codex_rules, ctx, ok, reasons, statuses)

PY = sys.executable

CREDIT_ERROR = json.dumps({"type": "result", "subtype": "success", "is_error": True,
                           "result": "Credit balance is too low", "api_error_status": 400,
                           "total_cost_usd": 0, "usage": {"input_tokens": 0, "output_tokens": 0},
                           "modelUsage": {}})
CODEX_LIMIT = "\n".join(json.dumps(e) for e in [
    {"type": "thread.started", "thread_id": "t1"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"type": "error", "message": "Exceeded skills context "
                                        "budget."}},
    {"type": "error", "message": "You've hit your usage limit."},
    {"type": "turn.failed", "error": {"message": "You've hit your usage limit."}},
])


class ClaudeFlagTests(unittest.TestCase):
    def test_print_mode_never_waits_for_permission_prompts(self):
        argv = ClaudeAdapter(effort="low").build_invocation("-rf prompt", model="haiku")
        self.assertEqual(argv[:4], ["claude", "-p", "--output-format", "json"])
        self.assertIn("--no-session-persistence", argv)
        self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
        self.assertEqual(argv[argv.index("--model") + 1], "haiku")
        self.assertEqual(argv[argv.index("--effort") + 1], "low")
        self.assertEqual(argv[-2:], ["--", "-rf prompt"])

    def test_writable_allows_edit_git_and_python_only(self):
        argv = ClaudeAdapter().build_invocation("x", writable=True)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        tools = argv[argv.index("--allowedTools") + 1]
        self.assertIn("Bash(git *)", tools)
        self.assertIn("Bash(python3 *)", tools)
        self.assertNotIn("bypassPermissions", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertLess(argv.index("--allowedTools"), argv.index("--"))

    def test_cancel_invocation_allows_only_sleep(self):
        argv = ClaudeAdapter().build_cancel_invocation("haiku")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Bash(sleep *)")
        self.assertEqual(argv[-1], CANCEL_PROMPT)

    def test_auth_status_is_json_and_checks_logged_in(self):
        self.assertEqual(ClaudeAdapter.auth_argv, ["claude", "auth", "status", "--json"])
        adapter = ClaudeAdapter()
        good = json.dumps({"loggedIn": True, "authMethod": "oauth_token",
                           "apiProvider": "firstParty", "email": "someone@example.com",
                           "orgId": "org-123"})
        logged_in, detail = adapter.parse_auth(CommandResult(0, good, ""))
        self.assertTrue(logged_in)
        self.assertEqual(detail, "login via oauth_token, provider firstParty")
        self.assertNotIn("example.com", detail)
        self.assertNotIn("org-123", detail)
        self.assertFalse(adapter.parse_auth(CommandResult(0, '{"loggedIn": false}', ""))[0])
        self.assertFalse(adapter.parse_auth(CommandResult(0, "Logged in", ""))[0])

    def test_logged_out_fails_authentication(self):
        commands = FakeCommands(claude_rules(**{"claude auth status": ok('{"loggedIn": false}')}))
        record = probes.probe_runner("claude", ctx(commands))
        self.assertEqual(statuses(record)["authentication"], "fail")
        self.assertEqual(reasons(record)["authentication"], "not logged in")

    def test_is_error_result_fails_inference_even_with_exit_zero(self):
        for code in (0, 1):
            with self.subTest(exit=code):
                commands = FakeCommands(claude_rules(**{
                    "claude -p": CommandResult(code, CREDIT_ERROR, "")}))
                record = probes.probe_runner("claude", ctx(commands))
                self.assertEqual(statuses(record)["live_inference"], "fail")
                self.assertIn("Credit balance is too low (HTTP 400)",
                              reasons(record)["live_inference"])
                self.assertEqual(record["status"], "failed")

    def test_parse_success(self):
        parsed = ClaudeAdapter().parse_output(CLAUDE_JSON)
        self.assertEqual(parsed["text"], "OK")
        self.assertIsNone(parsed["error"])
        self.assertEqual(parsed["model"], "claude-sonnet-x")


class CodexFlagTests(unittest.TestCase):
    def test_exec_flags(self):
        argv = CodexAdapter(effort="low").build_invocation("--x", model="m-luna", cwd="/w")
        self.assertEqual(argv[:3], ["codex", "exec", "--json"])
        self.assertIn("--ephemeral", argv)
        self.assertEqual(argv[argv.index("-C") + 1], "/w")
        self.assertEqual(argv[argv.index("-m") + 1], "m-luna")
        self.assertEqual(argv[argv.index("-c") + 1], 'model_reasoning_effort="low"')
        self.assertEqual(argv[-2:], ["--", "--x"])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertEqual(CodexAdapter.models_argv, ["codex", "debug", "models"])
        self.assertNotIn("--add-dir", argv)

    def test_writable_run_can_commit_in_its_repo_only(self):
        argv = CodexAdapter().build_invocation("x", cwd="/w/repo", writable=True)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(argv.count("--add-dir"), 1)
        self.assertEqual(argv[argv.index("--add-dir") + 1], os.path.join("/w/repo", ".git"))
        self.assertNotIn("--add-dir", CodexAdapter().build_invocation("x", writable=True))

    def test_auth_parse(self):
        adapter = CodexAdapter()
        self.assertTrue(adapter.parse_auth(CommandResult(0, "", "Logged in using ChatGPT\n"))[0])
        self.assertFalse(adapter.parse_auth(CommandResult(0, "Not logged in\n", ""))[0])

    def test_turn_failed_is_an_error_not_text(self):
        parsed = CodexAdapter().parse_output(CODEX_LIMIT)
        self.assertEqual(parsed["text"], "")
        self.assertIn("usage limit", parsed["error"])
        commands = FakeCommands(codex_rules(**{"codex exec": CommandResult(1, CODEX_LIMIT, "")}))
        record = probes.probe_runner("codex", ctx(commands, model="m"))
        self.assertIn("usage limit", reasons(record)["live_inference"])

    def test_skills_budget_item_alone_is_not_fatal(self):
        stream = CODEX_JSONL + "\n" + json.dumps(
            {"type": "item.completed", "item": {"type": "error", "message": "budget"}})
        parsed = CodexAdapter().parse_output(stream)
        self.assertEqual(parsed["text"], "OK")
        self.assertIsNone(parsed["error"])


class RepoExerciseTests(unittest.TestCase):
    def run_claude(self, **overrides):
        return probes.probe_runner("claude", ctx(FakeCommands(claude_rules(**overrides))))

    def test_exercise_writes_failing_project_and_verifies_independently(self):
        written = {}
        commands = FakeCommands(claude_rules())
        c = ctx(commands)
        c.write_file = lambda path, content: written.__setitem__(os.path.basename(path), content)
        record = probes.probe_runner("claude", c)
        self.assertEqual(statuses(record)["repo_exercise"], "pass", record)
        self.assertEqual(set(written), set(EXERCISE_FILES))
        joined = [" ".join(a) for a in commands.calls]
        agent = [i for i, a in enumerate(joined) if "failing unit test" in a]
        tests = [i for i, a in enumerate(joined) if a.startswith("python3 -B -m unittest")]
        self.assertEqual(len(agent), 1)
        self.assertEqual(len(tests), 2)
        self.assertTrue(tests[0] < agent[0] < tests[1])

    def test_agent_claims_success_but_did_not_commit(self):
        record = self.run_claude(**{"rev-list --count HEAD": ok("1\n")})
        self.assertEqual(reasons(record)["repo_exercise"], "agent did not commit its fix")

    def test_uncommitted_fix(self):
        record = self.run_claude(**{"status --porcelain": ok(" M calc.py\n")})
        self.assertEqual(reasons(record)["repo_exercise"], "fix is not fully committed")

    def test_test_file_modified(self):
        record = self.run_claude(**{"diff --quiet": CommandResult(1, "", "")})
        self.assertEqual(reasons(record)["repo_exercise"], "agent modified the test file")

    def test_tests_still_failing(self):
        record = self.run_claude(**{"-m unittest": UnitTestRuns((1, 1))})
        self.assertEqual(reasons(record)["repo_exercise"], "tests still fail after the agent run")

    def test_setup_that_already_passes_is_an_error(self):
        record = self.run_claude(**{"-m unittest": UnitTestRuns((0, 0))})
        self.assertIn("already passes", reasons(record)["repo_exercise"])

    def test_real_exercise_files_fail_then_pass_when_fixed(self):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for rel, content in EXERCISE_FILES.items():
                with open(os.path.join(tmp, rel), "w") as fh:
                    fh.write(content)
            run = lambda: subprocess.run([PY, "-B", "-m", "unittest", "-q"], cwd=tmp,
                                         capture_output=True).returncode
            self.assertNotEqual(run(), 0)
            with open(os.path.join(tmp, "calc.py"), "w") as fh:
                fh.write("def add(a, b):\n    return a + b\n")
            self.assertEqual(run(), 0)


def cancel_ctx(result, **kw):
    c = ctx(FakeCommands(claude_rules()), **kw)
    c.cancel_command = lambda argv, after, cwd=None: result
    return c


class CancellationTests(unittest.TestCase):
    def test_group_gone_passes(self):
        record = probes.probe_runner("claude", cancel_ctx(
            CancelResult(True, False, -9, True, 3, 5.0)))
        self.assertEqual(statuses(record)["cancellation_timeout"], "pass")
        self.assertIn("3 process(es)", [c for c in record["checks"]
                                        if c["name"] == "cancellation_timeout"][0]["detail"])

    def test_early_exit_fails_with_cli_error(self):
        record = probes.probe_runner("claude", cancel_ctx(
            CancelResult(True, True, 1, True, 0, 2.0, CREDIT_ERROR, "")))
        self.assertEqual(statuses(record)["cancellation_timeout"], "fail")
        self.assertIn("exited before the cancel point", reasons(record)["cancellation_timeout"])

    def test_surviving_group_fails(self):
        record = probes.probe_runner("claude", cancel_ctx(
            CancelResult(True, False, -9, False, 2, 5.0)))
        self.assertEqual(reasons(record)["cancellation_timeout"],
                         "process group survived cancellation")

    def test_injected_runner_early_exit_fails(self):
        commands = FakeCommands(claude_rules(**{"Cancellation probe": ok(CLAUDE_JSON)}))
        record = probes.probe_runner("claude", ctx(commands))
        self.assertEqual(statuses(record)["cancellation_timeout"], "fail")


class DefaultCancelRunnerTests(unittest.TestCase):
    def test_kills_whole_group_including_grandchild(self):
        code = ("import subprocess, sys, time; "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                "time.sleep(60)")
        start = time.monotonic()
        res = default_cancel_runner([PY, "-c", code], 0.8)
        self.assertLess(time.monotonic() - start, 15)
        self.assertTrue(res.started)
        self.assertFalse(res.exited_early)
        self.assertTrue(res.group_gone)
        self.assertGreaterEqual(res.members_before, 2)

    def test_early_exit_reported(self):
        res = default_cancel_runner([PY, "-c", "print('hi')"], 5.0)
        self.assertTrue(res.exited_early)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "hi")

    def test_rejects_shell_string_and_missing_binary(self):
        with self.assertRaises(TypeError):
            default_cancel_runner("sleep 1", 1.0)
        self.assertFalse(default_cancel_runner(["no-such-binary-xyz"], 1.0).started)

    def test_minimal_env(self):
        os.environ["CANCEL_TEST_SECRET"] = "x"
        try:
            res = default_cancel_runner([PY, "-c", "import os; print('CANCEL_TEST_SECRET' in "
                                              "os.environ)"], 5.0)
        finally:
            del os.environ["CANCEL_TEST_SECRET"]
        self.assertEqual(res.stdout.strip(), "False")

    def test_probe_uses_group_runner_with_default_command_runner(self):
        c = ProbeContext(env={"PATH": os.environ.get("PATH", ""), "HOME": "/nonexistent"},
                         step_timeout=5.0, cancel_timeout=0.5)
        isolated = probes._isolated_context(ClaudeAdapter(), c)
        self.assertIsNotNone(isolated.cancel_command)
        self.assertEqual(isolated.cancel_command.keywords["env"]["HOME"], "/nonexistent")


class HooksTests(unittest.TestCase):
    def test_on_step_and_transcript(self):
        events, transcript = [], []
        c = ctx(FakeCommands(claude_rules()))
        c.on_step = lambda check, phase: events.append((check, phase))
        c.transcript = transcript
        probes.probe_runner("claude", c)
        self.assertIn(("live_inference", "start"), events)
        self.assertIn(("live_inference", "pass"), events)
        self.assertTrue(any('"result": "OK"' in t for t in transcript))

    def test_failing_hook_does_not_break_probe(self):
        c = ctx(FakeCommands(claude_rules()))
        c.on_step = lambda check, phase: 1 / 0
        self.assertEqual(probes.probe_runner("claude", c)["status"], "certified")


class OpenRouterPolicyTests(unittest.TestCase):
    def test_policy_not_approved_even_with_key(self):
        env = {"OPENROUTER_API_KEY": "sk-or-v1-" + "cd" * 16}
        for e in (env, {}):
            with self.subTest(key=bool(e)):
                record = probes.probe_runner("openrouter", ctx(env=e, model=None))
                self.assertEqual(record["status"], "not_configured")
                self.assertTrue(reasons(record)["live_inference"].startswith(
                    "policy_not_approved"))


if __name__ == "__main__":
    unittest.main()
