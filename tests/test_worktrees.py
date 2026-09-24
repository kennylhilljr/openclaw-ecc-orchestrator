import json
import os
import subprocess
import tempfile
import unittest

from openclaw_ecc_orchestrator.worktrees.manager import WorktreeError, WorktreeManager, validate_root


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def init_repo(path):
    os.makedirs(path)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Test")
    git(path, "config", "commit.gpgsign", "false")
    with open(os.path.join(path, "README.md"), "w") as fh:
        fh.write("hello\n")
    git(path, "add", "README.md")
    git(path, "commit", "-q", "-m", "init")
    return path


def commit_file(cwd, name, content, msg="change"):
    with open(os.path.join(cwd, name), "w") as fh:
        fh.write(content)
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", msg)
    return git(cwd, "rev-parse", "HEAD")


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.repo = init_repo(os.path.join(self.tmp, "repo"))
        self.root = os.path.join(self.tmp, "agent-worktrees")
        self.wm = WorktreeManager(self.repo, self.root, clock=lambda: 1_700_000_000.0)

    def tearDown(self):
        self._tmp.cleanup()

    def create(self, unit="u1", run="r1"):
        res = self.wm.create(run, unit, base_ref="main", owner="w1")
        self.assertTrue(res["ok"], res)
        return res["data"]


class RootValidationTests(Base):
    def test_rejects_root_inside_repo(self):
        with self.assertRaises(WorktreeError):
            WorktreeManager(self.repo, os.path.join(self.repo, "wt"))
        with self.assertRaises(WorktreeError):
            WorktreeManager(self.repo, self.repo)

    def test_rejects_openclaw_dir(self):
        with self.assertRaises(WorktreeError):
            WorktreeManager(self.repo, os.path.join(self.tmp, "home", ".openclaw", "worktrees"))

    def test_rejects_symlink_into_repo(self):
        link = os.path.join(self.tmp, "sneaky")
        os.symlink(self.repo, link)
        with self.assertRaises(WorktreeError):
            WorktreeManager(self.repo, os.path.join(link, "wt"))

    def test_rejects_root_inside_other_repo(self):
        other = init_repo(os.path.join(self.tmp, "other"))
        res = validate_root(os.path.join(other, "nested", "wt"), self.repo)
        self.assertFalse(res["ok"])

    def test_rejects_traversal_ids(self):
        for bad in ["../x", "a/b", "..", ".hidden", "", "a b", "x" * 200]:
            with self.assertRaises(WorktreeError):
                self.wm.paths("r1", bad)
            with self.assertRaises(WorktreeError):
                self.wm.paths(bad, "u1")


class CreateTests(Base):
    def test_create_branch_worktree_and_marker(self):
        data = self.create()
        self.assertTrue(data["path"].startswith(os.path.realpath(self.root) + os.sep))
        self.assertTrue(os.path.isdir(data["path"]))
        self.assertEqual(git(data["path"], "rev-parse", "--abbrev-ref", "HEAD"), data["branch"])
        self.assertIn(data["path"], git(self.repo, "worktree", "list", "--porcelain"))
        with open(data["marker"]) as fh:
            marker = json.load(fh)
        self.assertEqual(marker["schema_version"], "1.0")
        self.assertEqual((marker["run_id"], marker["unit_id"], marker["owner"]), ("r1", "u1", "w1"))
        self.assertEqual(git(data["path"], "status", "--porcelain"), "")

    def test_create_dry_run_has_no_side_effects(self):
        res = self.wm.create("r1", "u1", base_ref="main", dry_run=True)
        self.assertTrue(res["ok"])
        self.assertFalse(res["changed"])
        self.assertTrue(res["data"]["commands"])
        self.assertFalse(os.path.exists(self.root))
        self.assertEqual(git(self.repo, "branch", "--list", "ecc/*"), "")

    def test_create_twice_refused(self):
        self.create()
        self.assertFalse(self.wm.create("r1", "u1", base_ref="main")["ok"])

    def test_list(self):
        self.create("u1")
        self.create("u2")
        res = self.wm.list()
        units = sorted(w["unit_id"] for w in res["data"]["worktrees"])
        self.assertEqual(units, ["u1", "u2"])
        self.assertTrue(all(w["registered"] for w in res["data"]["worktrees"]))


