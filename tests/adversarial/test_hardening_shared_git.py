"""Reviewer probe p11 as a regression: a supervised worker mutates shared git
state from its own worktree. Orchestrator git must not execute the planted
fsmonitor (nor leak its environment), and the guard must report every change."""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from openclaw_ecc_orchestrator.process.supervisor import Supervisor
from openclaw_ecc_orchestrator.worktrees.git import git_out, run_git
from openclaw_ecc_orchestrator.worktrees.guard import diff_shared_git_state, snapshot_shared_git_state
from openclaw_ecc_orchestrator.worktrees.manager import WorktreeManager

PY = sys.executable


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class SharedGitHardeningTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        with open(os.path.join(self.repo, "README.md"), "w") as fh:
            fh.write("base\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-q", "-m", "base")
        self.wm = WorktreeManager(self.repo, os.path.join(self.tmp, "wt"))
        self.unit = self.wm.create("r1", "u1", base_ref="main")["data"]

    def tearDown(self):
        self._tmp.cleanup()

    def test_worker_mutations_detected_and_not_executed(self):
        leak = os.path.join(self.tmp, "leak.txt")
        hooks = os.path.join(self.repo, ".git", "hooks")
        evil = (
            "import os, subprocess\n"
            "open('a.txt','w').write('x\\n')\n"
            "subprocess.run(['git','add','a.txt'], check=True)\n"
            "subprocess.run(['git','-c','user.name=w','-c','user.email=w@x','commit','-qm','w'], check=True)\n"
            "head = subprocess.run(['git','rev-parse','HEAD'],capture_output=True,text=True).stdout.strip()\n"
            "subprocess.run(['git','update-ref','refs/heads/main',head], check=True)\n"
            f"subprocess.run(['git','config','core.fsmonitor','env > {leak}; false'], check=True)\n"
            f"open({os.path.join(hooks, 'post-merge')!r},'w').write('#!/bin/sh\\nexit 0\\n')\n"
        )
        before = snapshot_shared_git_state(self.repo)
        sup = Supervisor(base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
        res = sup.run([PY, "-c", evil], cwd=self.unit["path"], timeout=60)
        self.assertEqual(res["exit_code"], 0, res)

        with mock.patch.dict(os.environ, {"ORCH_ONLY_SECRET": "sk-orchestrator-parent-secret-123456"}):
            git_out(["status", "--porcelain"], self.unit["path"])
            run_git(["status", "--porcelain"], self.repo)
            after = snapshot_shared_git_state(self.repo)
        self.assertFalse(os.path.exists(leak), "orchestrator git executed a worker planted fsmonitor")

        changes = diff_shared_git_state(before, after, allow_refs=[self.unit["branch"]])
        text = "\n".join(changes)
        self.assertIn("ref moved: refs/heads/main", text)
        self.assertIn("risky config added: config:core.fsmonitor", text)
        self.assertIn("hook added: hooks/post-merge", text)
        self.assertNotIn(self.unit["branch"], text)
        self.assertNotIn("env >", text)


if __name__ == "__main__":
    unittest.main()
