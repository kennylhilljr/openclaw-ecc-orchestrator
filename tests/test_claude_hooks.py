"""Claude Code project hooks: .claude/hooks/check_edit.py and .claude/settings.json.

The script is fed JSON on stdin the way Claude Code sends it. PreEditTests
launch the real script; PostEditTests call its main() with subprocess
mocked, plus one real smoke test of the docs route.
"""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / ".claude" / "hooks" / "check_edit.py"
SETTINGS = REPO / ".claude" / "settings.json"
COMMAND = 'python3 "${CLAUDE_PROJECT_DIR}/.claude/hooks/check_edit.py"'


def load_hook():
    spec = importlib.util.spec_from_file_location("check_edit_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def payload(event, file_path, tool="Write", cwd=None, extra=None):
    tool_input = {"file_path": file_path}
    if tool == "Write":
        tool_input["content"] = "x"
    elif tool == "Edit":
        tool_input.update(old_string="a", new_string="b")
    elif tool == "MultiEdit":
        tool_input["edits"] = [{"old_string": "a", "new_string": "b"}]
    doc = {"session_id": "s", "hook_event_name": event, "tool_name": tool, "tool_input": tool_input}
    if cwd is not None:
        doc["cwd"] = cwd
    if extra:
        doc.update(extra)
    return doc


def clean_env():
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env


def run_script(doc_or_text, timeout=60):
    text = doc_or_text if isinstance(doc_or_text, str) else json.dumps(doc_or_text)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=text,
        capture_output=True,
        text=True,
        cwd=tempfile.gettempdir(),
        env=clean_env(),
        timeout=timeout,
    )


def context_of(stdout):
    doc = json.loads(stdout)
    out = doc["hookSpecificOutput"]
    return out["hookEventName"], out["additionalContext"], doc


class PreEditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ecc-hook-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def pre(self, path, tool="Write", cwd=None):
        return run_script(payload("PreToolUse", path, tool=tool, cwd=cwd))

    def assert_blocked(self, proc):
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("environment variables", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(proc.stdout, "")

    def assert_schema_warning(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        event, text, doc = context_of(proc.stdout)
        self.assertEqual(event, "PreToolUse")
        self.assertIn("src/openclaw_ecc_orchestrator/schemas.py", text)
        self.assertIn("tests/test_schemas_json.py", text)
        self.assertNotIn("permissionDecision", doc["hookSpecificOutput"])
        self.assertNotIn("decision", doc)

    def assert_silent(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertNotIn("Traceback", proc.stderr)

    def test_env_blocked_for_each_tool(self):
        for tool in ("Write", "Edit", "MultiEdit"):
            with self.subTest(tool=tool):
                self.assert_blocked(self.pre(str(REPO / ".env"), tool=tool))

    def test_env_variants_blocked(self):
        for name in (".env", ".env.local", ".env.example", ".envrc", ".env.production"):
            with self.subTest(name=name):
                self.assert_blocked(self.pre(str(REPO / name)))
                self.assert_blocked(self.pre(str(REPO / "src" / name)))

    def test_env_relative_paths_blocked(self):
        self.assert_blocked(self.pre(".env"))
        self.assert_blocked(self.pre("config/.env.local", cwd=self.tmp))
        self.assert_blocked(self.pre("./sub/../.env", cwd=str(REPO)))

    def test_env_outside_repository_blocked(self):
        self.assert_blocked(self.pre(os.path.join(self.tmp, ".env")))
        self.assert_blocked(self.pre("/etc/app/.env.example", tool="Edit"))

    def test_env_symlink_alias_blocked(self):
        real = os.path.join(self.tmp, ".env")
        Path(real).write_text("A=1\n")
        alias = os.path.join(self.tmp, "settings.txt")
        os.symlink(real, alias)
        self.assert_blocked(self.pre(alias))

    def test_env_lookalikes_not_blocked(self):
        for path in ("docs/env.md", ".env-directory/ordinary.txt", "src/environment.py", "notes.env", "my.env.example"):
            with self.subTest(path=path):
                self.assert_silent(self.pre(path))

    def test_schema_warning_for_each_tool(self):
        for tool in ("Write", "Edit", "MultiEdit"):
            with self.subTest(tool=tool):
                self.assert_schema_warning(self.pre(str(REPO / "schemas" / "work_unit.schema.json"), tool=tool))

    def test_schema_warning_relative_and_cwd_fallback(self):
        self.assert_schema_warning(self.pre("schemas/x.json"))  # no cwd: repository root
        self.assert_schema_warning(self.pre("schemas/x.json", cwd=str(REPO)))
        self.assert_schema_warning(self.pre("../schemas/x.json", cwd=str(REPO / "docs")))

    def test_schema_warning_normalized_paths(self):
        self.assert_schema_warning(self.pre(str(REPO / "src" / ".." / "schemas" / "x.json")))
        self.assert_schema_warning(self.pre("schemas/nested/../x.json"))
        self.assert_schema_warning(self.pre("./schemas//x.json"))

    def test_schema_warning_through_symlink_alias(self):
        alias = os.path.join(self.tmp, "repo-alias")
        os.symlink(str(REPO), alias)
        self.assert_schema_warning(self.pre(os.path.join(alias, "schemas", "x.json")))

    def test_schema_lookalikes_not_warned(self):
        for path in (
            "tests/schemas/x.json",
            "schemas/nested/x.json",
            "schemas/x.yaml",
            "schemas",
            "fixtures/schemas.json",
            "src/openclaw_ecc_orchestrator/schemas.py",
        ):
            with self.subTest(path=path):
                self.assert_silent(self.pre(path))
        self.assert_silent(self.pre("schemas/x.json", cwd=self.tmp))
        self.assert_silent(self.pre(os.path.join(self.tmp, "schemas", "x.json")))

    def test_unrelated_tools_and_events_ignored(self):
        self.assert_silent(run_script(payload("PreToolUse", ".env", tool="Read")))
        self.assert_silent(run_script(payload("PreToolUse", ".env", tool="Bash")))
        self.assert_silent(run_script(payload("UserPromptSubmit", ".env")))
        self.assert_silent(run_script(payload("Notification", "schemas/x.json")))

    def test_malformed_input_is_diagnosed_without_traceback(self):
        cases = [
            "",
            "not json",
            "[1, 2]",
            json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Write"}),
            json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Write", "tool_input": "x"}),
            json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Write", "tool_input": {}}),
            json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Write", "tool_input": {"file_path": 7}}),
            json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Write", "tool_input": {"file_path": ""}}),
            json.dumps({"hook_event_name": "PreToolUse", "tool_name": [], "tool_input": {"file_path": ".env"}}),
            json.dumps({"hook_event_name": {"a": 1}, "tool_name": "Write", "tool_input": {"file_path": ".env"}}),
        ]
        for text in cases:
            with self.subTest(text=text):
                proc = run_script(text)
                self.assertEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout, "")
                self.assertIn("check_edit:", proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                if text:
                    self.assertNotIn(text, proc.stderr)

    def test_bad_cwd_type_falls_back_to_repository(self):
        doc = payload("PreToolUse", "schemas/x.json", extra={"cwd": 5})
        self.assert_schema_warning(run_script(doc))


class FakeRun:
    """Records subprocess.run calls and whether TMPDIR existed during each."""

    def __init__(self, returncode=0, stdout="ok\n", stderr="", exc=None):
        self.calls = []
        self.returncode, self.stdout, self.stderr, self.exc = returncode, stdout, stderr, exc

    def __call__(self, argv, **kwargs):
        tmpdir = kwargs["env"]["TMPDIR"]
        self.calls.append({"argv": argv, "kwargs": kwargs, "tmp_existed": os.path.isdir(tmpdir)})
        if self.exc is not None:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


class PostEditTests(unittest.TestCase):
    def setUp(self):
        self.hook = load_hook()
        self.tmp = tempfile.mkdtemp(prefix="ecc-hook-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def post(self, path, fake=None, tool="Edit", cwd=None, env=None):
        fake = fake if fake is not None else FakeRun()
        out, err = io.StringIO(), io.StringIO()
        stdin = io.StringIO(json.dumps(payload("PostToolUse", path, tool=tool, cwd=cwd)))
        patch_env = mock.patch.dict(os.environ, env or {}, clear=False)
        with patch_env, mock.patch.object(self.hook.subprocess, "run", fake):
            code = self.hook.main(stdin=stdin, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue(), fake

    def context(self, out):
        event, text, doc = context_of(out)
        self.assertEqual(event, "PostToolUse")
        self.assertNotIn("decision", doc)
        self.assertNotIn("permissionDecision", doc["hookSpecificOutput"])
        return text

    def test_selection_is_exact(self):
        sel = self.hook.python_test_modules
        self.assertEqual(sel(("src", "openclaw_ecc_orchestrator", "runs", "liveness.py")), ["tests.test_runs_liveness"])
        self.assertEqual(
            sel(("src", "openclaw_ecc_orchestrator", "schemas.py")),
            ["tests.test_schemas", "tests.test_schemas_fixtures", "tests.test_schemas_json"],
        )
        self.assertEqual(sel(("src", "openclaw_ecc_orchestrator", "runs", "__init__.py")), [])
        self.assertEqual(sel(("src", "openclaw_ecc_orchestrator", "runs", "data.txt")), [])
        self.assertEqual(sel(("src", "other", "runs", "liveness.py")), [])

    def test_python_route_argv_cwd_env(self):
        for tool in ("Write", "Edit", "MultiEdit"):
            with self.subTest(tool=tool):
                code, out, err, fake = self.post(
                    str(REPO / "src/openclaw_ecc_orchestrator/runs/liveness.py"), tool=tool, env={"PYTHONPATH": "/x/inherited"}
                )
                self.assertEqual(code, 0, err)
                self.assertEqual(len(fake.calls), 1)
                call = fake.calls[0]
                self.assertEqual(call["argv"], [sys.executable, "-m", "unittest", "tests.test_runs_liveness"])
                kw = call["kwargs"]
                self.assertEqual(Path(kw["cwd"]), REPO)
                self.assertFalse(kw.get("shell"))
                self.assertEqual(kw["timeout"], 120)
                self.assertEqual(kw["env"]["PYTHONPATH"], str(REPO / "src") + os.pathsep + "/x/inherited")
                self.assertIn("passed", self.context(out))
                self.assertIn("tests.test_runs_liveness", self.context(out))

    def test_schemas_source_runs_schema_tests(self):
        code, out, _err, fake = self.post("src/openclaw_ecc_orchestrator/schemas.py", cwd=str(REPO))
        self.assertEqual(code, 0)
        self.assertEqual(
            fake.calls[0]["argv"][3:], ["tests.test_schemas", "tests.test_schemas_fixtures", "tests.test_schemas_json"]
        )

    def test_pythonpath_set_when_not_inherited(self):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        with mock.patch.dict(os.environ, env, clear=True):
            _code, _out, _err, fake = self.post("src/openclaw_ecc_orchestrator/runs/liveness.py")
        self.assertEqual(fake.calls[0]["kwargs"]["env"]["PYTHONPATH"], str(REPO / "src"))

    def test_fresh_tmpdir_per_invocation_and_cleanup(self):
        fake = FakeRun()
        self.post("src/openclaw_ecc_orchestrator/runs/liveness.py", fake=fake)
        self.post("src/openclaw_ecc_orchestrator/runs/liveness.py", fake=fake)
        dirs = [c["kwargs"]["env"]["TMPDIR"] for c in fake.calls]
        self.assertEqual(len(set(dirs)), 2)
        for call, d in zip(fake.calls, dirs):
            self.assertTrue(call["tmp_existed"])
            self.assertFalse(os.path.exists(d))
            resolved = Path(d).resolve()
            self.assertNotIn(".openclaw", resolved.parts)
            self.assertNotIn(REPO, resolved.parents)

    def test_cleanup_on_failure_timeout_and_launch_error(self):
        fakes = [
            FakeRun(returncode=1, stdout="", stderr="FAIL: test_x\nAssertionError: boom\n"),
            FakeRun(exc=subprocess.TimeoutExpired(["x"], 120)),
            FakeRun(exc=FileNotFoundError(2, "No such file or directory")),
        ]
        expected = ["FAILED (exit 1)", "timed out after 120 seconds", "Could not launch"]
        for fake, want in zip(fakes, expected):
            with self.subTest(want=want):
                code, out, _err, _ = self.post("src/openclaw_ecc_orchestrator/runs/liveness.py", fake=fake)
                self.assertEqual(code, 0)
                text = self.context(out)
                self.assertIn(want, text)
                self.assertIn("tests.test_runs_liveness", text)
                self.assertFalse(os.path.exists(fake.calls[0]["kwargs"]["env"]["TMPDIR"]))
        _code, out, _err, _ = self.post("src/openclaw_ecc_orchestrator/runs/liveness.py", fake=fakes[0])
        self.assertIn("AssertionError: boom", self.context(out))

    def test_unsafe_inherited_tmpdir_rejected(self):
        openclaw_tmp = os.path.join(self.tmp, ".openclaw", "tmp")
        os.makedirs(openclaw_tmp)
        for bad in (openclaw_tmp, str(REPO / "tests"), "relative/tmp"):
            with self.subTest(bad=bad):
                _code, _out, _err, fake = self.post("docs/cli.md", env={"TMPDIR": bad})
                tmpdir = Path(fake.calls[0]["kwargs"]["env"]["TMPDIR"]).resolve()
                self.assertNotIn(".openclaw", tmpdir.parts)
                self.assertNotIn(REPO, tmpdir.parents)

    def test_unsafe_tmpdir_never_probed_via_gettempdir(self):
        with mock.patch.object(self.hook.tempfile, "gettempdir", side_effect=AssertionError("probed")):
            code, out, _err, fake = self.post("docs/cli.md", env={"TMPDIR": os.path.join(self.tmp, ".openclaw")})
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("passed", self.context(out))

    def test_cleanup_error_reported(self):
        real = self.hook.tempfile.TemporaryDirectory

        class Leaky:
            def __init__(self, **kwargs):
                self.inner = real(**kwargs)
                self.name = self.inner.name

            def cleanup(self):
                self.inner.cleanup()
                raise PermissionError(13, "denied")

        with mock.patch.object(self.hook.tempfile, "TemporaryDirectory", Leaky):
            code, out, _err, _fake = self.post("docs/cli.md")
        self.assertEqual(code, 0)
        text = self.context(out)
        self.assertIn("passed", text)
        self.assertIn("could not remove temporary directory", text)

    def test_no_safe_tmpdir_runs_nothing(self):
        fake = FakeRun()
        with mock.patch.object(self.hook, "safe_temp_base", return_value=None):
            code, out, _err, _ = self.post("docs/cli.md", fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls, [])
        self.assertIn("no safe temporary directory", self.context(out))

    def test_tempdir_creation_error_reported(self):
        fake = FakeRun()
        with mock.patch.object(self.hook.tempfile, "TemporaryDirectory", side_effect=OSError("nope")):
            code, out, _err, _ = self.post("docs/cli.md", fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls, [])
        self.assertIn("could not create a temporary directory", self.context(out))

    def test_unmatched_source_reports_and_launches_nothing(self):
        for path in (
            "src/openclaw_ecc_orchestrator/runs/__init__.py",
            "src/openclaw_ecc_orchestrator/cli/no_such_module.py",
            "src/openclaw_ecc_orchestrator/runs/notes.txt",
            "src/other_package/x.py",
        ):
            with self.subTest(path=path):
                code, out, _err, fake = self.post(path)
                self.assertEqual(code, 0)
                self.assertEqual(fake.calls, [])
                self.assertIn("No test files match", self.context(out))

    def test_docs_route(self):
        code, out, _err, fake = self.post(str(REPO / "docs" / "cli.md"), tool="Write")
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls[0]["argv"], [sys.executable, "-m", "unittest", "tests.test_cli.DocsTest"])
        self.assertEqual(Path(fake.calls[0]["kwargs"]["cwd"]), REPO)
        self.assertIn("passed", self.context(out))

    def test_plugin_route(self):
        for tool in ("Write", "Edit", "MultiEdit"):
            with self.subTest(tool=tool):
                code, out, _err, fake = self.post("integrations/openclaw-plugin/src/index.js", tool=tool)
                self.assertEqual(code, 0)
                call = fake.calls[0]
                self.assertEqual(call["argv"], ["npm", "test"])
                self.assertEqual(Path(call["kwargs"]["cwd"]), REPO / "integrations" / "openclaw-plugin")
                self.assertFalse(call["kwargs"].get("shell"))
                self.assertIn("npm test", self.context(out))

    def test_missing_npm_reported(self):
        fake = FakeRun(exc=FileNotFoundError(2, "No such file or directory"))
        code, out, _err, _ = self.post("integrations/openclaw-plugin/package.json", fake=fake)
        self.assertEqual(code, 0)
        self.assertIn("Could not launch `npm test`", self.context(out))

    def test_unrelated_paths_launch_nothing(self):
        alias = os.path.join(self.tmp, "fake-repo")
        os.makedirs(os.path.join(alias, "docs"))
        for path in (
            "docs/env.md",
            "docs/cli.md.bak",
            "docs/sub/cli.md",
            "README.md",
            "tests/test_runs_liveness.py",
            "integrations/openclaw-plugin",
            "integrations/openclaw-plugin-extra/index.js",
            "srcx/openclaw_ecc_orchestrator/runs/liveness.py",
            "src_old/openclaw_ecc_orchestrator/schemas.py",
            os.path.join(alias, "docs", "cli.md"),
            os.path.join(self.tmp, "src", "openclaw_ecc_orchestrator", "schemas.py"),
        ):
            with self.subTest(path=path):
                code, out, _err, fake = self.post(path)
                self.assertEqual(code, 0)
                self.assertEqual(fake.calls, [])
                self.assertEqual(out, "")
        _code, out, _err, fake = self.post("cli.md", cwd=os.path.join(alias, "docs"))
        self.assertEqual(fake.calls, [])

    def test_unrelated_events_and_tools_launch_nothing(self):
        fake = FakeRun()
        for doc in (
            payload("PostToolUse", "docs/cli.md", tool="Read"),
            payload("PostToolUse", "docs/cli.md", tool="Bash"),
            payload("Stop", "docs/cli.md"),
        ):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(self.hook.subprocess, "run", fake):
                code = self.hook.main(stdin=io.StringIO(json.dumps(doc)), stdout=out, stderr=err)
            self.assertEqual(code, 0)
            self.assertEqual(out.getvalue(), "")
        self.assertEqual(fake.calls, [])

    def test_docs_smoke_runs_real_doctest_without_pythonpath(self):
        proc = run_script(payload("PostToolUse", str(REPO / "docs" / "cli.md"), tool="Edit"), timeout=150)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        _event, text, _doc = context_of(proc.stdout)
        self.assertIn("tests.test_cli.DocsTest", text)
        self.assertIn("passed", text)
        self.assertNotIn("FAILED", text)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        with open(SETTINGS, encoding="utf-8") as fh:
            self.settings = json.load(fh)

    def registrations(self, event):
        regs = self.settings["hooks"][event]
        self.assertEqual(len(regs), 1)
        self.assertEqual(regs[0]["matcher"], "Write|Edit|MultiEdit")
        self.assertEqual(len(regs[0]["hooks"]), 1)
        return regs[0]["hooks"][0]

    def test_both_events_registered_with_exact_command(self):
        for event in ("PreToolUse", "PostToolUse"):
            with self.subTest(event=event):
                hook = self.registrations(event)
                self.assertEqual(hook["type"], "command")
                self.assertEqual(hook["command"], COMMAND)

    def test_only_check_edit_is_called(self):
        commands = [h["command"] for regs in self.settings["hooks"].values() for r in regs for h in r["hooks"]]
        self.assertTrue(commands)
        for command in commands:
            self.assertEqual(command, COMMAND)
        self.assertEqual(set(self.settings["hooks"]), {"PreToolUse", "PostToolUse"})

    def test_post_tool_use_timeout_covers_script_timeout(self):
        self.assertEqual(self.registrations("PostToolUse")["timeout"], 150)
        self.assertGreater(150, load_hook().TIMEOUT_SECONDS)

    def test_agents_md_mentions_hooks(self):
        text = (REPO / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn(".claude/settings.json", text)
        self.assertIn(".claude/hooks/check_edit.py", text)
        self.assertIn(".env", text)


if __name__ == "__main__":
    unittest.main()
