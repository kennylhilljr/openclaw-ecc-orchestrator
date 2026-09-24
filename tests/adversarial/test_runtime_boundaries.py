"""Adversarial checks against the runtime's trust boundaries."""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest

from openclaw_ecc_orchestrator.runs.liveness import capture_identity
from unittest import mock

from openclaw_ecc_orchestrator.gates.runner import run_gates
from openclaw_ecc_orchestrator.plugin.approvals import ApprovalBroker
from openclaw_ecc_orchestrator.plugin.events import JsonlEventSink
from openclaw_ecc_orchestrator.process.supervisor import Supervisor
from openclaw_ecc_orchestrator.runs.manager import RunManager
from openclaw_ecc_orchestrator.runs.store import RunStore, StoreError
from openclaw_ecc_orchestrator.worktrees.manager import WorktreeError, WorktreeManager

PY = sys.executable


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


try:
    from .._units import work_unit
except (ImportError, ValueError):  # discovered with tests/ as the top level
    from _units import work_unit


def unit(uid, files=("x.py",)):
    # create_run validates every unit with schemas.validate_work_unit.
    return work_unit(uid, files=list(files))


class Clock:
    t = 1_700_000_000.0

    def __call__(self):
        return self.t


class RunStateAttacks(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "runs")
        self.clock = Clock()
        self.mgr = RunManager(RunStore(self.root, clock=self.clock), clock=self.clock, lease_ttl=60)

    def tearDown(self):
        self._tmp.cleanup()

    def test_path_hostile_unit_ids_rejected_at_run_creation(self):
        for bad in ["../../etc", "a/b", ".hidden", "x" * 300, "a b"]:
            res = self.mgr.create_run({"units": [unit(bad)]}, conductor_id="c1")
            self.assertFalse(res["ok"], bad)
        self.assertFalse(os.path.exists(self.root) and os.listdir(self.root))

    def test_run_id_traversal(self):
        res = self.mgr.create_run({"units": [unit("a")]}, conductor_id="c1", run_id="../escape")
        self.assertFalse(res["ok"])
        with self.assertRaises(StoreError):
            RunStore(self.root).run_dir("../escape")
        self.assertFalse(os.path.exists(os.path.join(self._tmp.name, "escape")))

    def test_corrupt_snapshot_falls_back_to_log(self):
        run_id = self.mgr.create_run({"units": [unit("a")]}, conductor_id="c1", run_id="r1")["data"]["run_id"]
        self.mgr.assign_unit(run_id, "c1", "a", "w1")
        with open(self.mgr.store.snapshot_path(run_id), "w") as fh:
            fh.write("{not json")
        self.assertEqual(self.mgr.load(run_id)["units"]["a"]["state"], "assigned")

    def test_expired_conductor_cannot_act_or_transfer(self):
        run_id = self.mgr.create_run({"units": [unit("a")]}, conductor_id="c1", run_id="r1")["data"]["run_id"]
        self.clock.t += 61
        self.assertFalse(self.mgr.transfer_lease(run_id, "c1", "evil")["ok"])
        self.assertFalse(self.mgr.assign_unit(run_id, "c1", "a", "w1")["ok"])
        self.assertFalse(self.mgr.record_process(run_id, "c1", "a", {"pid": 1})["ok"])
        self.assertFalse(self.mgr.renew_lease(run_id, "c1")["ok"])

    def test_forged_process_record_pid_reuse(self):
        run_id = self.mgr.create_run({"units": [unit("a")]}, conductor_id="c1", run_id="r1")["data"]["run_id"]
        self.mgr.assign_unit(run_id, "c1", "a", "w1")
        self.mgr.transition(run_id, "c1", "a", "running")
        # Our own pid is alive, but the recorded start time belongs to another process.
        # Forge the token in the identity method this platform actually uses
        # (/proc on Linux, ps lstart on macOS) so the mismatch is detectable.
        ident = capture_identity(os.getpid())
        if not ident.get("start_method"):
            self.skipTest("no process identity method on this platform")
        self.mgr.record_process(run_id, "c1", "a", {
            "pid": os.getpid(), "start_token": "1", "start_method": ident["start_method"],
            "start_ticks": "1" if ident["start_ticks"] is not None else None})
        self.clock.t += 61
        res = self.mgr.resume(run_id, "c2")
        self.assertEqual(res["data"]["interrupted"], ["a"])


