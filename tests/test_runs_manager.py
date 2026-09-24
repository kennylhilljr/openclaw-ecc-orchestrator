import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from openclaw_ecc_orchestrator.runs.manager import LeaseError, RunError, RunManager
from openclaw_ecc_orchestrator.runs.state import IllegalTransition, check_transition
from openclaw_ecc_orchestrator.runs.store import RunStore

try:
    from ._units import work_unit
except ImportError:  # discovered as a top level module
    from _units import work_unit


class FakeClock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class Ids:
    def __init__(self, prefix="id"):
        self.n = 0
        self.prefix = prefix

    def __call__(self):
        self.n += 1
        return f"{self.prefix}{self.n}"


def unit(uid, deps=(), files=None, budget=None):
    return work_unit(uid, deps=deps, files=files, budget=budget)


def handoff(uid, files=None, sha="0123456789abcdef0123456789abcdef01234567"):
    return {
        "schema_version": "1.0", "unit_id": uid, "status": "succeeded", "outcome": "done",
        "files_changed": list(files or [f"src/{uid}.py"]), "behavior": "b",
        "commands": [{"command": "python3 -c pass", "exit_code": 0, "result": "ok"}],
        "unresolved_failures": [], "assumptions": [], "risks": [], "next_action": "review",
        "commit": {"sha": sha, "branch": f"ecc/r/{uid}", "worktree": "."}, "usage": None,
        "user_input_required": False,
    }


def read(path):
    with open(path) as fh:
        return fh.read()


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class StateMachineTests(unittest.TestCase):
    def test_legal_and_illegal(self):
        check_transition("pending", "ready")
        check_transition("running", "verifying")
        for frm, to in [("pending", "running"), ("merged", "ready"), ("cancelled", "ready"), ("ready", "merged"), ("running", "merged")]:
            with self.assertRaises(IllegalTransition):
                check_transition(frm, to)
        with self.assertRaises(IllegalTransition):
            check_transition("pending", "bogus")


class ManagerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.ids = Ids("run")
        self.store = RunStore(self.tmp.name, clock=self.clock)
        self.mgr = self.new_manager()

    def new_manager(self, is_alive=None):
        return RunManager(self.store, clock=self.clock, id_gen=self.ids, is_alive=is_alive, lease_ttl=60)

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, units, conductor="c1"):
        res = self.mgr.create_run({"units": units}, conductor_id=conductor)
        self.assertTrue(res["ok"], res)
        return res["data"]["run_id"]

    def drive(self, run_id, uid, states, conductor="c1", worker="w1"):
        for st in states:
            if st == "assigned":
                res = self.mgr.assign_unit(run_id, conductor, uid, worker)
            elif st == "verifying":
                self.assertTrue(self.mgr.record_handoff(run_id, conductor, uid, handoff(uid))["ok"])
                res = self.mgr.transition(run_id, conductor, uid, st)
            else:
                res = self.mgr.transition(run_id, conductor, uid, st)
            self.assertTrue(res["ok"], res)


class CreateRunTests(ManagerBase):
    def test_create_marks_roots_ready(self):
        run_id = self.create([unit("a"), unit("b", ["a"])])
        run = self.mgr.load(run_id)
        self.assertEqual(run["schema_version"], "1.0")
        self.assertEqual(run["units"]["a"]["state"], "ready")
        self.assertEqual(run["units"]["b"]["state"], "pending")
        self.assertEqual(run["layers"], [["a"], ["b"]])
        self.assertEqual(run["lease"]["conductor_id"], "c1")
        self.assertEqual(len(run["plan_sha256"]), 64)
        types = [e["type"] for e in self.store.read_events(run_id)]
        self.assertEqual(types[0], "run.created")

    def test_create_dry_run_does_not_write(self):
        res = self.mgr.create_run({"units": [unit("a")]}, conductor_id="c1", dry_run=True)
        self.assertTrue(res["ok"])
        self.assertFalse(res["changed"])
        self.assertEqual(res["data"]["layers"], [["a"]])
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_invalid_plan_rejected(self):
        res = self.mgr.create_run({"units": [unit("a", ["b"]), unit("b", ["a"])]}, conductor_id="c1")
        self.assertFalse(res["ok"])
        self.assertEqual(os.listdir(self.tmp.name), [])


