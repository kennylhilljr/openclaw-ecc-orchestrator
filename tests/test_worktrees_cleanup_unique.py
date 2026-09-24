"""Cleanup never loses gitignored files or detached HEAD commits (review finding 6)."""

import os
import subprocess
import tarfile
import tempfile
import unittest

from openclaw_ecc_orchestrator.worktrees.manager import WorktreeManager


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
        with open(os.path.join(self.repo, ".gitignore"), "w") as fh:
            fh.write("*.local\n.env\n__pycache__/\nbuild/\n")
        git(self.repo, "add", ".gitignore")
        git(self.repo, "commit", "-q", "-m", "ignore")
        self.wm = WorktreeManager(self.repo, os.path.join(self.tmp, "wt"), clock=lambda: 1_700_000_000.0)

    def tearDown(self):
        self._tmp.cleanup()

    def create(self, unit="u1"):
        res = self.wm.create("r1", unit, base_ref="main", owner="w1")
        self.assertTrue(res["ok"], res)
        return res["data"]

    def failed(self, res):
        return {c["name"] for c in res["checks"] if not c["ok"]}

    def archive_refs(self):
        return git(self.repo, "for-each-ref", "--format=%(refname)", "refs/ecc-archive")


class IgnoredFileTests(Base):
    def test_ignored_file_blocks_cleanup(self):
        data = self.create()
        notes = os.path.join(data["path"], "design-notes.local")
        with open(notes, "w") as fh:
            fh.write("3 hours of uncommitted work\n")
        dry = self.wm.cleanup("r1", "u1", "main", dry_run=True)
        self.assertFalse(dry["ok"])
        self.assertIn("no_ignored_files", self.failed(dry))
        self.assertIn("design-notes.local", dry["data"]["ignored_files"])
        res = self.wm.cleanup("r1", "u1", "main")
        self.assertFalse(res["ok"])
        self.assertTrue(os.path.exists(notes))
        self.assertTrue(any(a["kind"] == "unarchived_ignored_files" for a in res["required_user_actions"]))

    def test_ignored_files_archived_next_to_bundle(self):
        data = self.create()
        os.makedirs(os.path.join(data["path"], "build", "sub"))
        for rel, body in (("design-notes.local", "notes\n"), (".env", "X=1\n"), ("build/sub/out.bin", "b\n")):
            with open(os.path.join(data["path"], rel), "w") as fh:
                fh.write(body)
        commit_file(data["path"], "feature.txt", "f\n")
        dry = self.wm.cleanup("r1", "u1", "main", archive=True, dry_run=True)
        self.assertTrue(dry["ok"], dry)
        self.assertFalse(os.path.exists(dry["data"]["ignored_archive"]))
        res = self.wm.cleanup("r1", "u1", "main", archive=True)
        self.assertTrue(res["ok"], res)
        self.assertFalse(os.path.exists(data["path"]))
        rb = res["rollback_checkpoint"]
        tarball = rb["ignored_archive"]
        self.assertEqual(os.path.dirname(tarball), os.path.dirname(rb["bundle"]))
        with tarfile.open(tarball) as tf:
            names = set(tf.getnames())
            self.assertEqual(tf.extractfile("design-notes.local").read(), b"notes\n")
        self.assertEqual(names, {"design-notes.local", ".env", "build/sub/out.bin"})
        self.assertEqual(rb["restore_ignored"][:2], ["tar", "-xzf"])

    def test_ignored_only_archive_without_commits(self):
        data = self.create()
        with open(os.path.join(data["path"], "x.local"), "w") as fh:
            fh.write("x\n")
        res = self.wm.cleanup("r1", "u1", "main", archive=True)
        self.assertTrue(res["ok"], res)
        self.assertIsNone(res["data"]["bundle"])
        self.assertTrue(os.path.isfile(res["rollback_checkpoint"]["ignored_archive"]))

    def test_regenerable_caches_do_not_block(self):
        data = self.create()
        os.makedirs(os.path.join(data["path"], "__pycache__"))
        with open(os.path.join(data["path"], "__pycache__", "m.cpython-311.pyc"), "wb") as fh:
            fh.write(b"\0")
        res = self.wm.cleanup("r1", "u1", "main")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["data"]["ignored_files"], [])


class DetachedHeadTests(Base):
    def test_currently_detached_unreachable_commit_is_unmerged_work(self):
        data = self.create()
        git(data["path"], "checkout", "-q", "--detach")
        sha = commit_file(data["path"], "x.txt", "x\n", msg="detached work")
        res = self.wm.cleanup("r1", "u1", "main")
        self.assertFalse(res["ok"])
        self.assertIn("no_orphaned_commits", self.failed(res))
        self.assertIn(sha, res["data"]["orphaned_commits"])
        self.assertTrue(os.path.isdir(data["path"]))
        res = self.wm.cleanup("r1", "u1", "main", archive=True)
        self.assertTrue(res["ok"], res)
        self.assert_restorable(res, sha, "refs/ecc-archive/r1/u1/detached-head")

    def test_detached_commit_orphaned_by_checkout_back(self):
        # p6: commit while detached, then return to the unit branch.
        data = self.create()
        git(data["path"], "checkout", "-q", "--detach")
        sha = commit_file(data["path"], "x.txt", "x\n", msg="detached work")
        git(data["path"], "checkout", "-q", data["branch"])
        res = self.wm.cleanup("r1", "u1", "main")
        self.assertFalse(res["ok"])
        self.assertIn(sha, res["data"]["orphaned_commits"])
        res = self.wm.cleanup("r1", "u1", "main", archive=True)
        self.assertTrue(res["ok"], res)
        self.assert_restorable(res, sha, f"refs/ecc-archive/r1/u1/orphan-{sha[:12]}")

    def assert_restorable(self, res, sha, ref):
        rb = res["rollback_checkpoint"]
        self.assertIn(ref, rb["archived_refs"])
        heads = git(self.repo, "bundle", "list-heads", rb["bundle"])
        self.assertIn(f"{sha} {ref}", heads)
        self.assertEqual(self.archive_refs(), "")  # temporary refs removed from the shared repo
        # The bundle is thin (the target is a prerequisite): restore into the repository.
        git(self.repo, "fetch", "-q", rb["bundle"], f"{ref}:refs/heads/restored-{sha[:6]}")
        self.assertEqual(git(self.repo, "rev-parse", f"restored-{sha[:6]}"), sha)
        self.assertEqual(rb["restore_refs"][:3], ["git", "fetch", rb["bundle"]])

    def test_detached_at_reachable_commit_is_fine(self):
        data = self.create()
        git(data["path"], "checkout", "-q", "--detach")
        res = self.wm.cleanup("r1", "u1", "main")
        self.assertTrue(res["ok"], res)

    def test_amended_away_branch_commit_is_not_orphan(self):
        data = self.create()
        commit_file(data["path"], "a.txt", "1\n", msg="first")
        git(data["path"], "commit", "-q", "--amend", "-m", "amended")
        git(self.repo, "merge", "-q", "--ff-only", data["branch"])
        res = self.wm.cleanup("r1", "u1", "main")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["data"]["orphaned_commits"], [])

    def test_head_on_other_branch_still_refused(self):
        data = self.create()
        git(data["path"], "checkout", "-q", "-b", "elsewhere")
        res = self.wm.cleanup("r1", "u1", "main", archive=True)
        self.assertFalse(res["ok"])
        self.assertIn("head_on_unit_branch", self.failed(res))
        self.assertTrue(os.path.isdir(data["path"]))


if __name__ == "__main__":
    unittest.main()