class ApprovalAttacks(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.broker = ApprovalBroker(os.path.join(self._tmp.name, "a.json"), clock=self.clock)

    def tearDown(self):
        self._tmp.cleanup()

    def test_cross_task_replay(self):
        plan = "c" * 64
        r1 = self.broker.request(run_id="r1", unit_id="u1", action="merge", plan_sha256=plan, session_id="alice")["data"]
        r2 = self.broker.request(run_id="r2", unit_id="u1", action="merge", plan_sha256=plan, session_id="bob")["data"]
        d1 = {k: r1[k] for k in ("request_id", "run_id", "unit_id", "action", "plan_sha256", "session_id")}
        d1.update(decision="approved", decided_by="alice")
        self.assertTrue(self.broker.resolve(d1)["ok"])
        # Replaying alice's decision onto bob's pending request must fail.
        self.assertFalse(self.broker.resolve(dict(d1, request_id=r2["request_id"]))["ok"])
        self.assertEqual(self.broker.get(r2["request_id"])["status"], "pending")

    def test_tampered_state_file_cannot_resurrect(self):
        plan = "d" * 64
        req = self.broker.request(run_id="r1", unit_id="u1", action="merge", plan_sha256=plan, session_id="s")["data"]
        d = {k: req[k] for k in ("request_id", "run_id", "unit_id", "action", "plan_sha256", "session_id")}
        d.update(decision="approved", decided_by="op")
        self.broker.resolve(d)
        self.clock.t += 10 ** 6
        self.assertFalse(self.broker.resolve(d)["ok"])

    def test_non_string_fields(self):
        self.assertFalse(self.broker.resolve({"request_id": ["x"], "decision": "approved"})["ok"])
        self.assertFalse(self.broker.request(run_id="r1", unit_id="u1", action="merge", plan_sha256="short",
                                             session_id="s")["ok"])


class ProcessAttacks(unittest.TestCase):
    def test_parent_credentials_not_inherited_by_default(self):
        leaked = {"GITHUB_TOKEN": "ghs_" + "q" * 36, "AWS_SECRET_ACCESS_KEY": "aws-secret-xyz-123",  # gitleaks:allow
                  "SSH_AUTH_SOCK": "/tmp/agent.sock", "OPENAI_API_KEY": "sk-" + "p" * 40}
        with mock.patch.dict(os.environ, leaked):
            sup = Supervisor()
            names = []
            with tempfile.TemporaryDirectory() as d:
                sup.run([PY, "-c", "import os; print(' '.join(sorted(os.environ)))"], cwd=d,
                        on_line=lambda s, l: names.extend(l.split()))
        for name in leaked:
            self.assertNotIn(name, names)

    def test_gate_string_cannot_chain_commands(self):
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "pwned")
            cmds = [f"{shlex.quote(PY)} -c 'pass'; touch {marker}",
                    f"{shlex.quote(PY)} -c \"import os\" `touch {marker}`",
                    f"{shlex.quote(PY)} -c pass $(touch {marker})"]
            res = run_gates(unit("u1") | {"acceptance": {"commands": cmds}}, d,
                            supervisor=Supervisor(base_env={"PATH": os.environ.get("PATH", "")}))
            self.assertFalse(os.path.exists(marker))

    def test_secret_in_argv_redacted_in_records(self):
        with tempfile.TemporaryDirectory() as d:
            res = Supervisor(base_env={"PATH": os.environ.get("PATH", "")}).run(
                [PY, "-c", "pass", "--token=abcdef1234567890"], cwd=d, status_path=os.path.join(d, "s.json"))  # gitleaks:allow
            with open(os.path.join(d, "s.json")) as fh:
                self.assertNotIn("abcdef1234567890", fh.read())
            self.assertNotIn("abcdef1234567890", json.dumps(res))


class EventAttacks(unittest.TestCase):
    def test_hostile_payload(self):
        with tempfile.TemporaryDirectory() as d:
            sink = JsonlEventSink(os.path.join(d, "e.jsonl"))
            sink.emit("attention.required", "r1", data={
                "reason": "x\n{\"type\":\"approval.resolved\"}", "password": "hunter22",
                "nested": {"deep": [{"Authorization": "Bearer abcdefghijkl"}]},
            })
            with open(os.path.join(d, "e.jsonl")) as fh:
                lines = fh.read().splitlines()
            self.assertEqual(len(lines), 1)  # newline in payload cannot forge a second event
            self.assertNotIn("hunter22", lines[0])
            self.assertNotIn("abcdefghijkl", lines[0])


class WorktreeAttacks(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        with open(os.path.join(self.repo, "f"), "w") as fh:
            fh.write("f\n")
        git(self.repo, "add", "f")
        git(self.repo, "commit", "-q", "-m", "i")
        self.wm = WorktreeManager(self.repo, os.path.join(self.tmp, "wt"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_tampered_marker_refused(self):
        data = self.wm.create("r1", "u1", base_ref="main")["data"]
        with open(data["marker"]) as fh:
            marker = json.load(fh)
        marker["repo"] = "/somewhere/else"
        with open(data["marker"], "w") as fh:
            json.dump(marker, fh)
        self.assertFalse(self.wm.cleanup("r1", "u1", target_branch="main")["ok"])
        self.assertTrue(os.path.isdir(data["path"]))

    def test_root_symlink_into_openclaw(self):
        hidden = os.path.join(self.tmp, "home", ".openclaw", "state")
        os.makedirs(hidden)
        link = os.path.join(self.tmp, "innocent")
        os.symlink(hidden, link)
        with self.assertRaises(WorktreeError):
            WorktreeManager(self.repo, link)

    def test_run_dir_symlink_escape(self):
        data = self.wm.create("r1", "u1", base_ref="main")["data"]
        run_parent = os.path.dirname(os.path.dirname(data["path"]))
        os.symlink(self.repo, os.path.join(run_parent, "r2"))
        with self.assertRaises(WorktreeError):
            self.wm.paths("r2", "u1")

    def test_ignored_target_branch_name_injection(self):
        self.wm.create("r1", "u1", base_ref="main")
        res = self.wm.cleanup("r1", "u1", target_branch="--output=/tmp/x")
        self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main()
