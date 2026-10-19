"""TEAM-DELIVERY-v2 section 9: preflight round trip (AT-TD-12).

Each defect found in the 2026-10-19 pilot is reintroduced and must fail
preflight; the fixed platform must pass."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from openclaw_ecc_orchestrator.cli import preflight as P
from openclaw_ecc_orchestrator.process.supervisor import Supervisor
from openclaw_ecc_orchestrator.runners.cli_adapters import ClaudeAdapter

PY = sys.executable

# Finds the handoff path in the prompt text only (as Claude print mode must),
# never in the environment. Modes: good | bad | nouv | oldcli
FAKE = r'''
import json, os, re, subprocess, sys
mode, prompt = sys.argv[1], sys.argv[2]
if "USER" not in os.environ:
    print(json.dumps({"type": "result", "is_error": True, "result": "Not logged in · Please run /login"}))
    sys.exit(1)
if mode == "oldcli":
    print("error: unknown option '--permission-prompts'", file=sys.stderr)
    sys.exit(1)
m = re.search(r"file-writing tool: (\S+) \(", prompt)
if not m:
    print("cannot tell where the handoff goes")
    sys.exit(0)
path = m.group(1)
with open("PREFLIGHT.txt", "w") as fh:
    fh.write("ok\n")
subprocess.run(["git", "add", "PREFLIGHT.txt"], check=True)
subprocess.run(["git", "commit", "-q", "-m", "preflight"], check=True)
head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
uv = 127 if mode == "nouv" else subprocess.run(["uv", "--version"], capture_output=True).returncode
commands = [{"command": "uv --version", "exit_code": uv, "result": "x"},
            {"command": "git status --short", "exit_code": 0, "result": ""}]
doc = {"schema_version": "1.0", "unit_id": "PREFLIGHT", "status": "succeeded" if mode != "bad" else "completed",
       "outcome": "ok", "files_changed": ["PREFLIGHT.txt"], "behavior": "ok", "commands": commands,
       "unresolved_failures": [] if uv == 0 else ["uv missing"], "assumptions": [], "risks": [],
       "next_action": "none", "commit": {"sha": head, "branch": "b", "worktree": "."}, "usage": None,
       "user_input_required": False}
if uv != 0:
    doc["status"] = "failed"
with open(path, "w") as fh:
    json.dump(doc, fh)
'''


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@unittest.skipUnless(subprocess.run(["which", "uv"], capture_output=True).returncode == 0, "uv not installed")
class PreflightTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        with open(os.path.join(self.repo, "README.md"), "w") as fh:
            fh.write("x\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-q", "-m", "base")
        self.fake = os.path.join(self.tmp, "fake_runner.py")
        with open(self.fake, "w") as fh:
            fh.write(FAKE)
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        # Git identity for the runner's commit comes from the repository config.
        self.sup = Supervisor(base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home,
                                        "USER": "tester", "LOGNAME": "tester"}, grace=1.0)

    def tearDown(self):
        self._tmp.cleanup()

    def run_preflight(self, mode, adapter=None):
        return P.preflight_runner(repo=self.repo, target_branch="main",
                                  worktree_root=os.path.join(self.tmp, "wt"), logs_dir=os.path.join(self.tmp, "logs"),
                                  runner="claude", adapter=adapter or ClaudeAdapter(), supervisor=self.sup,
                                  timeout=60, argv_override=[PY, self.fake, mode, P.PROMPT_TOKEN])

    def failed(self, res):
        return {c["name"] for c in res["checks"] if not c["ok"]}

    def test_at_td_12_fixed_platform_passes_and_cleans_up(self):
        """AT-TD-12: the round trip passes; the throwaway worktree and branch are gone."""
        res = self.run_preflight("good")
        self.assertTrue(res["ok"], json.dumps(res["checks"], indent=1))
        self.assertEqual(git(self.repo, "branch", "--list", "ecc-preflight/*"), "")
        self.assertNotIn("_preflight", git(self.repo, "worktree", "list"))

    def test_defect_missing_user_fails(self):
        """rmcp-m0b/m0c: the runner environment dropped USER."""
        adapter = ClaudeAdapter()
        adapter.runtime_env = ()
        res = self.run_preflight("good", adapter)
        self.assertIn("runner_exited_cleanly", self.failed(res))
        self.assertIn("runner_auth", json.dumps(res["checks"]))

    def test_defect_old_cli_fails(self):
        """rmcp-m0: an old claude rejected --permission-prompts."""
        res = self.run_preflight("oldcli")
        self.assertIn("runner_missing", json.dumps(res["checks"]))

    def test_defect_handoff_path_not_in_prompt_fails(self):
        """rmcp-m0d: the prompt named only the environment variable."""
        old = ("Finally write a JSON handoff document to the file named by the ECC_HANDOFF_PATH "
               "environment variable.")
        with mock.patch.object(P, "runner_prompt", lambda unit, brief=None: old):
            res = self.run_preflight("good")
        self.assertIn("handoff_written_to_path", self.failed(res))

    def test_defect_malformed_handoff_fails(self):
        """rmcp-m0e: the handoff did not match the schema."""
        res = self.run_preflight("bad")
        self.assertIn("handoff_valid", self.failed(res))

    def test_defect_no_uv_fails(self):
        """The Claude runner could not run uv, so units could not lock or run gates."""
        res = self.run_preflight("nouv")
        self.assertIn("runner_ran_gate_commands", self.failed(res))


if __name__ == "__main__":
    unittest.main()
