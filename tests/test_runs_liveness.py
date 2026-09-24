"""Portable process liveness: Linux /proc identity, macOS/BSD `ps`, kill-only fallback."""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from openclaw_ecc_orchestrator.runs import liveness as L
from openclaw_ecc_orchestrator.runs.manager import RunManager
from openclaw_ecc_orchestrator.runs.store import RunStore



class FakeClock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class Ids:
    def __init__(self, prefix="id"):
        self.n, self.prefix = 0, prefix

    def __call__(self):
        self.n += 1
        return f"{self.prefix}{self.n}"


try:
    from ._units import work_unit
except ImportError:  # discovered as a top level module
    from _units import work_unit


def unit(uid):
    # create_run validates every unit with schemas.validate_work_unit.
    return work_unit(uid)


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid

LSTART_A = "Thu Sep 24 10:00:00 2026"
LSTART_B = "Thu Sep 24 11:30:05 2026"


class FakeProbe:
    """Injectable probe: `kill_exc` is raised by kill(pid, 0); `ident` is what identity() returns."""

    def __init__(self, ident=None, kill_exc=None, method="fake"):
        self.ident = ident
        self.kill_exc = kill_exc
        self.method = method
        self.kills = []

    def kill(self, pid, sig):
        self.kills.append((pid, sig))
        if self.kill_exc is not None:
            raise self.kill_exc

    def identity(self, pid):
        return self.ident


def ident(token, method="fake", state="S"):
    return L.Identity(state=state, token=token, method=method)


class FakeRun:
    """Stand-in for subprocess.run that records the call."""

    def __init__(self, stdout="", returncode=0, exc=None):
        self.stdout, self.returncode, self.exc = stdout, returncode, exc
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.exc is not None:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.returncode, stdout=self.stdout, stderr="")


class ClassifyTests(unittest.TestCase):
    def rec(self, token="T1", method="fake", pid=4242):
        return {"pid": pid, "start_token": token, "start_method": method}

    def test_alive_same_identity(self):
        self.assertEqual(L.process_liveness(self.rec(), probe=FakeProbe(ident("T1"))), L.ALIVE)

    def test_alive_but_reused_pid_is_dead(self):
        self.assertEqual(L.process_liveness(self.rec(), probe=FakeProbe(ident("T2"))), L.DEAD)

    def test_dead_pid(self):
        probe = FakeProbe(ident("T1"), kill_exc=ProcessLookupError())
        self.assertEqual(L.process_liveness(self.rec(), probe=probe), L.DEAD)
        self.assertFalse(L.is_process_alive(self.rec(), probe=probe))

    def test_eperm_means_alive(self):
        probe = FakeProbe(ident("T1"), kill_exc=PermissionError())
        self.assertEqual(L.process_liveness(self.rec(), probe=probe), L.ALIVE)
        # EPERM without a start time: alive but identity unverified.
        probe = FakeProbe(None, kill_exc=PermissionError())
        self.assertEqual(L.process_liveness(self.rec(), probe=probe), L.UNVERIFIED)

    def test_identity_unavailable_is_unverified_not_dead(self):
        probe = FakeProbe(None)
        self.assertEqual(L.process_liveness(self.rec(), probe=probe), L.UNVERIFIED)
        self.assertTrue(L.is_process_alive(self.rec(), probe=probe))  # "may be alive"

    def test_record_without_identity_is_unverified(self):
        self.assertEqual(L.process_liveness({"pid": 4242}, probe=FakeProbe(ident("T1"))), L.UNVERIFIED)
        self.assertEqual(L.process_liveness({"pid": 4242, "start_token": None, "start_method": None},
                                            probe=FakeProbe(ident("T1"))), L.UNVERIFIED)

    def test_method_mismatch_is_unverified(self):
        rec = self.rec(method=L.METHOD_PROC)
        self.assertEqual(L.process_liveness(rec, probe=FakeProbe(ident("T9", method=L.METHOD_PS))), L.UNVERIFIED)

    def test_zombie_is_dead(self):
        self.assertEqual(L.process_liveness(self.rec(), probe=FakeProbe(ident("T1", state="Z+"))), L.DEAD)

    def test_gone_between_kill_and_probe_is_dead(self):
        self.assertEqual(L.process_liveness(self.rec(), probe=FakeProbe(L.GONE)), L.DEAD)

    def test_invalid_records(self):
        probe = FakeProbe(ident("T1"))
        for rec in (None, {}, {"pid": 0}, {"pid": -3}, {"pid": "12"}, {"pid": True}):
            self.assertEqual(L.process_liveness(rec, probe=probe), L.DEAD, rec)
        self.assertEqual(probe.kills, [])

    def test_legacy_start_ticks_record_compares_against_proc_method(self):
        rec = {"pid": 4242, "start_ticks": "777"}
        self.assertEqual(L.process_liveness(rec, probe=FakeProbe(ident("777", method=L.METHOD_PROC))), L.ALIVE)
        self.assertEqual(L.process_liveness(rec, probe=FakeProbe(ident("778", method=L.METHOD_PROC))), L.DEAD)
        self.assertEqual(L.process_liveness(rec, probe=FakeProbe(ident(LSTART_A, method=L.METHOD_PS))), L.UNVERIFIED)


