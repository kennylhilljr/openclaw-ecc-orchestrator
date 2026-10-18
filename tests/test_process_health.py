"""Health monitor: hung means no progress signal moved for N checks; slow is fine."""

import os
import subprocess
import sys
import tempfile
import time
import unittest

from openclaw_ecc_orchestrator.process import health as H
from openclaw_ecc_orchestrator.process.supervisor import Supervisor

PY = sys.executable


class FakeClock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class ScriptedSampler:
    """Returns the next scripted sample; each entry is a dict of signals or
    the string "dead"."""

    def __init__(self, clock, samples):
        self.clock = clock
        self.samples = list(samples)

    def __call__(self):
        self.clock.t += 120
        spec = self.samples.pop(0)
        if spec == "dead":
            return H.Sample(alive=False, signals={}, at=self.clock.t)
        return H.Sample(alive=True, signals=dict(spec), at=self.clock.t)


def flat(cpu=1.0, out=10, events=2, wt="a", members=frozenset({1, 2})):
    return {"cpu": cpu, "output": out, "events": events, "worktree": wt, "members": members}


def monitor(samples, checks=3, baseline=None):
    """``samples`` are what the checks see; the baseline is ``flat()``."""
    clock = FakeClock()
    script = [baseline or flat()] + list(samples)
    mon = H.HealthMonitor(H.HealthSettings(120, checks), ScriptedSampler(clock, script), clock=clock)
    mon.start()
    return mon


class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        s = H.HealthSettings()
        self.assertEqual((s.interval_seconds, s.flat_checks_for_hang), (120.0, 5))

    def test_ranges_enforced(self):
        for interval, checks in ((29, 5), (1801, 5), (120, 1), (120, 21), (120, 2.5)):
            with self.subTest(interval=interval, checks=checks):
                with self.assertRaises(H.HealthSettingsError):
                    H.HealthSettings(interval, checks)
        H.HealthSettings(30, 2)
        H.HealthSettings(1800, 20)

    def test_test_override_allows_short_intervals_only_explicitly(self):
        s = H.HealthSettings.for_tests(0.1, 2)
        self.assertEqual(s.interval_seconds, 0.1)
        with self.assertRaises(H.HealthSettingsError):
            H.HealthSettings(0.1, 2)
        with self.assertRaises(H.HealthSettingsError):
            H.HealthSettings.for_tests(0, 2)

    def test_resolve_policy_then_unit_then_override(self):
        policy = {"health": {"interval_seconds": 300, "flat_checks_for_hang": 4}}
        unit = {"health": {"flat_checks_for_hang": 8}}
        s = H.resolve_health_settings(policy, unit)
        self.assertEqual((s.interval_seconds, s.flat_checks_for_hang), (300.0, 8))
        self.assertEqual(H.resolve_health_settings(None, None), H.HealthSettings())
        override = H.HealthSettings.for_tests(0.5, 2)
        self.assertIs(H.resolve_health_settings(policy, unit, override), override)
        with self.assertRaises(H.HealthSettingsError):
            H.resolve_health_settings({"health": {"interval_seconds": 5}})

    def test_block_errors(self):
        self.assertEqual(H.health_block_errors({"interval_seconds": 60, "flat_checks_for_hang": 3}), [])
        self.assertTrue(H.health_block_errors({"interval_seconds": True}))
        self.assertTrue(H.health_block_errors({"other": 1}))
        self.assertTrue(H.health_block_errors([]))


