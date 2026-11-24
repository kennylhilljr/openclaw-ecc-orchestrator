"""scripts/check.sh runs the suite with TMPDIR=/tmp/ecc-check.XXXXXX (issue 57).

A mixed-case, dotted mktemp name made the redaction engine mask state paths,
which broke five CLI and gate tests. This runs those tests in a child process
under a deterministic dotted TMPDIR so the condition is covered by the suite.
"""

import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = (
    "tests.test_cli.BackupTest.test_backup_dir_copies_state_and_refs",
    "tests.test_cli.DestructiveCleanupTest.test_archiving_cleanup_needs_broker_approval_when_not_on_tty",
    "tests.test_cli.DestructiveCleanupTest.test_archiving_cleanup_on_tty",
    "tests.test_cli.UsageAndConfigTest.test_defaults_derive_from_xdg_dirs_at_runtime",
    "tests.test_gates_policy.RunGatesPolicyTests.test_tmpdir_root_is_configurable",
)


class DottedTmpdirTest(unittest.TestCase):
    def test_issue_tests_pass_under_dotted_tmpdir(self):
        with tempfile.TemporaryDirectory(prefix="ecc-check.Ab3Xy9-", dir="/tmp") as tmp:
            env = dict(os.environ)
            env["TMPDIR"] = tmp
            src = os.path.join(REPO, "src")
            env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            proc = subprocess.run([sys.executable, "-m", "unittest", *TESTS], cwd=REPO, env=env,
                                  capture_output=True, text=True, timeout=600)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
