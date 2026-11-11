import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class TempdirRedirectTests(unittest.TestCase):
    def setUp(self):
        self.parent = tempfile.mkdtemp(dir="/tmp", prefix="ecc-synth-")
        self.openclaw_tmp = os.path.join(self.parent, ".openclaw", "tmp")
        os.makedirs(self.openclaw_tmp)
        self.home = os.path.join(self.parent, "home")
        os.makedirs(self.home)
        self.repo_root = Path(__file__).resolve().parent.parent
        self.src_dir = self.repo_root / "src"
        self.env = dict(os.environ, PYTHONPATH=str(self.src_dir), TMPDIR=self.openclaw_tmp, HOME=self.home)

    def tearDown(self):
        shutil.rmtree(self.parent, ignore_errors=True)

    def _run(self, test_dotted):
        return subprocess.run(
            [sys.executable, "-m", "unittest", "-v", test_dotted],
            cwd=str(self.repo_root),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_worktree_test_passes_under_openclaw_tmpdir(self):
        proc = self._run("tests.test_worktrees.RootValidationTests.test_rejects_openclaw_dir")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("root_not_in_openclaw_or_git_dir", proc.stderr)
        self.assertNotIn("inside an .openclaw directory", proc.stderr)

    def test_cli_state_dir_test_passes_under_openclaw_tmpdir(self):
        proc = self._run("tests.test_cli.UsageAndConfigTest.test_flags_override_config_file")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("root_not_in_openclaw_or_git_dir", proc.stderr)
        self.assertNotIn("inside an .openclaw directory", proc.stderr)

    def test_guard_still_refuses_openclaw_state_dir(self):
        proc = self._run("tests.test_cli.DoctorTest.test_doctor_rejects_openclaw_locations")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
