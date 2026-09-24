"""Slice 3: runner readiness probes with injected command runner and HTTP."""

import json
import sys
import threading
import time
import unittest

from openclaw_ecc_orchestrator import schemas
from openclaw_ecc_orchestrator.runners import probes
from openclaw_ecc_orchestrator.runners.base import CommandResult, ProbeContext
from openclaw_ecc_orchestrator.runners.discovery import HttpResponse
from openclaw_ecc_orchestrator.runners.registry import RetiredRunnerError

HANG = object()
NOW = 1_790_000_000.0
LEAKED = "sk-ant-api03-" + "Lk9Jh8Gf7Ds6Aq5Wz4Xc3Vb2Nm1Po0Iu"


def ok(stdout="", stderr=""):
    return CommandResult(0, stdout, stderr)


class FakeCommands:
    """Rules are (substring of the joined argv, result | callable | HANG)."""

    def __init__(self, rules):
        self.rules = rules
        self.calls = []
        self.release = threading.Event()

    def __call__(self, argv, timeout, cwd=None):
        if not isinstance(argv, list):
            raise TypeError("argv must be a list")
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for needle, result in self.rules:
            if needle in joined:
                if result is HANG:
                    self.release.wait(30)
                    return CommandResult(1, "", "released")
                return result(argv) if callable(result) else result
        return CommandResult(127, "", "unexpected command")


CLAUDE_JSON = json.dumps({"type": "result", "result": "OK", "total_cost_usd": 0.0012,
                          "usage": {"input_tokens": 12, "output_tokens": 2},
                          "modelUsage": {"claude-sonnet-x": {"inputTokens": 12}}})


class UnitTestRuns:
    """The probe's own test run: fails before the agent ran, passes after."""

    def __init__(self, results=(1, 0)):
        self.results = list(results)

    def __call__(self, argv):
        code = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        return CommandResult(code, "", "" if code == 0 else "FAILED (failures=1)")


def repo_rules():
    return {
        "failing unit test": None,          # agent invocation, filled per runner
        "git init": ok(),
        "config user": ok(),
        "add -A": ok(),
        "probe: base": ok(),
        "-m unittest": UnitTestRuns(),
        "rev-list --count HEAD": ok("2\n"),
        "status --porcelain": ok(""),
        "diff --quiet": ok(),
    }


CLAUDE_AUTH = json.dumps({"loggedIn": True, "authMethod": "oauth_token",
                          "apiProvider": "firstParty"})


def claude_rules(**overrides):
    rules = repo_rules()
    rules.update({
        "failing unit test": ok(CLAUDE_JSON),
        "claude --version": ok("2.1.0 (Claude Code)\n"),
        "claude auth status": ok(CLAUDE_AUTH),
        "Cancellation probe": CommandResult(-9, "", "", timed_out=True),
        "claude -p": ok(CLAUDE_JSON),
    })
    rules.update(overrides)
    return list(rules.items())


CODEX_JSONL = "\n".join(json.dumps(e) for e in [
    {"type": "thread.started", "thread_id": "t1"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}},
    {"type": "turn.completed", "usage": {"input_tokens": 30, "output_tokens": 3}},
])


def codex_rules(**overrides):
    rules = repo_rules()
    rules.update({
        "failing unit test": ok(CODEX_JSONL),
        "codex --version": ok("codex-cli 1.2.3\n"),
        "codex login status": ok("Logged in using ChatGPT\n"),
        "Cancellation probe": CommandResult(-9, "", "", timed_out=True),
        "codex exec": ok(CODEX_JSONL),
    })
    rules.update(overrides)
    return list(rules.items())