class MonitorTests(unittest.TestCase):
    def test_busy_cpu_without_output_is_healthy(self):
        mon = monitor([flat(cpu=float(i * 50)) for i in range(1, 8)])
        states = [mon.check().state for _ in range(6)]
        self.assertEqual(set(states), {H.HEALTHY})

    def test_steady_output_is_healthy(self):
        mon = monitor([flat(out=10 + i * 5) for i in range(1, 8)])
        self.assertEqual({mon.check().state for _ in range(6)}, {H.HEALTHY})

    def test_worktree_changes_are_healthy(self):
        mon = monitor([flat(wt="wt%d" % i) for i in range(1, 8)])
        self.assertEqual({mon.check().state for _ in range(6)}, {H.HEALTHY})

    def test_child_set_changes_are_healthy(self):
        mon = monitor([flat(members=frozenset({1, i})) for i in range(10, 17)])
        self.assertEqual({mon.check().state for _ in range(6)}, {H.HEALTHY})

    def test_fully_flat_for_n_checks_is_hung(self):
        mon = monitor([flat()] * 4, checks=3)
        first = mon.check()
        self.assertEqual((first.state, first.changed, first.flat_checks), (H.SUSPECTED_HANG, True, 1))
        second = mon.check()
        self.assertEqual((second.state, second.changed), (H.SUSPECTED_HANG, False))
        third = mon.check()
        self.assertTrue(third.hung)
        self.assertTrue(third.changed)
        self.assertEqual(third.evidence["flat_checks"], 3)
        self.assertEqual(third.evidence["moved"], [])
        self.assertEqual(third.evidence["cpu_delta_seconds"], 0.0)
        self.assertEqual(third.evidence["output_bytes_delta"], 0)

    def test_one_moving_signal_resets(self):
        samples = [flat(), flat(), flat(out=11), flat(out=11), flat(out=11)]
        mon = monitor(samples, checks=3)
        self.assertEqual(mon.check().flat_checks, 1)
        self.assertEqual(mon.check().flat_checks, 2)
        moved = mon.check()
        self.assertEqual((moved.state, moved.flat_checks, moved.moved, moved.changed),
                         (H.HEALTHY, 0, ("output",), True))
        self.assertEqual(mon.check().flat_checks, 1)
        self.assertFalse(mon.check().hung)

    def test_cpu_noise_floor(self):
        # 0.05 CPU seconds over a 120 second interval is idle timer noise.
        mon = monitor([flat(cpu=1.05), flat(cpu=1.1), flat(cpu=1.15)], checks=3)
        self.assertTrue([mon.check() for _ in range(3)][-1].hung)

    def test_dead_process_is_not_hung(self):
        mon2 = monitor([flat(), "dead", "dead", "dead"], checks=2)
        self.assertEqual(mon2.check().state, H.SUSPECTED_HANG)
        for _ in range(3):
            result = mon2.check()
            self.assertFalse(result.hung)
            self.assertFalse(result.alive)
            self.assertFalse(result.flat)

    def test_unknown_signal_is_indeterminate(self):
        samples = [flat(), dict(flat(), cpu=H.UNKNOWN), flat(), flat()]
        mon = monitor(samples, checks=2)
        self.assertEqual(mon.check().flat_checks, 1)
        undecided = mon.check()
        self.assertEqual((undecided.flat, undecided.flat_checks, undecided.unknown), (False, 1, ("cpu",)))
        self.assertEqual(mon.check().flat_checks, 1)  # previous cpu was unknown: indeterminate again
        self.assertTrue(mon.check().hung)

    def test_absent_sources_are_ignored(self):
        clock = FakeClock()
        sampler = ScriptedSampler(clock, [{"cpu": 1.0}] * 3)
        mon = H.HealthMonitor(H.HealthSettings(120, 2), sampler, clock=clock)
        mon.start()
        mon.check()
        self.assertTrue(mon.check().hung)

    def test_evidence_has_no_paths_or_commands(self):
        mon = monitor([flat()] * 2)
        evidence = mon.check().evidence
        self.assertEqual(set(evidence) - {
            "checked_at", "alive", "interval_seconds", "flat_checks_for_hang", "cpu_seconds",
            "cpu_delta_seconds", "output_bytes_total", "output_bytes_delta", "events_total", "events_delta",
            "group_processes", "group_changed", "worktree_changed", "flat", "flat_checks", "moved",
            "unknown"}, set())


