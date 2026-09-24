"""Orchestrator git runs with a minimal env and safety overrides (review finding 2),
and the shared git state guard detects worker mutations (p11 style scenarios)."""

import os
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from openclaw_ecc_orchestrator.worktrees import git as gitmod
from openclaw_ecc_orchestrator.worktrees.git import git_env, run_git
from openclaw_ecc_orchestrator.worktrees.guard import diff_shared_git_state, snapshot_shared_git_state
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


def write_exec(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.repo = init_repo(os.path.join(self.tmp, "repo"))

    def tearDown(self):
        self._tmp.cleanup()


class GitEnvTests(Base):
    def test_minimal_environment(self):
        with mock.patch.dict(os.environ, {"ORCH_ONLY_SECRET": "sk-orchestrator-parent-secret-123456",
                                          "GIT_DIR": "/elsewhere", "GIT_SSH_COMMAND": "evil"}):
            env = git_env()
        self.assertNotIn("ORCH_ONLY_SECRET", env)
        self.assertNotIn("GIT_DIR", env)
        self.assertNotIn("GIT_SSH_COMMAND", env)
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["LC_ALL"], "C")
        allowed = set(gitmod.GIT_ENV_ALLOW) | {"GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT", "LC_ALL",
                                               "GIT_OPTIONAL_LOCKS"}
        self.assertLessEqual(set(env), allowed)

    def test_env_seen_by_git_children(self):
        # A shell alias shows exactly what git and its children receive.
        with mock.patch.dict(os.environ, {"ORCH_ONLY_SECRET": "sk-orchestrator-parent-secret-123456",
                                          "EXTRA_OK": "yes"}):
            out = run_git(["-c", "alias.showenv=!env", "showenv"], self.repo).stdout
            self.assertNotIn("ORCH_ONLY_SECRET", out)
            self.assertNotIn("EXTRA_OK=yes", out)
            out = run_git(["-c", "alias.showenv=!env", "showenv"], self.repo, env_allow=["EXTRA_OK"]).stdout
            self.assertIn("EXTRA_OK=yes", out)

    def test_safety_overrides_on_every_command(self):
        with mock.patch.object(gitmod.subprocess, "run", wraps=subprocess.run) as spy:
            run_git(["status", "--porcelain"], self.repo)
        argv = spy.call_args[0][0]
        joined = " ".join(argv)
        self.assertEqual(argv[0], "git")
        for needle in ("core.fsmonitor=false", "core.hooksPath=" + os.devnull, "core.sshCommand=",
                       "credential.helper="):
            self.assertIn(needle, joined)
        self.assertLess(argv.index("status"), len(argv))
        self.assertLess(max(i for i, a in enumerate(argv) if a == "-c"), argv.index("status"))

    def test_fsmonitor_in_shared_config_is_not_executed(self):
        leak = os.path.join(self.tmp, "leak.txt")
        script = os.path.join(self.tmp, "fsmon.sh")
        write_exec(script, f"#!/bin/sh\nenv > {leak}\nexit 1\n")
        git(self.repo, "config", "core.fsmonitor", script)
        with mock.patch.dict(os.environ, {"ORCH_ONLY_SECRET": "sk-orchestrator-parent-secret-123456"}):
            run_git(["status", "--porcelain"], self.repo)
        self.assertFalse(os.path.exists(leak))

    def test_hooks_are_not_run(self):
        marker = os.path.join(self.tmp, "hook-ran")
        write_exec(os.path.join(self.repo, ".git", "hooks", "post-checkout"), f"#!/bin/sh\ntouch {marker}\n")
        wm = WorktreeManager(self.repo, os.path.join(self.tmp, "wt"))
        self.assertTrue(wm.create("r1", "u1", base_ref="main")["ok"])
        self.assertFalse(os.path.exists(marker))


