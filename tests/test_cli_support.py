"""Shared harness for the operator CLI tests (no test methods of its own).

Builds a small real git repository, a JSON config file with every path
relative to the config file, a repository policy, runner certification
records and a plan, and runs the CLI in process with an injected TTY flag
and stdin.
"""

import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest

from openclaw_ecc_orchestrator.cli import main
from openclaw_ecc_orchestrator.schemas import CERTIFICATION_CHECKS

try:
    from . import _integration as H
    from ._units import policy as make_policy
    from ._units import work_unit
except ImportError:  # discovered as a top level module
    import _integration as H
    from _units import policy as make_policy
    from _units import work_unit

PY = sys.executable
OPERATOR = "op-alice"
SESSION = "sess-A"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def certification(runner, kind="coding_cli"):
    return {
        "schema_version": "1.0", "runner": runner, "provider": runner, "status": "certified",
        "checks": [{"name": name, "status": "pass", "reason": "ok"} for name in CERTIFICATION_CHECKS],
        "certified_at": "2026-01-01T00:00:00Z", "expires_at": "2099-01-01T00:00:00Z",
        "version": "1.0", "models": [], "kind": kind,
    }


def exists_cmd(path):
    return f"{shlex.quote(PY)} -c \"import os,sys; sys.exit(0 if os.path.exists('{path}') else 1)\""


class CliTestBase(unittest.TestCase):
    RUN_ID = "run1"

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
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.runner = os.path.join(self.tmp, "runner.py")
        with open(self.runner, "w") as fh:
            fh.write(H.RUNNER)
        self.state = os.path.join(self.tmp, "state")
        self.worktree_root = os.path.join(self.tmp, "agent-worktrees")
        self.events_path = os.path.join(self.state, "openclaw-events.jsonl")
        self.write_json("policy.json", make_policy())
        self.write_json("certs.json", [certification("codex"), certification("claude")])
        self.config = {
            "state_dir": "state", "worktree_root": "agent-worktrees", "repo": "repo",
            "target_branch": "main", "policy_file": "policy.json", "certifications": "certs.json",
            "repo_checks": {"required": ["unit-tests"], "commands": {"unit-tests": f"{shlex.quote(PY)} -c pass"}},
        }
        self.config_path = self.write_json("ecc.json", self.config)
        self.plan = {"units": [
            work_unit("alpha", files=["alpha.txt"], commands=[exists_cmd("alpha.txt")]),
            work_unit("beta", deps=["alpha"], files=["beta.txt"], commands=[exists_cmd("beta.txt")]),
        ]}
        self.plan_path = self.write_json("plan.json", self.plan)

    def tearDown(self):
        self._tmp.cleanup()

    # == files ==
    def write_json(self, name, obj):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            json.dump(obj, fh)
        return path

    def env(self):
        return {"HOME": self.home, "PATH": os.environ.get("PATH", "/usr/bin:/bin")}

    # == running the CLI ==
    def run_cli(self, argv, tty=False, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        code = main(list(argv), stdin=io.StringIO(stdin), stdout=out, stderr=err, env=self.env(),
                    is_tty=lambda: tty, cwd=self.tmp)
        return code, out.getvalue(), err.getvalue()

    def cli(self, *args, tty=False, stdin="", config=True):
        argv = list(args) + (["--config", self.config_path] if config else []) + ["--json"]
        code, out, err = self.run_cli(argv, tty=tty, stdin=stdin)
        try:
            doc = json.loads(out)
        except ValueError:
            self.fail(f"no JSON envelope (exit {code}) stdout={out!r} stderr={err!r}")
        return code, doc

    def ok(self, result):
        code, doc = result
        self.assertEqual(code, 0, json.dumps(doc, indent=1, default=str)[:4000])
        self.assertTrue(doc["ok"])
        return doc

    def review_plan(self, *cmd, approve=True, operator=OPERATOR, session=SESSION, review_id=None):
        argv = ["review-plan", "--json"]
        if approve:
            argv += ["--approve", "--operator", operator, "--session", session]
        if review_id:
            argv += ["--review-id", review_id]
        argv += ["--", *cmd, "--config", self.config_path]
        code, out, err = self.run_cli(argv)
        try:
            return code, json.loads(out)
        except ValueError:
            self.fail(f"no JSON envelope (exit {code}) stdout={out!r} stderr={err!r}")

    def reviewed(self, *cmd):
        """Write a review record for the plan of `cmd`, then run it with --yes."""
        doc = self.ok(self.review_plan(*cmd))
        return self.cli(*cmd, "--yes", "--review-id", doc["data"]["review_id"])

    # == inspection ==
    def events(self):
        if not os.path.exists(self.events_path):
            return []
        with open(self.events_path) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def snapshot(self):
        """Content digest of every file under the state dir and worktree root,
        plus every git ref, the worktree list and HEAD of the repository."""
        files = {}
        for root in (self.state, self.worktree_root):
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d != ".git"]
                for name in filenames:
                    path = os.path.join(dirpath, name)
                    if os.path.islink(path):
                        files[path] = "link:" + os.readlink(path)
                        continue
                    with open(path, "rb") as fh:
                        files[path] = hashlib.sha256(fh.read()).hexdigest()
                for name in dirnames:
                    files.setdefault(os.path.join(dirpath, name) + "/", "dir")
        refs = git(self.repo, "for-each-ref", "--format=%(refname) %(objectname)")
        worktrees = git(self.repo, "worktree", "list", "--porcelain")
        head = git(self.repo, "rev-parse", "HEAD")
        return {"files": files, "refs": refs, "worktrees": worktrees, "head": head}

    def unit(self, unit_id, run_id=None):
        doc = self.ok(self.cli("status", "--run-id", run_id or self.RUN_ID))
        return {u["unit_id"]: u for u in doc["data"]["units"]}[unit_id]

    # == lifecycle shortcuts (non interactive, through review records) ==
    def create_run(self):
        return self.ok(self.reviewed("create-run", "--run-id", self.RUN_ID, "--plan", self.plan_path))

    def runner_json(self, *specs, mode="write"):
        return json.dumps([PY, self.runner, mode, *specs])

    def dispatch_args(self, unit_id, *specs, mode="write"):
        return ("dispatch", "--run-id", self.RUN_ID, "--unit", unit_id, "--allow-test-runner",
                "--runner-command-json", self.runner_json(*specs, mode=mode))

    def dispatch(self, unit_id, *specs, mode="write"):
        return self.reviewed(*self.dispatch_args(unit_id, *specs, mode=mode))

    def review_args(self, unit_id, head, session=SESSION, reviewer="rev-1"):
        return ("review", "--run-id", self.RUN_ID, "--unit", unit_id, "--reviewer", reviewer, "--runner", "claude",
                "--model", "opus", "--tier", "2", "--verdict", "approved", "--head", head, "--session", session)

    def to_review(self, unit_id="alpha", specs=("alpha.txt=a",)):
        self.create_run()
        self.ok(self.dispatch(unit_id, *specs))
        head = self.unit(unit_id)["verified_head"]
        doc = self.ok(self.reviewed(*self.review_args(unit_id, head)))
        return head, doc["data"]["approval_request"]["request_id"]

    def approve_args(self, request_id, session=SESSION, operator=OPERATOR, verb="approve"):
        return ("approvals", verb, request_id, "--operator", operator, "--session", session)