class TransitionTests(ManagerBase):
    def test_happy_path_and_dependency_promotion(self):
        run_id = self.create([unit("a"), unit("b", ["a"])])
        self.drive(run_id, "a", ["assigned", "running", "verifying", "reviewing", "queued_for_merge", "merged"])
        run = self.mgr.load(run_id)
        self.assertEqual(run["units"]["a"]["state"], "merged")
        self.assertEqual(run["units"]["b"]["state"], "ready")
        hist = run["units"]["a"]["history"]
        self.assertEqual([h["to"] for h in hist][-1], "merged")
        self.assertTrue(all(h["conductor_id"] == "c1" for h in hist))
        self.assertEqual(run["units"]["a"]["owner"], "w1")

    def test_illegal_transition_rejected_and_not_persisted(self):
        run_id = self.create([unit("a")])
        before = len(self.store.read_events(run_id))
        res = self.mgr.transition(run_id, "c1", "a", "merged")
        self.assertFalse(res["ok"])
        self.assertEqual(self.mgr.load(run_id)["units"]["a"]["state"], "ready")
        self.assertEqual(len(self.store.read_events(run_id)), before)

    def test_transition_dry_run(self):
        run_id = self.create([unit("a")])
        res = self.mgr.assign_unit(run_id, "c1", "a", "w1", dry_run=True)
        self.assertTrue(res["ok"])
        self.assertFalse(res["changed"])
        self.assertEqual(self.mgr.load(run_id)["units"]["a"]["state"], "ready")

    def test_unknown_unit(self):
        run_id = self.create([unit("a")])
        self.assertFalse(self.mgr.transition(run_id, "c1", "zzz", "cancelled")["ok"])


class LeaseTests(ManagerBase):
    def test_other_conductor_blocked_until_expiry(self):
        run_id = self.create([unit("a")])
        res = self.mgr.assign_unit(run_id, "c2", "a", "w1")
        self.assertFalse(res["ok"])
        self.assertIn("lease", res["checks"][0]["name"])
        self.assertFalse(self.mgr.acquire_lease(run_id, "c2")["ok"])
        self.clock.advance(61)
        self.assertTrue(self.mgr.acquire_lease(run_id, "c2")["ok"])
        self.assertTrue(self.mgr.assign_unit(run_id, "c2", "a", "w1")["ok"])
        self.assertFalse(self.mgr.transition(run_id, "c1", "a", "running")["ok"])

    def test_explicit_transfer_records_handoff(self):
        run_id = self.create([unit("a")])
        res = self.mgr.transfer_lease(run_id, "c1", "c2")
        self.assertTrue(res["ok"], res)
        run = self.mgr.load(run_id)
        self.assertEqual(run["lease"]["conductor_id"], "c2")
        handoff = run["handoffs"][-1]
        self.assertEqual((handoff["kind"], handoff["from"], handoff["to"]), ("conductor", "c1", "c2"))
        self.assertFalse(self.mgr.transfer_lease(run_id, "c1", "c3")["ok"])

    def test_renew_extends_expiry(self):
        run_id = self.create([unit("a")])
        self.clock.advance(50)
        self.assertTrue(self.mgr.renew_lease(run_id, "c1")["ok"])
        self.clock.advance(50)
        self.assertTrue(self.mgr.assign_unit(run_id, "c1", "a", "w1")["ok"])

    def test_require_lease_helper(self):
        run_id = self.create([unit("a")])
        with self.assertRaises(LeaseError):
            self.mgr.require_lease(self.mgr.load(run_id), "c9")