class GuardTests(Base):
    def setUp(self):
        super().setUp()
        self.wm = WorktreeManager(self.repo, os.path.join(self.tmp, "wt"))
        self.unit = self.wm.create("r1", "u1", base_ref="main")["data"]
        self.wt = self.unit["path"]
        self.before = snapshot_shared_git_state(self.repo)

    def diff(self, **kw):
        return diff_shared_git_state(self.before, snapshot_shared_git_state(self.repo), **kw)

    def unit_commit(self):
        with open(os.path.join(self.wt, "a.txt"), "w") as fh:
            fh.write("x\n")
        git(self.wt, "add", "a.txt")
        git(self.wt, "commit", "-q", "-m", "w")
        return git(self.wt, "rev-parse", "HEAD")

    def test_snapshot_shape(self):
        snap = self.before
        for key in ("common_dir", "head", "refs", "packed_refs_sha256", "config_sha256", "risky_config",
                    "hooks", "hooks_sha256"):
            self.assertIn(key, snap)
        self.assertEqual(snap["head"], "ref: refs/heads/main")
        self.assertIn("refs/heads/main", snap["refs"])
        self.assertIn("refs/heads/" + self.unit["branch"], snap["refs"])
        self.assertEqual(snapshot_shared_git_state(self.wt)["common_dir"], snap["common_dir"])

    def test_no_change_no_diff(self):
        self.assertEqual(self.diff(), [])

    def test_own_branch_allowed(self):
        self.unit_commit()
        self.assertEqual(self.diff(allow_refs=["refs/heads/" + self.unit["branch"]]), [])
        self.assertEqual(self.diff(allow_refs=[self.unit["branch"]]), [])
        changes = self.diff()
        self.assertEqual(len(changes), 1)
        self.assertIn(self.unit["branch"], changes[0])

    def test_update_ref_on_target_detected(self):
        head = self.unit_commit()
        git(self.wt, "update-ref", "refs/heads/main", head)
        changes = self.diff(allow_refs=[self.unit["branch"]])
        self.assertTrue(any("refs/heads/main" in c and "moved" in c for c in changes), changes)

    def test_new_tag_branch_and_remote_ref_detected(self):
        git(self.wt, "tag", "v9")
        git(self.wt, "branch", "side")
        git(self.wt, "update-ref", "refs/remotes/origin/fake", "HEAD")
        changes = " ".join(self.diff(allow_refs=[self.unit["branch"]]))
        for ref in ("refs/tags/v9", "refs/heads/side", "refs/remotes/origin/fake"):
            self.assertIn(ref, changes)

    def test_deleted_ref_detected(self):
        git(self.repo, "branch", "keep")
        before = snapshot_shared_git_state(self.repo)
        git(self.wt, "branch", "-D", "keep")
        changes = diff_shared_git_state(before, snapshot_shared_git_state(self.repo))
        self.assertTrue(any("refs/heads/keep" in c and "deleted" in c for c in changes), changes)

    def test_risky_config_detected(self):
        git(self.wt, "config", "core.fsmonitor", "env > /tmp/x; false")
        git(self.wt, "config", "alias.p", "push")
        git(self.wt, "config", "core.hooksPath", "/tmp/hooks")
        git(self.wt, "config", "include.path", "/tmp/evil.cfg")
        git(self.wt, "config", "credential.helper", "store")
        git(self.wt, "config", "remote.origin.url", "https://example.invalid/x.git")
        git(self.wt, "config", "filter.x.smudge", "evil")
        changes = "\n".join(self.diff())
        for key in ("core.fsmonitor", "alias.p", "core.hookspath", "include.path", "credential.helper",
                    "remote.origin.url", "filter.x.smudge"):
            self.assertIn(key, changes)
        self.assertIn("shared config", changes)
        self.assertNotIn("env > /tmp/x", changes)

    def test_benign_config_change_still_reported(self):
        git(self.wt, "config", "user.name", "Someone")
        changes = self.diff()
        self.assertEqual(len(changes), 1)
        self.assertIn("shared config", changes[0])

    def test_hook_added_detected(self):
        write_exec(os.path.join(self.before["common_dir"], "hooks", "pre-commit"), "#!/bin/sh\nexit 0\n")
        changes = self.diff()
        self.assertTrue(any("hook" in c and "pre-commit" in c for c in changes), changes)

    def test_head_retarget_detected(self):
        git(self.repo, "symbolic-ref", "HEAD", "refs/heads/other")
        changes = self.diff()
        self.assertTrue(any("HEAD" in c for c in changes), changes)

    def test_info_attributes_detected(self):
        with open(os.path.join(self.before["common_dir"], "info", "attributes"), "w") as fh:
            fh.write("* filter=x\n")
        self.assertTrue(any("info/attributes" in c for c in self.diff()))

    def test_snapshot_does_not_execute_repo_config(self):
        leak = os.path.join(self.tmp, "leak2.txt")
        script = os.path.join(self.tmp, "fsmon2.sh")
        write_exec(script, f"#!/bin/sh\ntouch {leak}\nexit 1\n")
        git(self.repo, "config", "core.fsmonitor", script)
        snapshot_shared_git_state(self.repo)
        self.assertFalse(os.path.exists(leak))


if __name__ == "__main__":
    unittest.main()
