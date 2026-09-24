"""Shared real-git harness for conductor level integration tests."""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest

from openclaw_ecc_orchestrator.merge_queue.dispatch import Dispatcher
from openclaw_ecc_orchestrator.merge_queue.queue import MergeQueue
from openclaw_ecc_orchestrator.plugin.approvals import ApprovalBroker
from openclaw_ecc_orchestrator.plugin.events import JsonlEventSink
from openclaw_ecc_orchestrator.process.supervisor import Supervisor
from openclaw_ecc_orchestrator.runs.conductor import Conductor
from openclaw_ecc_orchestrator.runs.manager import RunManager
from openclaw_ecc_orchestrator.runs.store import RunStore
from openclaw_ecc_orchestrator.worktrees.manager import WorktreeManager

try:
    from ._units import policy as make_policy
except ImportError:  # discovered as a top level module
    from _units import policy as make_policy

PY = sys.executable

# Test runner. Modes:
#   write SPEC...          commit files, write a truthful handoff
#   forged SPEC...         commit files, handoff claims success with a failing command
#   nohandoff SPEC...      commit files, write no handoff
#   claim=PATHS SPEC...    commit files, handoff lists PATHS (comma separated) as changed
#   fail | block | sleep:N
RUNNER = r'''
import json, os, signal, subprocess, sys, time
mode = sys.argv[1]
print("start", mode, flush=True)
if mode == "fail":
    print("simulated failure", file=sys.stderr, flush=True)
    sys.exit(1)
if mode == "block":
    signal.signal(signal.SIGTERM, lambda *a: (print("stopping", flush=True), sys.exit(143)))
    print("ready", flush=True)
    while True:
        time.sleep(0.05)
if mode.startswith("sleep:"):
    time.sleep(float(mode.split(":", 1)[1]))
    sys.exit(0)
specs = sys.argv[2:]
names = []
for spec in specs:
    name, _, content = spec.partition("=")
    os.makedirs(os.path.dirname(name) or ".", exist_ok=True)
    with open(name, "w") as fh:
        fh.write(content + "\n")
    subprocess.run(["git", "add", name], check=True)
    names.append(name)
subprocess.run(["git", "commit", "-q", "-m", "runner work"], check=True)
print("committed", flush=True)
path = os.environ.get("ECC_HANDOFF_PATH")
if mode == "nohandoff" or not path:
    sys.exit(0)
head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip()
commands = [{"command": "git commit", "exit_code": 0, "result": "ok"}]
if mode == "forged":
    commands.append({"command": "python3 -m unittest", "exit_code": 1, "result": "1 failed"})
if mode.startswith("claim="):
    names = mode.split("=", 1)[1].split(",")
handoff = {
    "schema_version": "1.0", "unit_id": os.environ["ECC_UNIT_ID"], "status": "succeeded",
    "outcome": "done", "files_changed": names, "behavior": "as specified", "commands": commands,
    "unresolved_failures": [], "assumptions": [], "risks": [], "next_action": "review",
    "commit": {"sha": head, "branch": branch or "unit", "worktree": "."},
    "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01, "model": "test"},
    "user_input_required": False,
}
with open(path, "w") as fh:
    json.dump(handoff, fh)
'''


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FakeClock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def exists_cmd(path):
    return f"{shlex.quote(PY)} -c \"import os,sys; sys.exit(0 if os.path.exists('{path}') else 1)\""


class IntegrationBase(unittest.TestCase):
    CERTIFIED = ["codex", "claude"]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        with open(os.path.join(self.repo, "README.md"), "w") as fh:
            fh.write("base\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-q", "-m", "base")
        self.runner = os.path.join(self.tmp, "runner.py")
        with open(self.runner, "w") as fh:
            fh.write(RUNNER)
        self.clock = FakeClock()
        self.state_root = os.path.join(self.tmp, "state")
        self.events_path = os.path.join(self.state_root, "openclaw-events.jsonl")
        self.policy = make_policy()

    def tearDown(self):
        self._tmp.cleanup()

    def conductor(self, cid, lease_ttl=60, **kw):
        emitter = JsonlEventSink(self.events_path, clock=self.clock)
        store = RunStore(os.path.join(self.state_root, "runs"), clock=self.clock)
        manager = RunManager(store, clock=self.clock, emitter=emitter, lease_ttl=lease_ttl)
        supervisor = Supervisor(base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, grace=1.0)
        worktrees = WorktreeManager(self.repo, os.path.join(self.tmp, "agent-worktrees"), clock=self.clock)
        repo_checks = {"required": ["syntax"], "commands": {"syntax": f"{shlex.quote(PY)} -c pass"}}
        holder = {}
        broker = ApprovalBroker(os.path.join(self.state_root, "approvals.json"), clock=self.clock, emitter=emitter)
        queue = MergeQueue(self.repo, "main", os.path.join(self.state_root, "merge-queue.json"),
                           os.path.join(self.tmp, "merge-scratch"),
                           gate_runner=lambda unit, path: holder["c"].gate(unit, path),
                           emitter=emitter, clock=self.clock, broker=broker, policy=self.policy,
                           certified_runners=kw.get("certified_runners", self.CERTIFIED))
        options = dict(policy=self.policy, certified_runners=self.CERTIFIED)
        options.update(kw)
        c = Conductor(cid, manager=manager, worktrees=worktrees, supervisor=supervisor, queue=queue, broker=broker,
                      emitter=emitter, repo_checks=repo_checks, target_branch="main",
                      log_root=os.path.join(self.state_root, "logs"), clock=self.clock, **options)
        holder["c"] = c
        return c, manager, Dispatcher(manager)

    def argv(self, mode, *specs):
        return [PY, self.runner, mode, *specs]

    def ok(self, res):
        self.assertTrue(res["ok"], json.dumps(res, indent=1, default=str)[:4000])
        return res

    def failed_checks(self, res):
        return [c["name"] for c in res["checks"] if not c["ok"]]

    def events(self):
        if not os.path.exists(self.events_path):
            return []
        with open(self.events_path) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def review(self, c, run_id, unit_id, runner="claude", model="opus", tier=2, reviewer="rev-1", head=None,
               verdict="approved", session="sess-A"):
        if head is None:
            head = c.manager.load(run_id)["units"][unit_id]["annotations"]["verification"]["head"]
        return c.record_review(run_id, unit_id, {"reviewer": reviewer, "runner": runner, "model": model,
                                                 "tier": tier, "head": head, "verdict": verdict},
                               session_id=session)

    def decide(self, c, request, decision="approved"):
        body = {k: request[k] for k in ("request_id", "run_id", "unit_id", "action", "plan_sha256", "session_id")}
        body.update(decision=decision, decided_by="operator")
        return self.ok(c.broker.resolve(body))["data"]

    def drive_to_review(self, c, run_id, unit_id, argv, worker=None):
        self.ok(c.assign_unit(run_id, unit_id, worker))
        self.ok(c.start_unit(run_id, unit_id, argv))
        return c.wait_unit(run_id, unit_id)
