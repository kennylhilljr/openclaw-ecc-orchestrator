"""scripts/plan_lint.py lints a plan against a repository's committed HEAD."""

import copy
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from openclaw_ecc_orchestrator.runs.plan_lint import lint_plan  # noqa: E402

SCRIPT = REPO_ROOT / "scripts" / "plan_lint.py"
FIXTURE = REPO_ROOT / "fixtures" / "plans" / "multi-unit.json"
COUPLING = {"source": "src/models.py", "generated": "schemas/model.json"}
POLICY = {
    "schema_version": "1.0",
    "high_risk_paths": [],
    "required_checks": [],
    "protected_commands": [],
    "allowed_providers": ["claude"],
    "budget": {"max_cost_usd_per_unit": 1, "max_cost_usd_total": 5,
               "max_minutes_per_unit": 30},
    "generated_file_couplings": [COUPLING],
}
COMMITTED = ["schemas/work_unit.schema.json", "docs/runtime.md", "src/models.py",
             "schemas/model.json"]


def _env():
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(name, None)
    env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
                "GIT_CONFIG_NOSYSTEM": "1"})
    return env


@unittest.skipUnless(shutil.which("git"), "git is required")
class PlanLintScriptTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "commit.gpgsign", "false")
        for path in COMMITTED:
            self.write(path, "x\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        self.plan = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, env=_env(),
                       capture_output=True)

    def write(self, rel, text):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def plan_file(self, plan, name="plan.json"):
        path = self.tmp / name
        path.write_text(json.dumps(plan), encoding="utf-8")
        return path

    def raw_file(self, text, name):
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return path

    def run_script(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                              cwd=str(cwd or self.repo), env=_env(), capture_output=True,
                              text=True, timeout=60)

    def test_clean_plan_exits_zero_with_the_full_result(self):
        result = self.run_script(self.plan_file(self.plan))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(set(out), {"ok", "findings", "finding_digest", "exemptions"})
        self.assertTrue(out["ok"])
        self.assertEqual(out, lint_plan(self.plan, target_paths=COMMITTED))

    def test_plain_units_list_is_accepted(self):
        result = self.run_script(self.plan_file(self.plan["units"]))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         lint_plan(self.plan["units"], target_paths=COMMITTED))

    def test_failing_plan_exits_one(self):
        plan = copy.deepcopy(self.plan)
        plan["units"][1]["requires"] = ["missing-token"]
        result = self.run_script(self.plan_file(plan))
        self.assertEqual(result.returncode, 1, result.stderr)
        out = json.loads(result.stdout)
        self.assertFalse(out["ok"])
        self.assertIn("requirements.unsatisfied", {f["code"] for f in out["findings"]})
        self.assertEqual(out, lint_plan(plan, target_paths=COMMITTED))

    def test_target_paths_come_from_committed_head_only(self):
        plan = copy.deepcopy(self.plan)
        plan["units"][1]["acceptance"]["paths"] = ["docs/untracked.md", "docs/staged.md"]
        self.write("docs/untracked.md", "u\n")
        self.write("docs/staged.md", "s\n")
        self.git("add", "docs/staged.md")
        (self.repo / "docs/runtime.md").unlink()  # deleted in the work tree only
        result = self.run_script(self.plan_file(plan))
        self.assertEqual(result.returncode, 1, result.stderr)
        out = json.loads(result.stdout)
        missing = [f for f in out["findings"] if f["code"] == "acceptance.path_missing"]
        flagged = {p for f in missing for p in f["paths"]}
        self.assertIn("docs/untracked.md", flagged)
        self.assertIn("docs/staged.md", flagged)
        self.assertEqual(out, lint_plan(plan, target_paths=COMMITTED))

        self.git("commit", "-q", "-m", "stage")
        out = json.loads(self.run_script(self.plan_file(plan)).stdout)
        flagged = {p for f in out["findings"] if f["code"] == "acceptance.path_missing"
                   for p in f["paths"]}
        self.assertNotIn("docs/staged.md", flagged)
        self.assertIn("docs/untracked.md", flagged)

    def test_default_policy_generates_coupling_findings(self):
        plan = copy.deepcopy(self.plan)
        plan["units"][0]["scope"]["files"] = ["src/models.py"]
        clean = self.run_script(self.plan_file(plan))
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
        self.write(".orchestration/config.yaml", json.dumps(POLICY))
        result = self.run_script(self.plan_file(plan))
        self.assertEqual(result.returncode, 1, result.stderr)
        out = json.loads(result.stdout)
        self.assertIn("generated.coupling", {f["code"] for f in out["findings"]})
        self.assertEqual(out, lint_plan(plan, target_paths=COMMITTED, policy=POLICY))

    def test_explicit_policy_and_repo_flags(self):
        plan = copy.deepcopy(self.plan)
        plan["units"][0]["scope"]["files"] = ["src/models.py"]
        policy = self.tmp / "policy.json"
        policy.write_text(json.dumps(POLICY), encoding="utf-8")
        result = self.run_script(self.plan_file(plan), "--repo", self.repo / "docs",
                                 "--policy", policy, cwd=self.tmp)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("generated.coupling",
                      {f["code"] for f in json.loads(result.stdout)["findings"]})

    def test_runs_without_pythonpath(self):
        result = subprocess.run([sys.executable, "-I", str(SCRIPT), str(self.plan_file(self.plan)),
                                 "--repo", str(self.repo)], cwd=str(self.tmp), env=_env(),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_input_errors_exit_two(self):
        bad_json = self.tmp / "bad.json"
        bad_json.write_text("{not json", encoding="utf-8")
        bad_policy = self.tmp / "bad-policy.json"
        bad_policy.write_text(json.dumps({"schema_version": "1.0"}), encoding="utf-8")
        not_repo = self.tmp / "plain"
        not_repo.mkdir()
        good = self.plan_file(self.plan)
        cases = {
            "missing file": [self.tmp / "absent.json"],
            "bad json": [bad_json],
            "scalar": [self.plan_file(3, "scalar.json")],
            "object without units": [self.plan_file({"id": "x"}, "obj.json")],
            "units not a list": [self.plan_file({"units": {}}, "units.json")],
            "malformed scope": [self.plan_file([{"id": "x", "scope": "oops"}], "scope.json")],
            "malformed nested plan": [self.plan_file({"units": [{"id": "x", "scope": "oops"}]},
                                                     "nested.json")],
            "incomplete unit": [self.plan_file([{"id": "x"}], "incomplete.json")],
            "duplicate key": [self.raw_file('{"units": [], "units": []}', "dup.json")],
            "non-finite number": [self.raw_file('{"units": [{"budget": NaN}]}', "nan.json")],
            "invalid policy": [good, "--policy", bad_policy],
            "missing policy": [good, "--policy", self.tmp / "nope.json"],
            "not a repo": [good, "--repo", not_repo],
            "missing repo": [good, "--repo", self.tmp / "gone"],
            "usage": [],
        }
        for label, args in cases.items():
            with self.subTest(case=label):
                result = self.run_script(*args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)

    def test_repository_without_commits_is_an_error(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        subprocess.run(["git", "-C", str(empty), "init", "-q"], check=True, env=_env())
        result = self.run_script(self.plan_file(self.plan), "--repo", empty)
        self.assertEqual(result.returncode, 2)
        self.assertIn("error:", result.stderr)

    def test_does_not_write_into_the_repository(self):
        before = subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain",
                                 "--ignored"], capture_output=True, text=True, env=_env()).stdout
        self.run_script(self.plan_file(self.plan))
        after = subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain",
                                "--ignored"], capture_output=True, text=True, env=_env()).stdout
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
