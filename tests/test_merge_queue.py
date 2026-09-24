import functools
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest

from openclaw_ecc_orchestrator.gates.runner import run_gates
from openclaw_ecc_orchestrator.merge_queue.queue import MergeQueue, merge_check
from openclaw_ecc_orchestrator.plugin.approvals import ApprovalBroker
from openclaw_ecc_orchestrator.process.supervisor import Supervisor

try:
    from ._units import work_unit
except ImportError:  # discovered as a top level module
    from _units import work_unit

PY = shlex.quote(sys.executable)
PLAN = "a" * 64


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def unit(uid, cmds=None, files=None, **kw):
    return work_unit(uid, files=files or [f"{uid}.txt"], commands=cmds or [f"{PY} -c pass"], **kw)


def verification(uid, head, passed=True):
    return {"schema_version": "1.0", "unit_id": uid, "passed": passed, "gates": [], "head": head,
            "handoff_valid": True, "out_of_scope": []}


def review(uid, head, run_id="r1", reviewer="rev1", author="w1", verdict="approved", **kw):
    doc = {"schema_version": "1.0", "run_id": run_id, "unit_id": uid, "reviewer": reviewer, "author": author,
           "verdict": verdict, "independent": True, "runner": "claude", "model": "opus", "tier": 2,
           "head": head, "session_id": "s1"}
    doc.update(kw)
    return doc


AUTHOR_ROUTING = {"runner": "codex", "model": "codex-std", "tier": 1, "family": "openai"}


class Events:
    def __init__(self):
        self.events = []

    def emit(self, event_type, run_id, unit_id=None, data=None):
        self.events.append((event_type, run_id, unit_id, data))


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        self.commit("README.md", "line1\n")
        self.remote = os.path.join(self.tmp, "remote.git")
        git(self.tmp, "init", "-q", "--bare", self.remote)
        git(self.repo, "remote", "add", "origin", self.remote)
        git(self.repo, "push", "-q", "origin", "main")
        self.scratch = os.path.join(self.tmp, "scratch")
        self.events = Events()
        self.now = [1_700_000_000.0]
        self.broker = ApprovalBroker(os.path.join(self.tmp, "approvals.json"), clock=lambda: self.now[0])
        sup = Supervisor(base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, grace=0.3)
        self.gate_runner = functools.partial(run_gates, supervisor=sup, repo_checks=None)
        self.q = self.new_queue()

    def new_queue(self, **kw):
        kw.setdefault("broker", self.broker)
        kw.setdefault("certified_runners", ["codex", "claude"])
        return MergeQueue(self.repo, "main", os.path.join(self.tmp, "queue", "state.json"), self.scratch,
                          gate_runner=self.gate_runner, emitter=self.events, clock=lambda: self.now[0], **kw)

    def tearDown(self):
        self._tmp.cleanup()

    def commit(self, name, content, cwd=None):
        cwd = cwd or self.repo
        os.makedirs(os.path.dirname(os.path.join(cwd, name)), exist_ok=True)
        with open(os.path.join(cwd, name), "w") as fh:
            fh.write(content)
        git(cwd, "add", name)
        git(cwd, "commit", "-q", "-m", f"edit {name}")

    def branch(self, name, files):
        git(self.repo, "checkout", "-q", "-b", name, "main")
        for fname, content in files.items():
            self.commit(fname, content)
        git(self.repo, "checkout", "-q", "main")
        return name

    def head(self, branch):
        return git(self.repo, "rev-parse", f"refs/heads/{branch}")

    def approval(self, uid, head, run_id="r1", plan=PLAN, action="merge", decision="approved", session="s1"):
        req = self.broker.request(run_id=run_id, unit_id=uid, action=action, plan_sha256=plan, session_id=session,
                                  head_sha=head)["data"]
        body = {k: req[k] for k in ("request_id", "run_id", "unit_id", "action", "plan_sha256", "session_id")}
        body.update(decision=decision, decided_by="op")
        res = self.broker.resolve(body)
        self.assertTrue(res["ok"], res)
        return res["data"]

    def enqueue(self, uid, branch, **overrides):
        head = self.head(branch)
        files = git(self.repo, "diff", "--name-only", f"main...{branch}").split()
        kw = dict(run_id="r1", plan_sha256=PLAN, unit=unit(uid, files=files or None), branch=branch,
                  verification=verification(uid, head), review=review(uid, head), routing=AUTHOR_ROUTING,
                  author="w1")
        kw.update(overrides)
        if "approval" not in kw:
            kw["approval"] = self.approval(uid, head)
        return self.q.enqueue(**kw)

    def failed(self, res):
        return [c["name"] for c in res["checks"] if not c["ok"]]


