"""Monitor robustness regression tests (review finding 1).

The monitor thread must never die silently and leave ``wait()`` blocked:
``os.waitid`` may be missing (some macOS builds) or fail, and any monitor
exception must end in a finished, error carrying result.
"""

import contextlib
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from openclaw_ecc_orchestrator.process import supervisor as sup_mod
from openclaw_ecc_orchestrator.process.supervisor import ExitWatcher, ProcessHandle, Supervisor, pid_gone

PY = sys.executable
_REAL_WAITID = getattr(os, "waitid", None)


@contextlib.contextmanager
def no_waitid():
    saved = {name: getattr(os, name) for name in ("waitid",) if hasattr(os, name)}
    for name in saved:
        delattr(os, name)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(os, name, value)


class FakeKqueue:
    """Linux stand in for select.kqueue with EVFILT_PROC/NOTE_EXIT semantics."""

    instances = []

    def __init__(self):
        self.pids = []
        self.closed = False
        FakeKqueue.instances.append(self)

    def control(self, changes, max_events, timeout=None):
        for ev in changes or ():
            self.pids.append(ev.ident)
        out = []
        for pid in self.pids:
            info = _REAL_WAITID(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if info is not None:
                out.append(types.SimpleNamespace(ident=pid))
        return out[:max_events] if max_events else []

    def close(self):
        self.closed = True


def fake_select():
    def kevent(ident, filter=None, flags=None, fflags=None):
        return types.SimpleNamespace(ident=ident, filter=filter, flags=flags, fflags=fflags)
    return types.SimpleNamespace(kqueue=FakeKqueue, kevent=kevent, KQ_FILTER_PROC=-5, KQ_NOTE_EXIT=0x80000000,
                                 KQ_EV_ADD=1, KQ_EV_ENABLE=4)


class MonitorFallbackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.sup = Supervisor(base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, grace=0.3)

    def tearDown(self):
        self._tmp.cleanup()

    def _basic_run(self):
        lines = []
        res = self.sup.start([PY, "-c", "print('hi'); raise SystemExit(4)"], cwd=self.tmp,
                             on_line=lambda s, l: lines.append(l)).wait(10)
        self.assertEqual(res["exit_code"], 4, res)
        self.assertIsNone(res["error"])
        self.assertEqual(lines, ["hi"])

    def test_missing_waitid_uses_fallback(self):
        with no_waitid():
            self._basic_run()

    def test_waitid_not_implemented_falls_back(self):
        with mock.patch.object(os, "waitid", side_effect=NotImplementedError("no waitid"), create=True):
            self._basic_run()

    def test_waitid_oserror_falls_back(self):
        with mock.patch.object(os, "waitid", side_effect=OSError(38, "Function not implemented"), create=True):
            self._basic_run()

    def test_poll_fallback_timeout_and_grandchild_cleanup(self):
        gc_file = os.path.join(self.tmp, "gc.pid")
        code = (
            "import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"open({gc_file!r}, 'w').write(str(p.pid))\n"
            "time.sleep(60)\n"
        )
        with no_waitid(), mock.patch.object(sup_mod, "select", types.SimpleNamespace()):
            t0 = time.monotonic()
            res = self.sup.run([PY, "-c", code], cwd=self.tmp, timeout=1.0)
            self.assertTrue(res["timed_out"], res)
            self.assertLess(time.monotonic() - t0, 10)
        with open(gc_file) as fh:
            gc_pid = int(fh.read())
        deadline = time.monotonic() + 5
        while not pid_gone(gc_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(pid_gone(gc_pid))

    def test_poll_fallback_cancel(self):
        ready = threading.Event()
        with no_waitid(), mock.patch.object(sup_mod, "select", types.SimpleNamespace()):
            handle = self.sup.start([PY, "-c", "import time; print('ready', flush=True); time.sleep(60)"],
                                    cwd=self.tmp, on_line=lambda s, l: ready.set())
            self.assertTrue(ready.wait(5))
            self.assertEqual(handle._watcher.mode, "poll")
            handle.cancel("stop")
            res = handle.wait(10)
        self.assertTrue(res["cancelled"])
        self.assertTrue(pid_gone(handle.pid))

    @unittest.skipIf(_REAL_WAITID is None, "fake kqueue needs os.waitid to observe exits")
    def test_kqueue_fallback(self):
        FakeKqueue.instances.clear()
        with no_waitid(), mock.patch.object(sup_mod, "select", fake_select()):
            handle = self.sup.start([PY, "-c", "print('k')"], cwd=self.tmp)
            res = handle.wait(10)
        self.assertEqual(res["exit_code"], 0, res)
        self.assertTrue(FakeKqueue.instances)
        self.assertTrue(all(k.closed for k in FakeKqueue.instances))
        self.assertIn(handle.pid, FakeKqueue.instances[0].pids)

    def test_watcher_mode_selection(self):
        proc = types.SimpleNamespace(pid=os.getpid(), poll=lambda: None)
        if _REAL_WAITID is not None:
            self.assertEqual(ExitWatcher(proc).mode, "waitid")
        with no_waitid():
            with mock.patch.object(sup_mod, "select", types.SimpleNamespace()):
                self.assertEqual(ExitWatcher(proc).mode, "poll")

    def test_kqueue_registration_esrch_means_exited(self):
        def kqueue():
            k = FakeKqueue()
            k.control = mock.Mock(side_effect=ProcessLookupError(3, "No such process"))
            return k
        fake = fake_select()
        fake.kqueue = kqueue
        proc = types.SimpleNamespace(pid=123456, poll=lambda: None)
        with no_waitid(), mock.patch.object(sup_mod, "select", fake):
            self.assertTrue(ExitWatcher(proc).exited())


class MonitorFailureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.sup = Supervisor(base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, grace=0.3)

    def tearDown(self):
        self._tmp.cleanup()

    def test_monitor_exception_sets_done_with_error_and_kills(self):
        with mock.patch.object(ProcessHandle, "_exit_probe", side_effect=RuntimeError("boom")):
            handle = self.sup.start([PY, "-c", "import time; time.sleep(60)"], cwd=self.tmp,
                                    status_path=os.path.join(self.tmp, "status.json"))
            res = handle.wait(10)
        self.assertIn("monitor_failed", res["error"])
        self.assertIn("RuntimeError", res["error"])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "status.json")))
        deadline = time.monotonic() + 5
        while not pid_gone(handle.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(pid_gone(handle.pid))

    def test_monitor_error_message_is_redacted(self):
        secret = "gsk_" + "Q1w2E3r4T5y6U7i8O9p0A1s2D3f4G5h6"
        with mock.patch.object(ProcessHandle, "_exit_probe", side_effect=RuntimeError("bad " + secret)):
            res = self.sup.start([PY, "-c", "pass"], cwd=self.tmp).wait(10)
        self.assertNotIn(secret, res["error"])

    def test_dead_monitor_thread_does_not_block_wait(self):
        with mock.patch.object(ProcessHandle, "_monitor", lambda self: None):
            handle = self.sup.start([PY, "-c", "import time; time.sleep(60)"], cwd=self.tmp)
            t0 = time.monotonic()
            res = handle.wait()
        self.assertLess(time.monotonic() - t0, 10)
        self.assertIn("monitor_failed", res["error"])

    def test_wait_timeout(self):
        handle = self.sup.start([PY, "-c", "import time; time.sleep(60)"], cwd=self.tmp)
        with self.assertRaises(TimeoutError):
            handle.wait(timeout=0.2)
        handle.cancel()
        self.assertTrue(handle.wait(10)["cancelled"])


if __name__ == "__main__":
    unittest.main()
