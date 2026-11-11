"""Tests for scripts/check.sh, driven through stub commands on PATH."""

import os
import pathlib
import subprocess
import tempfile
import textwrap
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check.sh"
PLUGIN_DIR = REPO_ROOT / "integrations" / "openclaw-plugin"
SKIP_LINE = (
    "SKIP: gitleaks not installed, install from "
    "https://github.com/gitleaks/gitleaks to run it locally"
)

# Each stub logs one tab-separated line: name, args, PWD, PYTHONPATH, TMPDIR,
# and whether it could create and delete a probe file inside TMPDIR.
STUB_TEMPLATE = textwrap.dedent(
    """\
    #!/bin/sh
    writable=no
    if [ -d "$TMPDIR" ]; then
      probe="$TMPDIR/stub-probe.$$"
      if : > "$probe" 2>/dev/null && rm "$probe"; then
        writable=yes
      fi
    fi
    printf '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' {name} "$*" "$PWD" "$PYTHONPATH" "$TMPDIR" "$writable" >> "$STUB_LOG"
    exit "${{{env}:-0}}"
    """
)

STUBS = {
    "python3.11": "STUB_PYTHON_EXIT",
    "npm": "STUB_NPM_EXIT",
    "gitleaks": "STUB_GITLEAKS_EXIT",
}
CHECK_FOR_STUB = {"python3.11": "python", "npm": "node", "gitleaks": "secrets"}


class CheckScriptTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = pathlib.Path(tmp.name)
        self.stub_dir = self.work / "bin"
        self.stub_dir.mkdir()
        self.log_path = self.work / "stub.log"

    def write_stubs(self, names):
        for name in names:
            path = self.stub_dir / name
            path.write_text(STUB_TEMPLATE.format(name=name, env=STUBS[name]))
            path.chmod(0o755)

    def run_check(self, extra_env=None):
        env = {
            "PATH": f"{self.stub_dir}:/usr/bin:/bin",
            "STUB_LOG": str(self.log_path),
        }
        env.update(extra_env or {})
        return subprocess.run(
            [str(SCRIPT)],
            env=env,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

    def log_entries(self):
        if not self.log_path.exists():
            return []
        entries = []
        for line in self.log_path.read_text().splitlines():
            name, args, pwd, pythonpath, tmpdir, writable = line.split("\t")
            entries.append(
                {
                    "name": name,
                    "args": args,
                    "pwd": pwd,
                    "pythonpath": pythonpath,
                    "tmpdir": tmpdir,
                    "writable": writable,
                }
            )
        return entries

    def entries_for(self, name):
        return [e for e in self.log_entries() if e["name"] == name]

    def assert_tmpdirs_removed(self):
        entries = self.log_entries()
        self.assertTrue(entries)
        for entry in entries:
            self.assertFalse(os.path.exists(entry["tmpdir"]), entry["tmpdir"])

    def test_all_pass(self):
        self.write_stubs(STUBS)
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK", result.stdout.splitlines())

        (py,) = self.entries_for("python3.11")
        self.assertEqual(py["args"], "-m unittest discover -s tests -t .")
        self.assertEqual(
            os.path.realpath(py["pwd"]), os.path.realpath(REPO_ROOT)
        )
        self.assertEqual(py["pythonpath"], "src")

        (npm,) = self.entries_for("npm")
        self.assertEqual(npm["args"], "test")
        self.assertEqual(
            os.path.realpath(npm["pwd"]), os.path.realpath(PLUGIN_DIR)
        )

        (gl,) = self.entries_for("gitleaks")
        self.assertEqual(
            gl["args"], "detect --source . --config .gitleaks.toml --redact"
        )

        self.assert_tmpdirs_removed()

    def test_each_failure_is_isolated(self):
        for failing, env_var in STUBS.items():
            with self.subTest(failing=failing):
                if self.log_path.exists():
                    self.log_path.unlink()
                self.write_stubs(STUBS)
                result = self.run_check({env_var: "1"})
                out = result.stdout.splitlines()
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(f"FAIL: {CHECK_FOR_STUB[failing]}", out)
                for other in STUBS:
                    if other == failing:
                        continue
                    self.assertIn(f"PASS: {CHECK_FOR_STUB[other]}", out)
                    self.assertEqual(len(self.entries_for(other)), 1)
                self.assertEqual(len(self.entries_for(failing)), 1)
                self.assert_tmpdirs_removed()

    def test_missing_gitleaks_is_skipped(self):
        self.write_stubs(["python3.11", "npm"])
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(SKIP_LINE, result.stdout.splitlines())
        self.assertIn("PASS: secrets", result.stdout.splitlines())
        self.assertEqual(self.entries_for("gitleaks"), [])

    def test_private_temp_folder(self):
        self.write_stubs(STUBS)
        parent_tmpdir = os.environ.get("TMPDIR", tempfile.gettempdir())

        first = self.run_check({"TMPDIR": parent_tmpdir})
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        (py,) = self.entries_for("python3.11")
        self.assertTrue(py["tmpdir"].startswith("/tmp/ecc-check."), py)
        self.assertNotEqual(py["tmpdir"], parent_tmpdir)
        self.assertEqual(py["writable"], "yes")

        self.log_path.unlink()
        second = self.run_check()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        (py2,) = self.entries_for("python3.11")
        self.assertNotEqual(py2["tmpdir"], py["tmpdir"])

        self.log_path.unlink()
        missing = "/tmp/does-not-exist-xyz"
        self.assertFalse(os.path.exists(missing))
        third = self.run_check({"TMPDIR": missing})
        self.assertEqual(third.returncode, 0, third.stdout + third.stderr)
        for entry in self.log_entries():
            self.assertTrue(entry["tmpdir"].startswith("/tmp/ecc-check."))
            self.assertEqual(entry["writable"], "yes")
        self.assert_tmpdirs_removed()


if __name__ == "__main__":
    unittest.main()