class SignalSourceTests(unittest.TestCase):
    def test_parse_cpu_time_forms(self):
        self.assertEqual(H.parse_cpu_time("00:00:07"), 7.0)
        self.assertEqual(H.parse_cpu_time("1-02:03:04"), 86400 + 7384)
        self.assertAlmostEqual(H.parse_cpu_time("0:01.25"), 1.25)
        self.assertAlmostEqual(H.parse_cpu_time("123:45.50"), 123 * 60 + 45.5)
        with self.assertRaises(ValueError):
            H.parse_cpu_time("x")

    def test_ps_snapshot_filters_group_and_zombies(self):
        out = ("  100   100 Ss    0:01.50\n  101   100 S+    0:00.25\n  102   100 Z     0:03.00\n"
               "  200   200 R     9:00.00\n")

        def runner(argv, **kw):
            self.assertEqual(argv[:2], ["ps", "-A"])
            self.assertEqual(kw["env"]["LC_ALL"], "C")
            self.assertIn("timeout", kw)
            return subprocess.CompletedProcess(argv, 0, out, "")
        snap = H.group_snapshot(100, platform="darwin", ps_runner=runner)
        self.assertAlmostEqual(snap.cpu_seconds, 4.75)
        self.assertEqual(snap.members, frozenset({100, 101}))

    def test_ps_failure_raises(self):
        runner = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "")
        with self.assertRaises(OSError):
            H.group_snapshot(1, platform="darwin", ps_runner=runner)

    def test_real_group_snapshot_sees_self_group(self):
        proc = subprocess.Popen([PY, "-c", "import time; time.sleep(5)"], start_new_session=True)
        try:
            snap = H.group_snapshot(proc.pid)
            self.assertIn(proc.pid, snap.members)
            self.assertGreaterEqual(snap.cpu_seconds, 0.0)
        finally:
            proc.kill()
            proc.wait()

    def test_porcelain_paths(self):
        out = ("# branch.oid abc\0# branch.head main\0"
               "1 .M N... 100644 100644 100644 a b src/x y.py\0"
               "2 R. N... 100644 100644 100644 a b R100 new.py\0old.py\0"
               "? untracked file.txt\0")
        self.assertEqual(H._porcelain_paths(out), ["src/x y.py", "new.py", "untracked file.txt"])

    def test_worktree_digest_changes_with_edits_and_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args):
                subprocess.run(["git", *args], cwd=tmp, check=True, capture_output=True)
            git("init", "-q")
            git("config", "user.email", "t@example.invalid")
            git("config", "user.name", "t")
            path = os.path.join(tmp, "f.txt")
            with open(path, "w") as fh:
                fh.write("a\n")
            first = H.worktree_digest(tmp)
            self.assertEqual(first, H.worktree_digest(tmp))
            with open(path, "a") as fh:
                fh.write("b\n")
            second = H.worktree_digest(tmp)
            self.assertNotEqual(first, second)
            git("add", "f.txt")
            git("commit", "-q", "-m", "x")
            self.assertNotEqual(second, H.worktree_digest(tmp))


class RealProcessTests(unittest.TestCase):
    """Short intervals are allowed only through the test override."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.sup = Supervisor(base_env={"PATH": os.environ.get("PATH", "")}, grace=0.5)

    def tearDown(self):
        self._tmp.cleanup()

    def run_child(self, code, interval=0.25, checks=3):
        seen = []
        cfg = H.HealthConfig(settings=H.HealthSettings.for_tests(interval, checks), on_check=seen.append)
        start = time.monotonic()
        res = self.sup.run([PY, "-c", code], cwd=self.tmp, health=cfg)
        return res, seen, time.monotonic() - start

    def test_sleeping_silent_child_becomes_hung(self):
        res, seen, took = self.run_child("import time; time.sleep(60)")
        self.assertLess(took, 20)
        self.assertTrue(res["hung"], res)
        self.assertTrue(res["cancelled"])
        self.assertEqual(res["cancel_reason"], "hung")
        self.assertEqual(res["health"]["state"], H.HUNG)
        states = [r.state for r in seen if r.changed]
        self.assertEqual(states, [H.SUSPECTED_HANG, H.HUNG])
        self.assertEqual(seen[-1].evidence["flat_checks"], 3)

    def test_cpu_busy_child_is_not_hung(self):
        code = "import time\nend = time.time() + 2.5\nx = 0\nwhile time.time() < end:\n    x += 1\n"
        res, seen, _ = self.run_child(code)
        self.assertFalse(res["hung"], res)
        self.assertEqual(res["exit_code"], 0)
        self.assertNotIn(H.HUNG, [r.state for r in seen])

    def test_periodically_printing_child_is_not_hung(self):
        code = ("import time\nfor i in range(25):\n    print('tick', i, flush=True)\n    time.sleep(0.1)\n")
        res, seen, _ = self.run_child(code)
        self.assertFalse(res["hung"], res)
        self.assertEqual(res["exit_code"], 0)
        self.assertGreater(res["bytes"]["stdout"], 100)
        self.assertNotIn(H.HUNG, [r.state for r in seen])

    def test_partial_line_output_counts_as_progress(self):
        code = ("import sys, time\nfor i in range(25):\n    sys.stdout.write('.')\n    sys.stdout.flush()\n"
                "    time.sleep(0.1)\n")
        res, _, _ = self.run_child(code)
        self.assertFalse(res["hung"], res)
        self.assertEqual(res["bytes"]["stdout"], 25)

    def test_child_that_exits_is_not_hung(self):
        res, seen, _ = self.run_child("import time; time.sleep(0.3)", interval=0.2, checks=2)
        self.assertFalse(res["hung"])
        self.assertEqual(res["exit_code"], 0)

    def test_no_wall_clock_limit_by_default(self):
        res = self.sup.run([PY, "-c", "import time; time.sleep(0.5)"], cwd=self.tmp)
        self.assertFalse(res["timed_out"])
        self.assertFalse(res["hung"])
        self.assertIsNone(res["health"])


if __name__ == "__main__":
    unittest.main()
