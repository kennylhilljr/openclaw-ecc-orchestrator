import json
import os
import sys
import tempfile
import threading
import time
import unittest

from openclaw_ecc_orchestrator.process.supervisor import Supervisor, pid_gone

PY = sys.executable


def wait_for_file(path, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path) as fh:
                return fh.read().strip()
        time.sleep(0.02)
    raise AssertionError(f"{path} never appeared")


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.log = os.path.join(self.tmp, "logs", "run.log")
        self.base_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "UNRELATED_SECRET_TOKEN": "leakme-123456",  # gitleaks:allow
                         "KEEP_ME": "1", "MY_API_KEY": "allowed-secret-value-987"}
        self.sup = Supervisor(base_env=self.base_env, grace=0.5)

    def tearDown(self):
        self._tmp.cleanup()

    def test_minimal_allowlisted_env(self):
        code = "import os, json; print(json.dumps(sorted(os.environ)))"
        lines = []
        res = self.sup.run([PY, "-c", code], cwd=self.tmp, env_allow=["KEEP_ME"],
                           on_line=lambda s, l: lines.append(l))
        self.assertEqual(res["exit_code"], 0, res)
        names = json.loads(lines[0])
        self.assertIn("KEEP_ME", names)
        self.assertIn("PATH", names)
        self.assertNotIn("UNRELATED_SECRET_TOKEN", names)
        self.assertNotIn("MY_API_KEY", names)

    def test_streams_to_callback_and_log_with_redaction(self):
        code = (
            "import os, sys\n"
            "print('hello out')\n"
            "print('oops err', file=sys.stderr)\n"
            "print('token=abcdef123456')\n"  # gitleaks:allow
            "print('value is ' + os.environ['MY_API_KEY'])\n"
        )
        seen = []
        res = self.sup.run([PY, "-c", code], cwd=self.tmp, env_allow=["MY_API_KEY"], log_path=self.log,
                           on_line=lambda s, l: seen.append((s, l)))
        self.assertEqual(res["exit_code"], 0)
        self.assertIn(("stdout", "hello out"), seen)
        self.assertIn(("stderr", "oops err"), seen)
        with open(self.log) as fh:
            log = fh.read()
        for secret in ("abcdef123456", "allowed-secret-value-987"):
            self.assertNotIn(secret, log)
            self.assertNotIn(secret, " ".join(l for _, l in seen))
        self.assertIn("[stdout] hello out", log)
        self.assertIn("[stderr] oops err", log)

    def test_exit_status_record(self):
        status = os.path.join(self.tmp, "status.json")
        res = self.sup.run([PY, "-c", "import sys; sys.exit(3)"], cwd=self.tmp, status_path=status)
        self.assertEqual(res["exit_code"], 3)
        self.assertFalse(res["timed_out"])
        with open(status) as fh:
            rec = json.load(fh)
        self.assertEqual(rec["schema_version"], "1.0")
        self.assertEqual(rec["exit_code"], 3)
        self.assertEqual(rec["argv"][0], PY)

    def test_cwd(self):
        out = []
        self.sup.run([PY, "-c", "import os; print(os.getcwd())"], cwd=self.tmp, on_line=lambda s, l: out.append(l))
        self.assertEqual(os.path.realpath(out[0]), os.path.realpath(self.tmp))

    def test_rejects_shell_string(self):
        with self.assertRaises(TypeError):
            self.sup.run("echo hi", cwd=self.tmp)

    def test_spawn_failure_reported(self):
        res = self.sup.run(["/nonexistent/binary-xyz"], cwd=self.tmp)
        self.assertIsNone(res["exit_code"])
        self.assertTrue(res["error"])

    def test_hard_timeout(self):
        t0 = time.monotonic()
        res = self.sup.run([PY, "-c", "import time; time.sleep(30)"], cwd=self.tmp, timeout=0.5)
        self.assertTrue(res["timed_out"])
        self.assertIsNone(res["exit_code"])
        self.assertLess(time.monotonic() - t0, 10)

    def test_cooperative_cancel(self):
        code = (
            "import signal, sys, time\n"
            "signal.signal(signal.SIGTERM, lambda *a: (print('bye', flush=True), sys.exit(0)))\n"
            "print('ready', flush=True)\n"
            "time.sleep(30)\n"
        )
        ready = threading.Event()
        handle = self.sup.start([PY, "-c", code], cwd=self.tmp,
                                on_line=lambda s, l: ready.set() if l == "ready" else None)
        self.assertTrue(ready.wait(5))
        handle.cancel("user asked")
        res = handle.wait(10)
        self.assertTrue(res["cancelled"])
        self.assertEqual(res["cancel_reason"], "user asked")
        self.assertFalse(res["escalated_to_kill"])
        self.assertEqual(res["exit_code"], 0)

    def test_cancel_escalates_when_sigterm_ignored(self):
        code = (
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('ready', flush=True)\n"
            "while True: time.sleep(0.1)\n"
        )
        ready = threading.Event()
        handle = self.sup.start([PY, "-c", code], cwd=self.tmp,
                                on_line=lambda s, l: ready.set() if l == "ready" else None)
        self.assertTrue(ready.wait(5))
        handle.cancel()
        res = handle.wait(10)
        self.assertTrue(res["cancelled"])
        self.assertTrue(res["escalated_to_kill"])
        self.assertEqual(res["signal"], 9)
        self.assertTrue(pid_gone(handle.pid))

    def test_cancel_kills_grandchild(self):
        gc_file = os.path.join(self.tmp, "gc.pid")
        code = (
            "import subprocess, sys, time\n"
            f"p = subprocess.Popen([sys.executable, '-c', 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
            f"open({gc_file!r}, 'w').write(str(p.pid))\n"
            "time.sleep(60)\n"
        )
        handle = self.sup.start([PY, "-c", code], cwd=self.tmp)
        gc_pid = int(wait_for_file(gc_file))
        handle.cancel()
        res = handle.wait(10)
        self.assertTrue(res["cancelled"])
        deadline = time.monotonic() + 5
        while not pid_gone(gc_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(pid_gone(gc_pid))

    def test_lingering_grandchild_does_not_hang_wait(self):
        gc_file = os.path.join(self.tmp, "gc2.pid")
        code = (
            "import subprocess, sys\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"open({gc_file!r}, 'w').write(str(p.pid))\n"
            "print('parent done')\n"
        )
        t0 = time.monotonic()
        res = self.sup.run([PY, "-c", code], cwd=self.tmp, timeout=20)
        self.assertEqual(res["exit_code"], 0)
        self.assertLess(time.monotonic() - t0, 10)
        gc_pid = int(wait_for_file(gc_file))
        deadline = time.monotonic() + 5
        while not pid_gone(gc_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(pid_gone(gc_pid))

    def test_on_start_reports_pid(self):
        pids = []
        res = self.sup.run([PY, "-c", "pass"], cwd=self.tmp, on_start=pids.append)
        self.assertEqual(pids, [res["pid"]])


if __name__ == "__main__":
    unittest.main()