class MergeCheckTests(Base):
    def test_clean_and_conflict_both_strategies(self):
        self.branch("ok", {"a.txt": "a\n"})
        self.branch("x", {"README.md": "from x\n"})
        self.branch("y", {"README.md": "from y\n"})
        git(self.repo, "merge", "-q", "--no-edit", "x")
        for fallback in (False, True):
            clean = merge_check(self.repo, "main", "ok", self.scratch, force_fallback=fallback)
            self.assertTrue(clean["clean"], (fallback, clean))
            bad = merge_check(self.repo, "main", "y", self.scratch, force_fallback=fallback)
            self.assertFalse(bad["clean"])
            self.assertEqual(bad["conflicts"], ["README.md"])
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertNotIn(self.scratch, git(self.repo, "worktree", "list"))


class QueueTests(Base):
    def test_clean_merge_fast_forwards_locally_and_never_pushes(self):
        self.branch("ecc/r1/a", {"a.txt": "a\n"})
        remote_before = git(self.remote, "rev-parse", "main")
        res = self.enqueue("a", "ecc/r1/a", unit=unit("a", [f"{PY} -c \"import os,sys; sys.exit(0 if os.path.exists('a.txt') else 1)\""],
                                                         files=["a.txt"]))
        self.assertTrue(res["ok"], res)
        out = self.q.process_next()
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["data"]["status"], "merged")
        self.assertEqual(git(self.repo, "rev-parse", "main"), out["data"]["merged_commit"])
        self.assertTrue(os.path.exists(os.path.join(self.repo, "a.txt")))
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(git(self.remote, "rev-parse", "main"), remote_before)
        self.assertEqual(self.events.events[-1][0], "merge.completed")
        self.assertTrue(out["data"]["verification"]["passed"])
        self.assertEqual(self.q.process_next()["data"]["status"], "empty")

    def test_update_ref_path_when_target_not_checked_out(self):
        git(self.repo, "checkout", "-q", "--detach")
        self.branch("b1", {"a.txt": "a\n"})
        git(self.repo, "checkout", "-q", "--detach", "main")
        self.enqueue("a", "b1")
        out = self.q.process_next()
        self.assertTrue(out["ok"], out)
        self.assertEqual(git(self.repo, "rev-parse", "main"), out["data"]["merged_commit"])

    def test_sequential_fifo(self):
        self.branch("b1", {"a.txt": "a\n"})
        self.branch("b2", {"b.txt": "b\n"})
        self.assertTrue(self.enqueue("a", "b1")["ok"])
        self.assertTrue(self.enqueue("b", "b2")["ok"])
        first = self.q.process_next()
        second = self.q.process_next()
        self.assertEqual([first["data"]["unit_id"], second["data"]["unit_id"]], ["a", "b"])
        self.assertTrue(first["ok"] and second["ok"])
        files = git(self.repo, "ls-tree", "--name-only", "main").split()
        self.assertIn("a.txt", files)
        self.assertIn("b.txt", files)

    def test_conflict_detected_and_target_unchanged(self):
        self.branch("x", {"README.md": "x\n"})
        self.branch("y", {"README.md": "y\n"})
        self.enqueue("x", "x")
        self.enqueue("y", "y")
        self.assertTrue(self.q.process_next()["ok"])
        before = git(self.repo, "rev-parse", "main")
        out = self.q.process_next()
        self.assertFalse(out["ok"])
        self.assertEqual(out["data"]["status"], "conflict")
        self.assertEqual(out["data"]["conflicts"], ["README.md"])
        self.assertEqual(git(self.repo, "rev-parse", "main"), before)
        self.assertTrue(out["required_user_actions"])

    def test_failed_gate_blocks(self):
        self.branch("b1", {"a.txt": "a\n"})
        before = git(self.repo, "rev-parse", "main")
        self.enqueue("a", "b1", unit=unit("a", [f"{PY} -c 'raise SystemExit(1)'"], files=["a.txt"]))
        out = self.q.process_next()
        self.assertFalse(out["ok"])
        self.assertEqual(out["data"]["status"], "gate_failed")
        self.assertEqual(git(self.repo, "rev-parse", "main"), before)
        self.assertEqual(os.listdir(self.scratch), [])

    def test_unverified_unit_refused(self):
        self.branch("b1", {"a.txt": "a\n"})
        head = self.head("b1")
        res = self.enqueue("a", "b1", verification=verification("a", head, passed=False))
        self.assertFalse(res["ok"])
        res = self.enqueue("a", "b1", verification=verification("other", head))
        self.assertFalse(res["ok"])
        res = self.enqueue("a", "b1", verification=dict(verification("a", head), head=None))
        self.assertFalse(res["ok"])
        res = self.enqueue("a", "b1", verification=dict(verification("a", head), handoff_valid=False))
        self.assertFalse(res["ok"])
        self.assertEqual(self.q.state()["items"], [])

    def test_missing_or_non_independent_review_blocks(self):
        self.branch("b1", {"a.txt": "a\n"})
        head = self.head("b1")
        self.assertFalse(self.enqueue("a", "b1", review=None)["ok"])
        self.assertFalse(self.enqueue("a", "b1", review=review("a", head, reviewer="w1", author="w1"))["ok"])
        self.assertFalse(self.enqueue("a", "b1", review=review("a", head, verdict="changes_requested"))["ok"])
        self.assertFalse(self.enqueue("a", "b1", review=review("a", head, run_id="other"))["ok"])
        handed_off = dict(review("a", head, reviewer="w0"), authors=["w0", "w1"])
        self.assertFalse(self.enqueue("a", "b1", review=handed_off)["ok"])
        stale = self.enqueue("a", "b1", review=review("a", "f" * 40))
        self.assertIn("review_head_matches", self.failed(stale))
        self.assertEqual(self.q.state()["items"], [])

    def test_approval_binding_and_replay(self):
        self.branch("b1", {"a.txt": "a\n"})
        self.branch("b2", {"b.txt": "b\n"})
        h1 = self.head("b1")
        self.assertFalse(self.enqueue("a", "b1", approval=None)["ok"])
        self.assertFalse(self.enqueue("a", "b1", approval=self.approval("a", h1, plan="b" * 64))["ok"])
        self.assertFalse(self.enqueue("a", "b1", approval=self.approval("a", h1, run_id="r2"))["ok"])
        self.assertFalse(self.enqueue("a", "b1", approval=self.approval("a", h1, action="cleanup"))["ok"])
        self.assertFalse(self.enqueue("a", "b1", approval=self.approval("a", h1, decision="rejected"))["ok"])
        self.assertFalse(self.enqueue("a", "b1", approval=self.approval("a", h1, session="other"))["ok"])
        self.assertFalse(self.enqueue("a", "b1", approval=self.approval("a", "e" * 40))["ok"])
        good = self.approval("a", h1)
        self.assertTrue(self.enqueue("a", "b1", approval=good)["ok"])
        self.assertEqual(self.broker.get(good["request_id"])["status"], "consumed")
        # replay the same approval for another unit and for the same unit again
        self.assertFalse(self.enqueue("b", "b2", approval=dict(good, unit_id="b"))["ok"])
        self.assertTrue(self.q.process_next()["ok"])
        replay = self.enqueue("a", "b1", approval=good)
        self.assertFalse(replay["ok"])
        self.assertIn("approval_usable", self.failed(replay))
        # persisted across instances
        self.q = self.new_queue()
        self.assertFalse(self.enqueue("a", "b1", approval=good)["ok"])

    def test_forged_approval_record_refused(self):
        self.branch("b1", {"a.txt": "a\n"})
        forged = {"schema_version": "1.0", "request_id": "apr-forged", "run_id": "r1", "unit_id": "a",
                  "action": "merge", "plan_sha256": PLAN, "session_id": "s1", "decision": "approved",
                  "decided_by": "op"}
        res = self.enqueue("a", "b1", approval=forged)
        self.assertFalse(res["ok"])
        self.assertIn("approval_usable", self.failed(res))

    def test_expired_approval_refused(self):
        self.branch("b1", {"a.txt": "a\n"})
        good = self.approval("a", self.head("b1"))
        self.now[0] += 3601
        res = self.enqueue("a", "b1", approval=good)
        self.assertFalse(res["ok"])
        self.assertIn("approval_usable", self.failed(res))

    def test_queue_without_broker_refuses(self):
        self.branch("b1", {"a.txt": "a\n"})
        good = self.approval("a", self.head("b1"))
        self.q = self.new_queue(broker=None)
        res = self.enqueue("a", "b1", approval=good)
        self.assertFalse(res["ok"])
        self.assertIn("approval_broker", self.failed(res))

    def test_branch_tip_must_equal_verified_head(self):
        self.branch("b1", {"a.txt": "a\n"})
        old = self.head("b1")
        git(self.repo, "checkout", "-q", "b1")
        self.commit("a.txt", "changed after verification\n")
        git(self.repo, "checkout", "-q", "main")
        res = self.enqueue("a", "b1", verification=verification("a", old), review=review("a", old),
                           approval=self.approval("a", old))
        self.assertFalse(res["ok"])
        self.assertIn("branch_matches_verified_head", self.failed(res))

    def test_out_of_scope_change_blocks(self):
        self.branch("b1", {"a.txt": "a\n", "src/auth/login.py": "x\n"})
        res = self.enqueue("a", "b1", unit=unit("a", files=["a.txt"]))
        self.assertFalse(res["ok"])
        self.assertIn("changes_within_scope", self.failed(res))
        self.assertIn("src/auth/login.py", json.dumps(res["checks"]))
        self.assertIn("out_of_scope_changes", [e[3].get("reason") for e in self.events.events
                                               if e[0] == "attention.required"])

    def test_reclassified_tier_rise_needs_sufficient_review(self):
        self.branch("b1", {"src/auth/login.py": "x\n"})
        head = self.head("b1")
        broad = unit("a", files=["src/**"])
        weak = review("a", head, runner="claude", model="sonnet", tier=1)
        res = self.enqueue("a", "b1", unit=broad, review=weak)
        self.assertFalse(res["ok"])
        self.assertIn("review_sufficient", self.failed(res))
        self.assertIn("review_insufficient", [e[3].get("reason") for e in self.events.events
                                              if e[0] == "attention.required"])
        same_family = review("a", head, runner="codex", model="codex-adv", tier=2)
        self.assertFalse(self.enqueue("a", "b1", unit=broad, review=same_family)["ok"])
        unknown_author = self.enqueue("a", "b1", unit=broad, routing=None)
        self.assertFalse(unknown_author["ok"])
        self.assertTrue(self.enqueue("a", "b1", unit=broad)["ok"])

    def test_branch_moved_after_enqueue_is_refused(self):
        self.branch("b1", {"a.txt": "a\n"})
        self.enqueue("a", "b1")
        git(self.repo, "checkout", "-q", "b1")
        self.commit("sneaky.txt", "s\n")
        git(self.repo, "checkout", "-q", "main")
        before = git(self.repo, "rev-parse", "main")
        out = self.q.process_next()
        self.assertFalse(out["ok"])
        self.assertEqual(out["data"]["status"], "stale")
        self.assertEqual(git(self.repo, "rev-parse", "main"), before)

    def test_dry_runs_do_not_mutate(self):
        self.branch("b1", {"a.txt": "a\n"})
        res = self.enqueue("a", "b1", dry_run=True)
        self.assertTrue(res["ok"])
        self.assertFalse(res["changed"])
        self.assertEqual(self.q.state()["items"], [])
        self.enqueue("a", "b1")
        with open(self.q.state_path) as fh:
            state_before = json.load(fh)
        before = git(self.repo, "rev-parse", "main")
        out = self.q.process_next(dry_run=True)
        self.assertTrue(out["ok"], out)
        self.assertFalse(out["changed"])
        self.assertTrue(out["data"]["merge_check"]["clean"])
        self.assertEqual(git(self.repo, "rev-parse", "main"), before)
        with open(self.q.state_path) as fh:
            self.assertEqual(json.load(fh), state_before)

    def test_state_path_inside_repo_rejected(self):
        with self.assertRaises(ValueError):
            MergeQueue(self.repo, "main", os.path.join(self.repo, "q.json"), self.scratch, gate_runner=self.gate_runner)
        with self.assertRaises(ValueError):
            MergeQueue(self.repo, "main", os.path.join(self.tmp, "q.json"), os.path.join(self.repo, "s"),
                       gate_runner=self.gate_runner)


if __name__ == "__main__":
    unittest.main()