class CleanupTests(Base):
    def test_clean_worktree_removed(self):
        data = self.create()
        res = self.wm.cleanup("r1", "u1", target_branch="main")
        self.assertTrue(res["ok"], res)
        self.assertFalse(os.path.exists(data["path"]))
        self.assertFalse(os.path.exists(data["marker"]))
        self.assertEqual(git(self.repo, "branch", "--list", data["branch"]), "")

    def test_refuses_uncommitted_changes(self):
        data = self.create()
        with open(os.path.join(data["path"], "README.md"), "a") as fh:
            fh.write("edit\n")
        res = self.wm.cleanup("r1", "u1", target_branch="main", archive=True)
        self.assertFalse(res["ok"])
        self.assertTrue(os.path.exists(data["path"]))
        self.assertIn("uncommitted", " ".join(c["name"] for c in res["checks"] if not c["ok"]))

    def test_refuses_untracked_files(self):
        data = self.create()
        with open(os.path.join(data["path"], "new.txt"), "w") as fh:
            fh.write("x\n")
        res = self.wm.cleanup("r1", "u1", target_branch="main")
        self.assertFalse(res["ok"])
        self.assertTrue(os.path.exists(os.path.join(data["path"], "new.txt")))

    def test_refuses_unmerged_commits_without_archive(self):
        data = self.create()
        commit_file(data["path"], "feature.txt", "f\n")
        res = self.wm.cleanup("r1", "u1", target_branch="main")
        self.assertFalse(res["ok"])
        self.assertTrue(os.path.exists(data["path"]))
        self.assertTrue(res["required_user_actions"])

    def test_archive_then_remove(self):
        data = self.create()
        tip = commit_file(data["path"], "feature.txt", "f\n")
        dry = self.wm.cleanup("r1", "u1", target_branch="main", archive=True, dry_run=True)
        self.assertTrue(dry["ok"], dry)
        self.assertFalse(dry["changed"])
        self.assertTrue(os.path.exists(data["path"]))
        self.assertFalse(os.path.exists(dry["data"]["bundle"]))
        res = self.wm.cleanup("r1", "u1", target_branch="main", archive=True)
        self.assertTrue(res["ok"], res)
        bundle = res["rollback_checkpoint"]["bundle"]
        self.assertTrue(os.path.isfile(bundle))
        self.assertFalse(bundle.startswith(self.repo + os.sep))
        self.assertFalse(os.path.exists(data["path"]))
        git(self.repo, "fetch", "-q", bundle, f"{data['branch']}:refs/heads/restored")
        self.assertEqual(git(self.repo, "rev-parse", "restored"), tip)

    def test_commits_on_remote_are_safe(self):
        remote = os.path.join(self.tmp, "remote.git")
        git(self.tmp, "init", "-q", "--bare", remote)
        git(self.repo, "remote", "add", "origin", remote)
        data = self.create()
        commit_file(data["path"], "feature.txt", "f\n")
        git(data["path"], "push", "-q", "origin", data["branch"])
        res = self.wm.cleanup("r1", "u1", target_branch="main")
        self.assertTrue(res["ok"], res)

    def test_merged_commits_are_safe(self):
        data = self.create()
        commit_file(data["path"], "feature.txt", "f\n")
        git(self.repo, "merge", "-q", "--ff-only", data["branch"])
        self.assertTrue(self.wm.cleanup("r1", "u1", target_branch="main")["ok"])

    def test_dry_run_does_not_remove(self):
        data = self.create()
        res = self.wm.cleanup("r1", "u1", target_branch="main", dry_run=True)
        self.assertTrue(res["ok"])
        self.assertFalse(res["changed"])
        self.assertTrue(os.path.isdir(data["path"]))
        self.assertTrue(os.path.exists(data["marker"]))

    def test_refuses_without_marker(self):
        data = self.create()
        os.unlink(data["marker"])
        res = self.wm.cleanup("r1", "u1", target_branch="main")
        self.assertFalse(res["ok"])
        self.assertTrue(os.path.isdir(data["path"]))

    def test_detached_head_work_needs_archive(self):
        # Detached HEAD commits are unmerged work: refused without archive,
        # bundled under a named ref with archive=True (see test_worktrees_cleanup_unique).
        data = self.create()
        git(data["path"], "checkout", "-q", "--detach")
        commit_file(data["path"], "x.txt", "x\n")
        res = self.wm.cleanup("r1", "u1", target_branch="main")
        self.assertFalse(res["ok"])
        self.assertTrue(os.path.isdir(data["path"]))
        res = self.wm.cleanup("r1", "u1", target_branch="main", archive=True)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["rollback_checkpoint"]["archived_refs"])

    def test_refuses_when_head_moved_to_other_branch(self):
        data = self.create()
        git(data["path"], "checkout", "-q", "-b", "other")
        res = self.wm.cleanup("r1", "u1", target_branch="main", archive=True)
        self.assertFalse(res["ok"])
        self.assertTrue(os.path.isdir(data["path"]))


if __name__ == "__main__":
    unittest.main()
