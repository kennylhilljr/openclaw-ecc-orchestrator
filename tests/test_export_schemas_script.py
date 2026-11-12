"""scripts/export_schemas.py writes and checks the committed JSON Schema files."""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from openclaw_ecc_orchestrator import schemas  # noqa: E402

SCRIPT = REPO_ROOT / "scripts" / "export_schemas.py"
SCHEMA_DIR = REPO_ROOT / "schemas"


def _env_without_pythonpath():
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env


def run_script(*args, cwd=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd or tempfile.gettempdir(),
                          env=_env_without_pythonpath(), capture_output=True, text=True,
                          timeout=60)


def snapshot(directory):
    return {p.name: p.read_bytes() for p in sorted(pathlib.Path(directory).iterdir())}


class ExportSchemasScriptTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = pathlib.Path(self._tmp.name) / "out"

    def test_generates_every_file_into_an_empty_directory(self):
        self.out.mkdir()
        result = run_script("--output-dir", str(self.out))
        self.assertEqual(result.returncode, 0, result.stderr)
        files = snapshot(self.out)
        expected = {spec["file"] for spec in schemas.DOCUMENT_SPECS.values()}
        self.assertEqual(set(files), expected)
        for name, data in files.items():
            with self.subTest(file=name):
                text = data.decode("utf-8")
                self.assertTrue(text.endswith("}\n"))
                self.assertFalse(text.endswith("\n\n"))
                parsed = json.loads(text)
                self.assertEqual(parsed, schemas.json_schemas()[name])
                self.assertEqual(text, json.dumps(parsed, indent=2, sort_keys=True,
                                                  ensure_ascii=False) + "\n")

    def test_creates_a_missing_output_directory(self):
        result = run_script("--output-dir", str(self.out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(snapshot(self.out)), 8)

    def test_generation_is_repeatable(self):
        self.assertEqual(run_script("--output-dir", str(self.out)).returncode, 0)
        first = snapshot(self.out)
        second_run = run_script("--output-dir", str(self.out))
        self.assertEqual(second_run.returncode, 0, second_run.stderr)
        self.assertIn("0 of 8", second_run.stdout)
        self.assertEqual(snapshot(self.out), first)

    def test_generated_files_match_the_committed_files(self):
        self.assertEqual(run_script("--output-dir", str(self.out)).returncode, 0)
        self.assertEqual(snapshot(self.out),
                         {name: (SCHEMA_DIR / name).read_bytes() for name in snapshot(self.out)})

    def test_check_passes_for_fresh_output(self):
        self.assertEqual(run_script("--output-dir", str(self.out)).returncode, 0)
        result = run_script("--output-dir", str(self.out), "--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("up to date", result.stdout)

    def test_check_passes_for_the_repository(self):
        result = run_script("--check", cwd=REPO_ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_check_reports_a_changed_file_and_does_not_write(self):
        self.assertEqual(run_script("--output-dir", str(self.out)).returncode, 0)
        target = self.out / "run_status.schema.json"
        target.write_text(target.read_text(encoding="utf-8").replace("Run status", "Changed"),
                          encoding="utf-8")
        before = snapshot(self.out)
        result = run_script("--output-dir", str(self.out), "--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("differs:", result.stdout)
        self.assertIn("run_status.schema.json", result.stdout)
        self.assertEqual(snapshot(self.out), before)

    def test_check_reports_formatting_only_drift(self):
        self.assertEqual(run_script("--output-dir", str(self.out)).returncode, 0)
        target = self.out / "usage_record.schema.json"
        target.write_text(json.dumps(json.loads(target.read_text(encoding="utf-8")), indent=4),
                          encoding="utf-8")
        result = run_script("--output-dir", str(self.out), "--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("usage_record.schema.json", result.stdout)

    def test_check_reports_a_missing_file_and_does_not_write(self):
        self.assertEqual(run_script("--output-dir", str(self.out)).returncode, 0)
        (self.out / "handoff.schema.json").unlink()
        before = snapshot(self.out)
        result = run_script("--output-dir", str(self.out), "--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing:", result.stdout)
        self.assertIn("handoff.schema.json", result.stdout)
        self.assertEqual(snapshot(self.out), before)

    def test_check_does_not_create_a_missing_directory(self):
        result = run_script("--output-dir", str(self.out), "--check")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.out.exists())

    def test_output_path_that_is_a_file_is_an_error(self):
        self.out.write_text("not a directory", encoding="utf-8")
        self.assertEqual(run_script("--output-dir", str(self.out), "--check").returncode, 2)
        self.assertEqual(run_script("--output-dir", str(self.out)).returncode, 2)
        self.assertEqual(self.out.read_text(encoding="utf-8"), "not a directory")

    def test_unknown_option_is_a_usage_error(self):
        self.assertEqual(run_script("--bogus").returncode, 2)

    def test_runs_without_pythonpath_or_installation(self):
        result = subprocess.run([sys.executable, "-I", str(SCRIPT), "--check"],
                                cwd=tempfile.gettempdir(), env=_env_without_pythonpath(),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
