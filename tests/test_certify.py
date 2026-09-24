"""Phase 3 certification entry point (runners/certify.py) with fakes."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from openclaw_ecc_orchestrator.runners import certify
from openclaw_ecc_orchestrator.runners.base import CancelResult, CommandResult
from openclaw_ecc_orchestrator.runners.discovery import HttpResponse
from openclaw_ecc_orchestrator.runners.registry import RetiredRunnerError
from tests.test_runners_probes import (CLAUDE_JSON, CODEX_JSONL, FakeCommands, claude_rules,
                                       codex_rules, ok)

NOW = 1_790_000_000.0
GROQ_KEY = "gsk_" + "Qa1Ws2Ed3Rf4Tg5Yh6Uj7Ik8Ol9Pz0Xc"
CODEX_CATALOG = json.dumps({"models": [
    {"slug": "gpt-9-astra", "visibility": "list", "description": "Frontier"},
    {"slug": "gpt-9-luna", "visibility": "list", "description": "Fast and affordable"},
    {"slug": "gpt-8.5-luna", "visibility": "list", "description": "Older fast"},
    {"slug": "gpt-hidden-luna", "visibility": "hide", "description": "hidden"},
]})


def groq_catalog(url, headers, timeout):
    if "groq" in url:
        return HttpResponse(200, json.dumps({"data": [
            {"id": "open-large-70b-versatile"},
            {"id": "open-small-9b-instant"},
            {"id": "open-small-8b-instant-legacy", "active": False},
        ]}))
    if "openrouter" in url:
        assert "Authorization" not in headers
        return HttpResponse(200, json.dumps({"data": [{"id": "a/b"}, {"id": "c/d"}]}))
    return HttpResponse(404, "")


def chat_ok(url, headers, body, timeout):
    return HttpResponse(200, json.dumps({
        "model": body["model"], "choices": [{"message": {"content": "OK"}}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 1}}))


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "out")
        self.work = os.path.join(self.tmp.name, "work")
        os.mkdir(self.work)
        self.logs = []

    def deps(self, commands=None, env=None, cancel=None):
        return certify.Deps(
            run_command=commands or FakeCommands([]), http_get=groq_catalog, http_post=chat_ok,
            cancel_command=cancel, env=env if env is not None else {"HOME": "/Users/probe"},
            clock=lambda: NOW, log=self.logs.append, write_file=lambda p, c: None)

    def opts(self, runners, **kw):
        return certify.Options(runners=tuple(runners), out=self.out, work_root=self.work,
                               step_timeout=2.0, cancel_after=0.1, **kw)


class ModelSelectionTests(unittest.TestCase):
    def test_codex_picks_listed_economical_model_from_catalog(self):
        model, info = certify.select_codex_model(CODEX_CATALOG, certify.DEFAULT_CODEX_PREFERENCES)
        self.assertEqual(model, "gpt-9-luna")
        self.assertEqual(info["listed_models"], 3)
        self.assertEqual(info["description"], "Fast and affordable")

    def test_codex_no_match_is_none(self):
        self.assertEqual(certify.select_codex_model(CODEX_CATALOG, ["zzz-*"])[0], None)
        self.assertEqual(certify.select_codex_model("not json", ["*"])[0], None)

    def test_claude_model_sources(self):
        self.assertEqual(certify.claude_model({}, None), ("haiku", "default economical alias"))
        pol = {"cli_models": {"claude": {"economical": "cheap-alias"}}}
        self.assertEqual(certify.claude_model(pol, None)[0], "cheap-alias")
        placeholder = {"cli_models": {"claude": {"economical": "REPLACE_WITH_ALIAS"}}}
        self.assertEqual(certify.claude_model(placeholder, None)[0], "haiku")
        self.assertEqual(certify.claude_model(pol, "x")[0], "x")


class RunTests(Harness):
    def test_groq_uses_live_listed_model_not_hardcoded(self):
        env = {"HOME": "/Users/probe", "GROQ_API_KEY": GROQ_KEY}
        summary = certify.run(self.opts(["groq"]), self.deps(env=env))
        result = summary["results"][0]
        self.assertEqual(result["model_selection"]["model"], "open-small-9b-instant")
        self.assertEqual(result["model_selection"]["source"], "live catalog")
        self.assertEqual(result["record"]["status"], "certified", result)
        text = read(os.path.join(self.out, "summary.json"))
        self.assertNotIn(GROQ_KEY, text)
        self.assertNotIn(GROQ_KEY, read(os.path.join(self.out, "groq.transcript.log")))

    def test_missing_keys_not_configured(self):
        summary = certify.run(self.opts(["gemini", "groq", "kimi"]), self.deps())
        for result in summary["results"]:
            self.assertEqual(result["record"]["status"], "not_configured")
            self.assertEqual(result["model_selection"]["catalog_status"], "not_configured")
        self.assertEqual(summary["certified"], [])
        self.assertFalse(summary["gate"]["gate_met"])

    def test_openrouter_policy_not_approved_with_key_and_catalog_reachability(self):
        env = {"HOME": "/Users/probe", "OPENROUTER_API_KEY": "sk-or-v1-" + "ef" * 16}
        summary = certify.run(self.opts(["openrouter"]), self.deps(env=env))
        result = summary["results"][0]
        self.assertEqual(result["record"]["status"], "not_configured")
        reasons = {c["reason"] for c in result["record"]["checks"]}
        self.assertTrue(all(r.startswith("policy_not_approved") for r in reasons))
        self.assertEqual(result["model_selection"]["catalog"],
                         {"reachable": True, "http_status": 200, "model_count": 2})

    def test_claude_and_codex_certified_with_fakes(self):
        cycle = iter([1, 0] * 4)
        tests = ("-m unittest", lambda argv: CommandResult(next(cycle), "", ""))
        rules = [r for r in codex_rules() + claude_rules() if r[0] != "-m unittest"]
        commands = FakeCommands([rules[0], tests] + rules[1:] + [
            ("codex debug models", ok(CODEX_CATALOG))])
        cancel = lambda argv, after, cwd=None: CancelResult(True, False, -9, True, 2, 0.1)
        summary = certify.run(self.opts(["claude", "codex"]), self.deps(commands, cancel=cancel))
        by = {r["runner"]: r for r in summary["results"]}
        self.assertEqual(by["claude"]["record"]["status"], "certified", by["claude"])
        self.assertEqual(by["codex"]["record"]["status"], "certified", by["codex"])
        self.assertEqual(by["codex"]["model_selection"]["model"], "gpt-9-luna")
        self.assertEqual(by["claude"]["model_selection"]["model"], "haiku")
        codex_calls = [a for a in commands.calls if a[:2] == ["codex", "exec"]]
        self.assertTrue(codex_calls)
        for argv in codex_calls:
            self.assertEqual(argv[argv.index("-m") + 1], "gpt-9-luna")
            self.assertIn('model_reasoning_effort="low"', argv)
        self.assertTrue(summary["gate"]["coding_runners_certified"])
        self.assertTrue(any("[claude] live_inference: start" == line for line in self.logs))

    def test_home_and_email_sanitized(self):
        rules = claude_rules(**{"claude -p": CommandResult(
            1, "", "failed for someone@example.com at /Users/probe/.claude")})
        certify.run(self.opts(["claude"]), self.deps(FakeCommands(rules)))
        text = read(os.path.join(self.out, "claude.json"))
        self.assertNotIn("someone@example.com", text)
        self.assertNotIn("/Users/probe", text)

    def test_retired_runner_refused(self):
        with self.assertRaises(RetiredRunnerError):
            certify.run(self.opts(["claude", "windsurf"]), self.deps())
        with self.assertRaises(RetiredRunnerError):
            certify.run(self.opts(["openai-api"]), self.deps())

    def test_workdirs_are_created_and_removed_under_work_root(self):
        opts = certify.Options(runners=("claude",), out=self.out, work_root=self.work,
                               step_timeout=2.0, cancel_after=0.1)
        deps = self.deps(FakeCommands(claude_rules()))
        deps.write_file = None
        seen = []
        orig = certify.certify_runner

        def spy(name, o, d, root):
            real_write = __import__("openclaw_ecc_orchestrator.runners.base",
                                    fromlist=["x"]).default_write_file
            d.write_file = lambda p, c: (seen.append(p), real_write(p, c))
            return orig(name, o, d, root)
        certify.certify_runner = spy
        try:
            certify.run(opts, deps)
        finally:
            certify.certify_runner = orig
        self.assertTrue(seen)
        self.assertTrue(all(p.startswith(self.work + os.sep) for p in seen))
        self.assertEqual(os.listdir(self.work), [])


class DryRunTests(Harness):
    def test_dry_run_executes_nothing(self):
        commands = FakeCommands([])

        def no_http(*a, **k):
            raise AssertionError("dry run must not call HTTP")
        deps = self.deps(commands)
        deps.http_get = no_http
        env = {"HOME": "/Users/probe", "PATH": "/bin", "GROQ_API_KEY": GROQ_KEY,
               "UNRELATED_SECRET": "x"}
        deps.env = env
        summary = certify.run(self.opts(list(certify.DEFAULT_RUNNERS), dry_run=True), deps)
        self.assertEqual(commands.calls, [])
        plans = {p["runner"]: p for p in summary["plans"]}
        self.assertEqual(plans["claude"]["env_names"], ["HOME", "PATH"])
        self.assertIn("--permission-prompts", plans["claude"]["inference_argv"])
        self.assertNotIn("--effort", plans["claude"]["inference_argv"])
        self.assertIn('model_reasoning_effort="low"', plans["codex"]["inference_argv"])
        self.assertTrue(plans["groq"]["key_present"])
        self.assertFalse(plans["openrouter"]["policy_approved"])
        text = read(os.path.join(self.out, "plan.json"))
        self.assertNotIn(GROQ_KEY, text)
        self.assertNotIn("UNRELATED_SECRET", text)

    def test_main_dry_run_and_bounds(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(certify.main(["--runners", "kimi", "--out", self.out,
                                           "--dry-run"]), 0)
        self.assertIn('"kimi"', buf.getvalue())
        with self.assertRaises(SystemExit):
            certify.main(["--out", self.out, "--dry-run", "--step-timeout", "600"])


class WorkRootTests(unittest.TestCase):
    def test_refuses_inside_git_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, ".git"))
            with self.assertRaises(ValueError):
                certify.make_work_root(base=tmp)
            self.assertEqual(sorted(os.listdir(tmp)), [".git"])

    def test_refuses_inside_openclaw(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, ".openclaw"))
            with self.assertRaises(ValueError):
                certify.make_work_root(base=os.path.join(tmp, ".openclaw"), env={"HOME": tmp})

    def test_fresh_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = certify.make_work_root(base=tmp, env={"HOME": "/nonexistent"})
            self.assertTrue(os.path.isdir(root))


class GateTests(unittest.TestCase):
    def test_gate(self):
        recs = [{"runner": "claude", "status": "certified"},
                {"runner": "codex", "status": "certified"},
                {"runner": "gemini", "status": "certified"},
                {"runner": "groq", "status": "failed"}]
        self.assertFalse(certify.evaluate_gate(recs)["gate_met"])
        recs[3]["status"] = "certified"
        self.assertTrue(certify.evaluate_gate(recs)["gate_met"])


if __name__ == "__main__":
    unittest.main()