class PsProbeTests(unittest.TestCase):
    def test_argv_env_timeout_and_parse(self):
        run = FakeRun(stdout=f"Ss   {LSTART_A.replace(' 24 ', '  24 ')}\n")
        got = L.PsProbe(runner=run, timeout=1.5).identity(4242)
        self.assertEqual(got, L.Identity(state="Ss", token=LSTART_A, method=L.METHOD_PS))
        argv, kw = run.calls[0]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[0], "ps")
        self.assertEqual(argv[-2:], ["-p", "4242"])
        self.assertIn("lstart=", argv)
        self.assertEqual(kw["timeout"], 1.5)
        self.assertEqual(kw["env"]["LC_ALL"], "C")
        self.assertFalse(kw.get("shell", False))

    def test_ps_missing_is_unavailable(self):
        self.assertIsNone(L.PsProbe(runner=FakeRun(exc=FileNotFoundError("ps"))).identity(4242))

    def test_ps_timeout_is_unavailable(self):
        exc = subprocess.TimeoutExpired(["ps"], 2.0)
        self.assertIsNone(L.PsProbe(runner=FakeRun(exc=exc)).identity(4242))

    def test_ps_garbage_is_unavailable(self):
        self.assertIsNone(L.PsProbe(runner=FakeRun(stdout="S\n")).identity(4242))
        self.assertIsNone(L.PsProbe(runner=FakeRun(stdout="", returncode=2)).identity(4242))

    def test_ps_no_such_pid_is_gone(self):
        self.assertIs(L.PsProbe(runner=FakeRun(stdout="", returncode=1)).identity(4242), L.GONE)