class ResumeTests(ManagerBase):
    def test_resume_marks_dead_process_interrupted_and_keeps_live(self):
        run_id = self.create([unit("a"), unit("b")])
        self.drive(run_id, "a", ["assigned", "running"])
        self.drive(run_id, "b", ["assigned", "running"], worker="w2")
        self.mgr.record_process(run_id, "c1", "a", {"pid": dead_pid()})
        self.mgr.record_process(run_id, "c1", "b", {"pid": os.getpid()})
        self.clock.advance(120)
        mgr2 = self.new_manager()
        res = mgr2.resume(run_id, "c2")
        self.assertTrue(res["ok"], res)
        run = mgr2.load(run_id)
        self.assertEqual(run["units"]["a"]["state"], "interrupted")
        self.assertEqual(run["units"]["b"]["state"], "running")
        self.assertEqual(run["lease"]["conductor_id"], "c2")
        self.assertEqual(res["data"]["interrupted"], ["a"])
        kinds = [a["kind"] for a in res["required_user_actions"]]
        self.assertIn("reassign_interrupted_unit", kinds)

    def test_resume_refused_while_other_lease_valid(self):
        run_id = self.create([unit("a")])
        res = self.new_manager().resume(run_id, "c2")
        self.assertFalse(res["ok"])

    def test_resume_dry_run_does_not_mutate(self):
        run_id = self.create([unit("a")])
        self.drive(run_id, "a", ["assigned", "running"])
        self.clock.advance(120)
        before = read(self.store.snapshot_path(run_id))
        events = len(self.store.read_events(run_id))
        res = self.new_manager().resume(run_id, "c2", dry_run=True)
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["interrupted"], ["a"])
        self.assertEqual(read(self.store.snapshot_path(run_id)), before)
        self.assertEqual(len(self.store.read_events(run_id)), events)

    def test_resume_replays_log_after_crash_before_snapshot(self):
        run_id = self.create([unit("a")])
        with mock.patch.object(self.store, "save", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                self.mgr.assign_unit(run_id, "c1", "a", "w1")
        self.assertEqual(self.store.load(run_id)["units"]["a"]["state"], "ready")
        run = self.new_manager().load(run_id)
        self.assertEqual(run["units"]["a"]["state"], "assigned")

    def test_rebuild_from_log_when_snapshot_lost(self):
        run_id = self.create([unit("a")])
        self.drive(run_id, "a", ["assigned", "running"])
        os.unlink(self.store.snapshot_path(run_id))
        run = self.new_manager().load(run_id)
        self.assertEqual(run["units"]["a"]["state"], "running")


class ReassignTests(ManagerBase):
    def test_reassign_records_handoff(self):
        run_id = self.create([unit("a")])
        self.drive(run_id, "a", ["assigned", "running", "interrupted"])
        self.mgr.record_workspace(run_id, "c1", "a", {"branch": "ecc/r/a", "worktree": "/wt/a"})
        res = self.mgr.reassign_unit(run_id, "c1", "a", "w2", reason="worker lost")
        self.assertTrue(res["ok"], res)
        run = self.mgr.load(run_id)
        u = run["units"]["a"]
        self.assertEqual(u["state"], "assigned")
        self.assertEqual(u["owner"], "w2")
        handoff = [h for h in run["handoffs"] if h["id"] == u["handoff_ref"]][0]
        self.assertEqual((handoff["kind"], handoff["from"], handoff["to"]), ("unit", "w1", "w2"))
        self.assertEqual(handoff["workspace"]["branch"], "ecc/r/a")
        self.assertEqual(handoff["previous_state"], "interrupted")

    def test_silent_takeover_refused(self):
        run_id = self.create([unit("a")])
        self.drive(run_id, "a", ["assigned", "running", "failed", "ready"])
        res = self.mgr.assign_unit(run_id, "c1", "a", "w2")
        self.assertFalse(res["ok"])
        self.assertTrue(self.mgr.assign_unit(run_id, "c1", "a", "w1")["ok"])

    def test_reassign_refused_for_running_unit(self):
        run_id = self.create([unit("a")])
        self.drive(run_id, "a", ["assigned", "running"])
        self.assertFalse(self.mgr.reassign_unit(run_id, "c1", "a", "w2", reason="x")["ok"])


class BudgetTests(ManagerBase):
    def test_attempt_budget(self):
        run_id = self.create([unit("a", budget={"attempts": 1})])
        self.drive(run_id, "a", ["assigned", "running", "failed", "ready"])
        res = self.mgr.assign_unit(run_id, "c1", "a", "w1")
        self.assertFalse(res["ok"])
        self.assertEqual(self.mgr.load(run_id)["units"]["a"]["state"], "needs_user")
        self.assertEqual(res["required_user_actions"][0]["kind"], "budget_exhausted")

    def test_minutes_budget(self):
        run_id = self.create([unit("a", budget={"minutes": 10})])
        self.drive(run_id, "a", ["assigned", "running"])
        self.assertTrue(self.mgr.record_usage(run_id, "c1", "a", minutes=6)["ok"])
        res = self.mgr.record_usage(run_id, "c1", "a", minutes=6)
        self.assertFalse(res["ok"])
        u = self.mgr.load(run_id)["units"]["a"]
        self.assertEqual(u["state"], "needs_user")
        self.assertEqual(u["minutes_used"], 12)


class ResumeScopeTests(ManagerBase):
    """Restart must not interrupt units that are already past the runner."""

    def test_resume_leaves_post_runner_units_untouched(self):
        run_id = self.create([unit("v"), unit("r"), unit("q")])
        for uid, states in (("v", ["assigned", "running", "verifying"]),
                            ("r", ["assigned", "running", "verifying", "reviewing"]),
                            ("q", ["assigned", "running", "verifying", "reviewing", "queued_for_merge"])):
            self.drive(run_id, uid, states[:2])
            self.mgr.record_process(run_id, "c1", uid, {"pid": dead_pid()})
            self.drive(run_id, uid, states[2:])
        self.clock.advance(120)
        res = self.new_manager().resume(run_id, "c2")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["data"]["interrupted"], [])
        states = {u: s["state"] for u, s in self.mgr.load(run_id)["units"].items()}
        self.assertEqual(states, {"v": "verifying", "r": "reviewing", "q": "queued_for_merge"})

    def test_runner_exit_marks_process_finished(self):
        run_id = self.create([unit("a")])
        self.drive(run_id, "a", ["assigned", "running"])
        self.mgr.record_process(run_id, "c1", "a", {"pid": dead_pid()})
        res = self.mgr.record_process_exit(run_id, "c1", "a", {"exit_code": 0, "signal": None})
        self.assertTrue(res["ok"], res)
        proc = self.mgr.load(run_id)["units"]["a"]["process"]
        self.assertTrue(proc["finished"])
        self.assertEqual(proc["exit_code"], 0)
        # A running unit whose runner already exited has nothing left to wait for.
        self.clock.advance(120)
        seen = []
        res = self.new_manager(is_alive=lambda rec: seen.append(rec) or True).resume(run_id, "c2")
        self.assertEqual(seen, [])
        self.assertEqual(res["data"]["interrupted"], ["a"])

    def test_assigned_without_process_is_interrupted(self):
        run_id = self.create([unit("a")])
        self.drive(run_id, "a", ["assigned"])
        self.clock.advance(120)
        res = self.new_manager().resume(run_id, "c2")
        self.assertEqual(res["data"]["interrupted"], ["a"])