def ctx(commands=None, http_get=None, http_post=None, env=None, model="sonnet",
        policy=None, step_timeout=2.0):
    return ProbeContext(
        run_command=commands or FakeCommands([]),
        http_get=http_get or (lambda url, headers, timeout: HttpResponse(200, '{"data": []}')),
        http_post=http_post or (lambda url, headers, body, timeout: HttpResponse(500, "")),
        env=env if env is not None else {},
        clock=lambda: NOW,
        step_timeout=step_timeout,
        cancel_timeout=0.1,
        make_workdir=lambda: "probe-workdir",
        cleanup_workdir=lambda path: None,
        write_file=lambda path, content: None,
        model=model,
        policy=policy or {},
    )


def statuses(record):
    return {c["name"]: c["status"] for c in record["checks"]}


def reasons(record):
    return {c["name"]: c["reason"] for c in record["checks"]}


class ClaudeProbeTests(unittest.TestCase):
    def test_certified_with_fakes(self):
        record = probes.probe_runner("claude", ctx(FakeCommands(claude_rules())))
        self.assertEqual(record["status"], "certified", record)
        self.assertTrue(schemas.validate_certification_record(record).ok)
        self.assertEqual(record["version"], "2.1.0 (Claude Code)")
        self.assertIn("claude-sonnet-x", record["models"])
        self.assertAlmostEqual(record["usage"]["cost_usd"], 0.0012)
        self.assertTrue(all(s == "pass" for s in statuses(record).values()))

    def test_hung_cli_yields_timeout_not_hang(self):
        commands = FakeCommands(claude_rules(**{"claude -p": HANG, "failing unit test": HANG,
                                                "Cancellation probe": HANG}))
        start = time.monotonic()
        try:
            record = probes.probe_runner("claude", ctx(commands, step_timeout=0.2))
        finally:
            commands.release.set()
        self.assertLess(time.monotonic() - start, 5.0)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(statuses(record)["live_inference"], "fail")
        self.assertEqual(reasons(record)["live_inference"], "timeout")
        self.assertEqual(reasons(record)["cancellation_timeout"], "timeout")
        self.assertEqual(statuses(record)["installed"], "pass")
        self.assertTrue(schemas.validate_certification_record(record).ok)

    def test_hung_version_fails_everything_quickly(self):
        commands = FakeCommands([("claude", HANG)])
        start = time.monotonic()
        try:
            record = probes.probe_runner("claude", ctx(commands, step_timeout=0.2))
        finally:
            commands.release.set()
        self.assertLess(time.monotonic() - start, 3.0)
        self.assertEqual(reasons(record)["installed"], "timeout")
        self.assertEqual(record["status"], "failed")
        self.assertTrue(reasons(record)["authentication"].startswith("prerequisite failed"))

    def test_not_installed(self):
        commands = FakeCommands([("claude --version", CommandResult(127, "", "not found"))])
        record = probes.probe_runner("claude", ctx(commands))
        self.assertEqual(statuses(record)["installed"], "fail")
        self.assertEqual(record["status"], "failed")

    def test_secret_in_output_fails_log_inspection_and_is_redacted(self):
        leaky = json.dumps({"type": "result", "result": "key is " + LEAKED,
                            "total_cost_usd": 0.001, "usage": {"input_tokens": 1,
                                                               "output_tokens": 1},
                            "modelUsage": {"m": {}}})
        commands = FakeCommands(claude_rules(**{"claude -p": ok(leaky)}))
        record = probes.probe_runner("claude", ctx(commands))
        self.assertEqual(statuses(record)["log_inspection"], "fail")
        self.assertEqual(record["status"], "failed")
        self.assertNotIn(LEAKED, json.dumps(record))

    def test_missing_metadata_fails_metadata_capture(self):
        bare = json.dumps({"type": "result", "result": "OK"})
        commands = FakeCommands(claude_rules(**{"claude -p": ok(bare),
                                                "failing unit test": ok(bare)}))
        record = probes.probe_runner("claude", ctx(commands, model=None))
        self.assertEqual(statuses(record)["metadata_capture"], "fail")
        self.assertNotEqual(record["status"], "certified")

    def test_repo_exercise_failure(self):
        commands = FakeCommands(claude_rules(**{"-m unittest": UnitTestRuns((1, 1))}))
        record = probes.probe_runner("claude", ctx(commands))
        self.assertEqual(statuses(record)["repo_exercise"], "fail")
        self.assertEqual(record["status"], "failed")

    def test_invocation_is_argument_list(self):
        adapter = probes.get_adapter("claude")
        argv = adapter.build_invocation("do x; rm -rf / && echo $HOME", model="opus")
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[0], "claude")
        self.assertIn("-p", argv)
        self.assertIn("do x; rm -rf / && echo $HOME", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")


class CodexProbeTests(unittest.TestCase):
    def test_certified_with_fakes(self):
        record = probes.probe_runner("codex", ctx(FakeCommands(codex_rules()), model="std"))
        self.assertEqual(record["status"], "certified", record)
        self.assertEqual(record["usage"]["input_tokens"], 30)
        self.assertTrue(schemas.validate_certification_record(record).ok)

    def test_invocation_is_argument_list(self):
        adapter = probes.get_adapter("codex")
        argv = adapter.build_invocation("fix `tests` | tee", model="std", cwd="repo",
                                        writable=True)
        self.assertEqual(argv[:2], ["codex", "exec"])
        self.assertIn("--json", argv)
        self.assertEqual(argv[-1], "fix `tests` | tee")
        self.assertEqual(argv[argv.index("-C") + 1], "repo")
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
        ro = adapter.build_invocation("x", model=None)
        self.assertEqual(ro[ro.index("--sandbox") + 1], "read-only")
        self.assertNotIn("-m", ro)

    def test_auth_failure(self):
        commands = FakeCommands(codex_rules(**{"codex login status":
                                               CommandResult(1, "", "Not logged in")}))
        record = probes.probe_runner("codex", ctx(commands, model="std"))
        self.assertEqual(statuses(record)["authentication"], "fail")
        self.assertEqual(record["status"], "failed")


def chat_ok(url, headers, body, timeout):
    return HttpResponse(200, json.dumps({
        "model": body["model"], "choices": [{"message": {"content": "OK"}}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 1}}))


GROQ_ENV = {"GROQ_API_KEY": "gsk_" + "Tq1Wr2Ey3Ut4Io5Pa6Sd7Fg8Hj9Kl0Zx"}


class ApiProbeTests(unittest.TestCase):
    def test_groq_certified(self):
        record = probes.probe_runner("groq", ctx(http_post=chat_ok, env=GROQ_ENV,
                                                 model="open-small-9b-instant"))
        self.assertEqual(record["status"], "certified", record)
        self.assertEqual(statuses(record)["repo_exercise"], "skip")
        self.assertEqual(record["models"], ["open-small-9b-instant"])
        self.assertNotIn(GROQ_ENV["GROQ_API_KEY"], json.dumps(record))
        self.assertTrue(schemas.validate_certification_record(record).ok)

    def test_groq_missing_key_not_configured(self):
        record = probes.probe_runner("groq", ctx(http_post=chat_ok, env={}, model="m"))
        self.assertEqual(record["status"], "not_configured")

    def test_kimi_absent_credentials_is_not_configured_not_error(self):
        record = probes.probe_runner("kimi", ctx(env={}, model=None))
        self.assertEqual(record["status"], "not_configured")
        self.assertTrue(schemas.validate_certification_record(record).ok)

    def test_auth_rejected(self):
        record = probes.probe_runner("groq", ctx(
            http_get=lambda u, h, t: HttpResponse(401, ""), http_post=chat_ok, env=GROQ_ENV,
            model="m"))
        self.assertEqual(statuses(record)["authentication"], "fail")
        self.assertEqual(statuses(record)["live_inference"], "fail")
        self.assertEqual(record["status"], "failed")

    def test_no_model_resolved(self):
        record = probes.probe_runner("groq", ctx(http_post=chat_ok, env=GROQ_ENV, model=None))
        self.assertEqual(statuses(record)["live_inference"], "fail")

    def test_hung_http_times_out(self):
        release = threading.Event()

        def hang(url, headers, body, timeout):
            release.wait(30)
            return HttpResponse(500, "")
        try:
            start = time.monotonic()
            record = probes.probe_runner("groq", ctx(http_post=hang, env=GROQ_ENV, model="m",
                                                     step_timeout=0.2))
        finally:
            release.set()
        self.assertLess(time.monotonic() - start, 3.0)
        self.assertEqual(reasons(record)["live_inference"], "timeout")

    def test_http_exception_redacted(self):
        def boom(url, headers, body, timeout):
            raise RuntimeError("bad auth " + headers.get("Authorization", ""))
        record = probes.probe_runner("groq", ctx(http_post=boom, env=GROQ_ENV, model="m"))
        self.assertEqual(statuses(record)["live_inference"], "fail")
        self.assertNotIn(GROQ_ENV["GROQ_API_KEY"], json.dumps(record))

    def test_gemini_certified(self):
        seen = {}

        def post(url, headers, body, timeout):
            seen["url"], seen["headers"] = url, headers
            return HttpResponse(200, json.dumps({
                "candidates": [{"content": {"parts": [{"text": "OK"}]}}],
                "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 1},
                "modelVersion": "gemini-flash-next"}))
        env = {"GEMINI_API_KEY": "AIza" + "Sy0123456789abcdefghijABCDEFGHIJ"}
        record = probes.probe_runner("gemini", ctx(http_post=post, env=env,
                                                   model="gemini-flash-next"))
        self.assertEqual(record["status"], "certified", record)
        self.assertNotIn(env["GEMINI_API_KEY"], seen["url"])
        self.assertEqual(seen["headers"]["x-goog-api-key"], env["GEMINI_API_KEY"])

    def test_openrouter_requires_approved_models_and_data_policy(self):
        env = {"OPENROUTER_API_KEY": "sk-or-v1-" + "ab" * 16}
        record = probes.probe_runner("openrouter", ctx(http_post=chat_ok, env=env,
                                                       model="vendor-a/coder-large"))
        self.assertEqual(record["status"], "not_configured")
        pol = {"openrouter": {"approved_models": ["vendor-b/reviewer-mid"],
                              "data_policy": "no-training"}}
        record = probes.probe_runner("openrouter", ctx(http_post=chat_ok, env=env, policy=pol,
                                                       model="vendor-a/coder-large"))
        self.assertEqual(record["status"], "not_configured")
        record = probes.probe_runner("openrouter", ctx(http_post=chat_ok, env=env, policy=pol,
                                                       model="vendor-b/reviewer-mid"))
        self.assertEqual(record["status"], "certified", record)


class RetiredAndDefaultsTests(unittest.TestCase):
    def test_retired_runners_rejected(self):
        for name in ("windsurf", "pi", "openai-api", "Windsurf"):
            with self.subTest(name=name):
                with self.assertRaises(RetiredRunnerError):
                    probes.get_adapter(name)
                with self.assertRaises(RetiredRunnerError):
                    probes.probe_runner(name, ctx())

    def test_unknown_runner(self):
        with self.assertRaises(KeyError):
            probes.get_adapter("mystery")

    def test_default_command_runner_enforces_timeout(self):
        start = time.monotonic()
        result = probes.default_command_runner(
            [sys.executable, "-c", "import time; time.sleep(10)"], 0.3)
        self.assertTrue(result.timed_out)
        self.assertLess(time.monotonic() - start, 5.0)

    def test_default_command_runner_rejects_shell_strings(self):
        with self.assertRaises(TypeError):
            probes.default_command_runner("echo hi", 1.0)

    def test_default_command_runner_missing_binary(self):
        result = probes.default_command_runner(["definitely-not-a-real-binary-xyz"], 1.0)
        self.assertEqual(result.returncode, 127)


if __name__ == "__main__":
    unittest.main()