class DarwinSimulationTests(unittest.TestCase):
    """Force the darwin strategy on any host by patching sys.platform, ps, and os.kill."""

    def setUp(self):
        patches = [mock.patch.object(sys, "platform", "darwin"),
                   mock.patch.object(L.os, "kill", self.fake_kill)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.alive = {4242}
        self.eperm = set()
        self.run = FakeRun(stdout=f"S {LSTART_A}\n")
        p = mock.patch.object(L.subprocess, "run", self.run)
        p.start()
        self.addCleanup(p.stop)

    def fake_kill(self, pid, sig):
        if pid in self.eperm:
            raise PermissionError(1, "Operation not permitted")
        if pid not in self.alive:
            raise ProcessLookupError(3, "No such process")

    def test_platform_selects_strategy(self):
        self.assertIsInstance(L.default_probe(), L.PsProbe)
        for plat, cls in (("linux", L.ProcProbe), ("freebsd14", L.PsProbe), ("openbsd7", L.PsProbe),
                          ("win32", L.KillOnlyProbe)):
            with mock.patch.object(sys, "platform", plat):
                self.assertIsInstance(L.default_probe(), cls, plat)

    def test_capture_then_alive_same_identity(self):
        rec = {"pid": 4242, **L.capture_identity(4242)}
        self.assertEqual(rec["start_token"], LSTART_A)
        self.assertEqual(rec["start_method"], L.METHOD_PS)
        self.assertIsNone(rec["start_ticks"])  # key kept for older readers
        self.assertEqual(L.process_liveness(rec), L.ALIVE)

    def test_reused_pid(self):
        rec = {"pid": 4242, **L.capture_identity(4242)}
        self.run.stdout = f"S {LSTART_B}\n"
        self.assertEqual(L.process_liveness(rec), L.DEAD)

    def test_dead_pid(self):
        rec = {"pid": 4242, **L.capture_identity(4242)}
        self.alive.clear()
        self.assertEqual(L.process_liveness(rec), L.DEAD)

    def test_ps_unavailable_falls_back_to_kill(self):
        rec = {"pid": 4242, **L.capture_identity(4242)}
        self.run.exc = FileNotFoundError("ps")
        self.assertEqual(L.process_liveness(rec), L.UNVERIFIED)
        self.run.exc = subprocess.TimeoutExpired(["ps"], 2.0)
        self.assertEqual(L.process_liveness(rec), L.UNVERIFIED)
        self.alive.clear()
        self.assertEqual(L.process_liveness(rec), L.DEAD)

    def test_capture_when_ps_unavailable(self):
        self.run.exc = FileNotFoundError("ps")
        self.assertEqual(L.capture_identity(4242), {"start_ticks": None, "start_token": None, "start_method": None})

    def test_permission_error_means_alive(self):
        rec = {"pid": 4242, **L.capture_identity(4242)}
        self.eperm.add(4242)
        self.assertEqual(L.process_liveness(rec), L.ALIVE)

    def test_legacy_proc_record_on_darwin_is_unverified(self):
        self.assertEqual(L.process_liveness({"pid": 4242, "start_ticks": "12345"}), L.UNVERIFIED)


@unittest.skipUnless(sys.platform.startswith("linux") and os.path.isdir("/proc/self"), "needs Linux /proc")
class LinuxProcTests(unittest.TestCase):
    def test_own_process_verified(self):
        ident_fields = L.capture_identity(os.getpid())
        self.assertEqual(ident_fields["start_method"], L.METHOD_PROC)
        self.assertEqual(ident_fields["start_ticks"], ident_fields["start_token"])
        self.assertEqual(ident_fields["start_ticks"], L.process_start_ticks(os.getpid()))
        self.assertEqual(L.process_liveness({"pid": os.getpid(), **ident_fields}), L.ALIVE)
        self.assertEqual(L.process_liveness({"pid": os.getpid(), "start_ticks": "1"}), L.DEAD)

    def test_dead_pid(self):
        self.assertEqual(L.process_liveness({"pid": dead_pid()}), L.DEAD)


class ManagerResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        self.store = RunStore(self.tmp.name, clock=self.clock)

    def manager(self, **kw):
        return RunManager(self.store, clock=self.clock, id_gen=Ids("run"), lease_ttl=60, **kw)

    def start(self, mgr, uids):
        run_id = mgr.create_run({"units": [unit(u) for u in uids]}, conductor_id="c1")["data"]["run_id"]
        for uid in uids:
            self.assertTrue(mgr.assign_unit(run_id, "c1", uid, "w1")["ok"])
            self.assertTrue(mgr.transition(run_id, "c1", uid, "running")["ok"])
        return run_id

    def test_unverified_alive_is_flagged_needs_user_not_interrupted(self):
        mgr = self.manager(probe=FakeProbe(ident("T1")))
        run_id = self.start(mgr, ["a", "b", "c"])
        mgr.record_process(run_id, "c1", "a", {"pid": 4242})
        mgr.record_process(run_id, "c1", "b", {"pid": 4243})
        mgr.record_process(run_id, "c1", "c", {"pid": 4244})
        procs = {u: mgr.load(run_id)["units"][u]["process"] for u in "abc"}
        self.assertEqual(procs["a"]["start_token"], "T1")
        self.assertEqual(procs["a"]["start_method"], "fake")
        self.assertIn("start_ticks", procs["a"])
        self.clock.advance(120)

        statuses = {4242: L.ALIVE, 4243: L.UNVERIFIED, 4244: L.DEAD}
        mgr2 = self.manager(is_alive=lambda rec: statuses[rec["pid"]])
        res = mgr2.resume(run_id, "c2")
        self.assertTrue(res["ok"], res)
        run = mgr2.load(run_id)
        self.assertEqual(run["units"]["a"]["state"], "running")
        self.assertEqual(run["units"]["b"]["state"], "needs_user")
        self.assertEqual(run["units"]["b"]["last_reason"], "liveness_unverified")
        self.assertEqual(run["units"]["b"]["process"]["pid"], 4243)  # kept for the operator
        self.assertEqual(run["units"]["c"]["state"], "interrupted")
        self.assertEqual(res["data"]["interrupted"], ["c"])
        self.assertEqual(res["data"]["unverified"], ["b"])
        kinds = {(a["kind"], a["unit_id"]) for a in res["required_user_actions"]}
        self.assertIn(("verify_worker_process", "b"), kinds)
        self.assertIn(("reassign_interrupted_unit", "c"), kinds)
        self.assertNotIn(("reassign_interrupted_unit", "b"), kinds)

    def test_default_liveness_uses_injected_probe(self):
        probe = FakeProbe(ident("T1"))
        mgr = self.manager(probe=probe)
        run_id = self.start(mgr, ["a"])
        mgr.record_process(run_id, "c1", "a", {"pid": 4242})
        self.clock.advance(120)
        probe.ident = None  # ps went away / timed out after restart
        res = self.manager(probe=probe).resume(run_id, "c2")
        self.assertEqual(res["data"]["unverified"], ["a"])
        self.assertEqual(res["data"]["interrupted"], [])

    def test_bool_is_alive_still_supported(self):
        mgr = self.manager()
        run_id = self.start(mgr, ["a", "b"])
        mgr.record_process(run_id, "c1", "a", {"pid": 11, "start_ticks": "x"})
        mgr.record_process(run_id, "c1", "b", {"pid": 12, "start_ticks": "y"})
        self.clock.advance(120)
        res = self.manager(is_alive=lambda rec: rec["pid"] == 11).resume(run_id, "c2")
        self.assertEqual(res["data"]["interrupted"], ["b"])
        self.assertEqual(res["data"]["unverified"], [])
        # Caller-supplied legacy fields are not overwritten.
        self.assertNotIn("start_token", self.manager().load(run_id)["units"]["a"]["process"])


if __name__ == "__main__":
    unittest.main()
