"""Certification probe isolation (review findings 5 and 7): minimal allowlisted
env for probed CLIs, one process group per step killed on timeout, and HOME in
the Claude and Codex adapter profiles."""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

from openclaw_ecc_orchestrator.runners import base, probes
from openclaw_ecc_orchestrator.runners.base import PROBE_ENV_ALLOW, ProbeContext, default_command_runner, minimal_env
from openclaw_ecc_orchestrator.runners.cli_adapters import ClaudeAdapter, CodexAdapter

PY = sys.executable


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split(") ", 1)[1].split()[0] != "Z"
    except OSError:
        return True


def wait_gone(pid, timeout=5.0):
    deadline = time.monotonic() + timeout
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not pid_alive(pid)


class DefaultRunnerEnvTests(unittest.TestCase):
    def test_parent_secrets_not_inherited(self):
        with mock.patch.dict(os.environ, {"ORCH_ONLY_SECRET": "sk-parent-secret-value-0123456789",
                                          "GROQ_API_KEY": "gsk_x", "HOME": "/home/probe"}):
            res = default_command_runner([PY, "-c", "import os, json; print(json.dumps(sorted(os.environ)))"], 10)
        names = set(json.loads(res.stdout))
        self.assertNotIn("ORCH_ONLY_SECRET", names)
        self.assertNotIn("GROQ_API_KEY", names)
        self.assertIn("PATH", names)
        self.assertIn("HOME", names)
        self.assertLessEqual(names - {"LC_CTYPE", "PWD", "SHLVL", "_", "__CF_USER_TEXT_ENCODING"}, set(PROBE_ENV_ALLOW))

    def test_minimal_env_allowlist(self):
        src = {"PATH": "/bin", "HOME": "/h", "USER": "u", "LANG": "C", "TERM": "xterm", "TMPDIR": "/t",
               "SECRET": "s", "ANTHROPIC_API_KEY": "k"}
        self.assertEqual(set(minimal_env(src)), {"PATH", "HOME", "USER", "LANG", "TERM", "TMPDIR"})
        self.assertIn("ANTHROPIC_API_KEY", minimal_env(src, ["ANTHROPIC_API_KEY"]))
        self.assertNotIn("SECRET", minimal_env(src, ["ANTHROPIC_API_KEY"]))


class DefaultRunnerProcessGroupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_timeout_kills_grandchild(self):
        gc_file = os.path.join(self.tmp, "gc.pid")
        code = (
            "import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"open({gc_file!r}, 'w').write(str(p.pid))\n"
            "print('started', flush=True)\n"
            "time.sleep(60)\n"
        )
        t0 = time.monotonic()
        res = default_command_runner([PY, "-c", code], 1.5)
        self.assertTrue(res.timed_out)
        self.assertIn("started", res.stdout)
        self.assertLess(time.monotonic() - t0, 10)
        with open(gc_file) as fh:
            self.assertTrue(wait_gone(int(fh.read())))

    def test_sigterm_ignoring_group_gets_sigkill(self):
        gc_file = os.path.join(self.tmp, "gc2.pid")
        ignore = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
        code = (
            "import signal, subprocess, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"p = subprocess.Popen([sys.executable, '-c', {ignore!r}])\n"
            f"open({gc_file!r}, 'w').write(str(p.pid))\n"
            "time.sleep(60)\n"
        )
        t0 = time.monotonic()
        with mock.patch.object(base, "KILL_GRACE_SECONDS", 0.5):
            res = default_command_runner([PY, "-c", code], 1.0)
        self.assertTrue(res.timed_out)
        self.assertLess(time.monotonic() - t0, 10)
        with open(gc_file) as fh:
            self.assertTrue(wait_gone(int(fh.read())))

    def test_normal_exit_unchanged(self):
        res = default_command_runner([PY, "-c", "import sys; print('x'); sys.exit(3)"], 10)
        self.assertEqual((res.returncode, res.stdout.strip(), res.timed_out), (3, "x", False))


class ProbeEnvTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.dump = os.path.join(self.tmp, "env.json")
        self.fake = os.path.join(self.tmp, "fake_cli.py")
        with open(self.fake, "w") as fh:
            fh.write("import json, os, sys\n"
                     f"json.dump(dict(os.environ), open({self.dump!r}, 'w'))\n"
                     "print('9.9.9 (fake)')\n")

    def tearDown(self):
        self._tmp.cleanup()

    def env(self):
        # PATH points at an empty directory so no real claude, codex or git is ever run.
        return {"PATH": self.tmp, "HOME": "/home/op", "USER": "op", "LANG": "C",
                "TERM": "dumb", "TMPDIR": self.tmp, "ANTHROPIC_API_KEY": "sk-ant-api03-" + "k" * 30,
                "GROQ_API_KEY": "gsk_" + "g" * 30, "ORCH_ONLY_SECRET": "zzzzzzzzzzzz", "CODEX_HOME": "/home/op/.codex"}

    def probe(self, adapter):
        ctx = ProbeContext(env=self.env(), step_timeout=20, cancel_timeout=1, clock=lambda: 1_790_000_000.0)
        record = probes.probe(adapter, ctx)
        with open(self.dump) as fh:
            return record, json.load(fh)

    def test_claude_probe_env_is_minimal(self):
        adapter = ClaudeAdapter(version_argv=[PY, self.fake, "--version"], auth_argv=[PY, self.fake])
        record, seen = self.probe(adapter)
        self.assertEqual(seen.get("HOME"), "/home/op")
        self.assertIn("ANTHROPIC_API_KEY", seen)
        for name in ("GROQ_API_KEY", "ORCH_ONLY_SECRET", "CODEX_HOME"):
            self.assertNotIn(name, seen)
        self.assertNotIn("k" * 30, json.dumps(record))

    def test_codex_probe_env_is_minimal(self):
        adapter = CodexAdapter(version_argv=[PY, self.fake, "--version"], auth_argv=[PY, self.fake])
        _, seen = self.probe(adapter)
        self.assertEqual(seen.get("HOME"), "/home/op")
        self.assertEqual(seen.get("CODEX_HOME"), "/home/op/.codex")
        for name in ("GROQ_API_KEY", "ORCH_ONLY_SECRET", "ANTHROPIC_API_KEY"):
            self.assertNotIn(name, seen)

    def test_adapter_profiles_include_home(self):
        for adapter in (ClaudeAdapter(), CodexAdapter()):
            self.assertIn("HOME", adapter.credential_env)
            self.assertIn("HOME", adapter.env_allow())
        self.assertIn("ANTHROPIC_API_KEY", ClaudeAdapter().credential_env)

    def test_injected_runner_untouched(self):
        calls = []

        def runner(argv, timeout, cwd=None):
            calls.append(argv)
            return base.CommandResult(1, "", "no")
        ctx = ProbeContext(run_command=runner, env=self.env(), step_timeout=5, cancel_timeout=1)
        probes.probe(ClaudeAdapter(), ctx)
        self.assertTrue(calls)


if __name__ == "__main__":
    unittest.main()
