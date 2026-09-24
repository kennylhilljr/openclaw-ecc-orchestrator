import os
import subprocess
import tempfile
import unittest

from openclaw_ecc_orchestrator.merge_queue.conflicts import (
    changed_files,
    out_of_scope_changes,
    patterns_overlap,
    predict_changed_conflicts,
    predict_scope_conflicts,
)
from openclaw_ecc_orchestrator.merge_queue.dispatch import Dispatcher, plan_dispatch
from openclaw_ecc_orchestrator.runs.manager import RunManager
from openclaw_ecc_orchestrator.runs.store import RunStore

try:
    from ._units import work_unit
except ImportError:  # discovered as a top level module
    from _units import work_unit


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def unit(uid, files, deps=()):
    return work_unit(uid, deps=deps, files=files)


class OverlapTests(unittest.TestCase):
    def test_patterns(self):
        yes = [("src/a.py", "src/a.py"), ("src", "src/a.py"), ("src/", "src/a/b.py"), ("src/*.py", "src/a.py"),
               ("src/**", "src/x/y.py"), ("src/*", "src/lib/*"), ("./docs/a.md", "docs/a.md"), ("*", "anything")]
        no = [("src/a.py", "src/b.py"), ("src/a", "src/ab.py"), ("docs/*", "src/*"), ("tests/x.py", "src/*.py")]
        for a, b in yes:
            self.assertTrue(patterns_overlap(a, b), (a, b))
            self.assertTrue(patterns_overlap(b, a), (b, a))
        for a, b in no:
            self.assertFalse(patterns_overlap(a, b), (a, b))
            self.assertFalse(patterns_overlap(b, a), (b, a))

    def test_predict_scope_conflicts(self):
        units = [unit("a", ["src/a.py"]), unit("b", ["src/*.py"]), unit("c", ["docs/c.md"])]
        pairs = predict_scope_conflicts(units)
        self.assertEqual([(p["a"], p["b"]) for p in pairs], [("a", "b")])
        self.assertEqual(pairs[0]["overlaps"], [["src/a.py", "src/*.py"]])

    def test_out_of_scope(self):
        self.assertEqual(out_of_scope_changes(["src/a.py", "README.md"], ["src/*"]), ["README.md"])

    def test_plan_dispatch_defers_overlap(self):
        active = [unit("a", ["src/a.py"])]
        cands = [unit("b", ["src/a.py"]), unit("c", ["docs/*"]), unit("d", ["docs/d.md"])]
        plan = plan_dispatch(cands, active)
        self.assertEqual(plan["dispatch"], ["c"])
        deferred = {d["unit_id"]: d["conflicts_with"] for d in plan["deferred"]}
        self.assertEqual(deferred, {"b": ["a"], "d": ["c"]})


class ChangedFilesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self._tmp.name, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        self.commit("README.md", "hi\n")

    def tearDown(self):
        self._tmp.cleanup()

    def commit(self, name, content):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content)
        git(self.repo, "add", name)
        git(self.repo, "commit", "-q", "-m", name)

    def branch(self, name, files):
        git(self.repo, "checkout", "-q", "-b", name, "main")
        for f in files:
            self.commit(f, name + "\n")
        git(self.repo, "checkout", "-q", "main")

    def test_changed_and_predicted(self):
        self.branch("b1", ["src/a.py", "src/shared.py"])
        self.branch("b2", ["src/shared.py", "docs/x.md"])
        self.branch("b3", ["docs/y.md"])
        self.commit("main-only.txt", "m\n")
        self.assertEqual(changed_files(self.repo, "b1", "main"), ["src/a.py", "src/shared.py"])
        pairs = predict_changed_conflicts(self.repo, "main", {"u1": "b1", "u2": "b2", "u3": "b3"})
        self.assertEqual([(p["a"], p["b"], p["files"]) for p in pairs], [("u1", "u2", ["src/shared.py"])])


class WholeRepositoryScopeTests(unittest.TestCase):
    """A unit without declared scope may touch anything: it runs alone."""

    def test_empty_scope_conflicts_with_everything(self):
        whole = {"id": "w", "scope": {"files": []}}
        none = {"id": "n"}
        other = {"id": "o", "scope": {"files": ["docs/o.md"]}}
        self.assertTrue(predict_scope_conflicts([whole, other]))
        self.assertTrue(predict_scope_conflicts([none, other]))
        self.assertTrue(predict_scope_conflicts([whole, none]))

    def test_empty_scope_dispatched_alone(self):
        whole = {"id": "w", "scope": {"files": []}}
        other = {"id": "o", "scope": {"files": ["docs/o.md"]}}
        plan = plan_dispatch([whole, other], [], limit=5)
        self.assertEqual(plan["dispatch"], ["w"])
        self.assertEqual(plan["deferred"][0]["unit_id"], "o")
        plan = plan_dispatch([whole], [other], limit=5)
        self.assertEqual(plan["dispatch"], [])

    def test_out_of_scope_uses_glob_semantics(self):
        self.assertEqual(out_of_scope_changes(["src/a/b.py", "src/c.py"], ["src/*.py"]), ["src/a/b.py"])
        self.assertEqual(out_of_scope_changes(["src/a/b.py"], ["src/**"]), [])
        self.assertEqual(out_of_scope_changes(["src/a/b.py"], ["src"]), [])
        self.assertEqual(out_of_scope_changes(["x.py"], []), [])


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.mgr = RunManager(RunStore(self._tmp.name), clock=lambda: 1_700_000_000.0)

    def tearDown(self):
        self._tmp.cleanup()

    def test_refuses_parallel_overlap(self):
        units = [unit("a", ["src/a.py"]), unit("b", ["src/*"]), unit("c", ["docs/c.md"])]
        run_id = self.mgr.create_run({"units": units}, conductor_id="c1", run_id="r1")["data"]["run_id"]
        d = Dispatcher(self.mgr)
        dry = d.dispatch(run_id, "c1", ["w1", "w2", "w3"], dry_run=True)
        self.assertTrue(dry["ok"])
        self.assertEqual(self.mgr.load(run_id)["units"]["a"]["state"], "ready")
        res = d.dispatch(run_id, "c1", ["w1", "w2", "w3"])
        self.assertTrue(res["ok"], res)
        assigned = {a["unit_id"] for a in res["data"]["assigned"]}
        self.assertEqual(assigned, {"a", "c"})
        self.assertEqual(res["data"]["deferred"][0]["unit_id"], "b")
        explicit = d.dispatch_unit(run_id, "c1", "b", "w9")
        self.assertFalse(explicit["ok"])
        self.assertEqual(self.mgr.load(run_id)["units"]["b"]["state"], "ready")
        # b overlaps only a; once a stops occupying a checkout, b may run.
        self.mgr.transition(run_id, "c1", "a", "cancelled")
        self.assertTrue(d.dispatch_unit(run_id, "c1", "b", "w9")["ok"])


if __name__ == "__main__":
    unittest.main()