class ConcurrentCreateTests(ManagerBase):
    def test_duplicate_run_id_returns_envelope(self):
        self.assertTrue(self.mgr.create_run({"units": [unit("a")]}, conductor_id="c1", run_id="dup")["ok"])
        res = self.new_manager().create_run({"units": [unit("a")]}, conductor_id="c1", run_id="dup")
        self.assertFalse(res["ok"])
        self.assertIn("run_id_unique", [c["name"] for c in res["checks"] if not c["ok"]])

    def test_parallel_creates_one_winner(self):
        import multiprocessing
        ctx = multiprocessing.get_context("fork")
        with ctx.Pool(4) as pool:
            results = pool.map(_create_in_child, [self.tmp.name] * 4)
        self.assertEqual(sorted(results), [False, False, False, True])


def _create_in_child(root):
    mgr = RunManager(RunStore(root), lease_ttl=60)
    try:
        return mgr.create_run({"units": [unit("a")]}, conductor_id="c1", run_id="race")["ok"]
    except Exception as exc:  # the regression: StoreError escaped
        return type(exc).__name__


class LegacyBudgetAliasTests(ManagerBase):
    def test_persisted_legacy_budget_still_enforced(self):
        run_id = self.create([unit("a")])
        # Simulate a run persisted before the budget rename.
        path = self.store.snapshot_path(run_id)
        import json
        with open(path) as fh:
            doc = json.load(fh)
        doc["units"]["a"]["budget"] = {"max_attempts": 1, "max_minutes": 5}
        with open(path, "w") as fh:
            json.dump(doc, fh)
        self.drive(run_id, "a", ["assigned", "running", "failed", "ready"])
        res = self.mgr.assign_unit(run_id, "c1", "a", "w1")
        self.assertFalse(res["ok"])
        self.assertEqual(res["required_user_actions"][0]["kind"], "budget_exhausted")


class EmitterTests(ManagerBase):
    def test_emits_external_events(self):
        seen = []

        class Sink:
            def emit(self, event_type, run_id, unit_id=None, data=None):
                seen.append((event_type, unit_id, dict(data or {})))

        mgr = RunManager(self.store, clock=self.clock, id_gen=self.ids, emitter=Sink())
        run_id = mgr.create_run({"units": [unit("a")]}, conductor_id="c1")["data"]["run_id"]
        mgr.assign_unit(run_id, "c1", "a", "w1")
        types = [s[0] for s in seen]
        self.assertEqual(types[0], "run.created")
        changed = [s for s in seen if s[0] == "unit.state_changed"]
        self.assertEqual(changed[-1][2]["to"], "assigned")


if __name__ == "__main__":
    unittest.main()
