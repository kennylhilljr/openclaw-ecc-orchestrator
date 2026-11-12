"""scripts/routing_trace.py reports the real classifier's decision for each unit."""

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

from openclaw_ecc_orchestrator.routing.classify import classify  # noqa: E402

SCRIPT = REPO_ROOT / "scripts" / "routing_trace.py"

POLICY = {
    "schema_version": "1.0", "high_risk_paths": ["src/payments/**"],
    "required_checks": [], "protected_commands": [], "allowed_providers": ["claude"],
    "budget": {"max_cost_usd_per_unit": 5, "max_cost_usd_total": 50, "max_minutes_per_unit": 60},
}


def unit(uid="U-1", **overrides):
    doc = {
        "schema_version": "1.0", "id": uid, "title": "t", "depends_on": [],
        "scope": {"files": ["src/pkg/a.py", "tests/test_a.py"]},
        "acceptance": {"commands": ["python3 -m unittest tests.test_a"]},
        "risk": "low",
        "capabilities": {"network": False, "secrets": []},
        "routing": {"initial_tier": 0, "maximum_tier": 2, "reviewer_must_differ_from_author": True},
        "budget": {"attempts": 2, "minutes": 30, "maximum_cost_usd": 1.0},
        "rollback": "revert",
    }
    doc.update(overrides)
    return doc


def _env():
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    for name in ("GIT_DIR", "GIT_WORK_TREE"):
        env.pop(name, None)
    return env


def expected(units, policy=None):
    entries = []
    for item in units:
        entry = classify(item, policy).to_dict()
        entry["initial_tier"] = item["routing"]["initial_tier"]
        entries.append(entry)
    return {"ok": all(e["ok"] for e in entries), "classifications": entries}


@unittest.skipUnless(shutil.which("git"), "git is required")
class RoutingTraceScriptTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True, env=_env())

    def json_file(self, data, name="units.json"):
        path = self.tmp / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def run_script(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                              cwd=str(cwd or self.repo), env=_env(), capture_output=True,
                              text=True, timeout=60)

    def trace(self, data, *args, code=0):
        result = self.run_script(self.json_file(data), *args)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_single_unit_matches_classifier(self):
        item = unit()
        out = self.trace(item)
        self.assertEqual(out, expected([item]))
        entry = out["classifications"][0]
        for key in ("signals", "score", "score_tier", "risk", "declared_risk",
                    "risk_minimum_tier", "minimum_tier", "maximum_tier", "chosen_tier",
                    "review_tier", "reasons", "errors", "initial_tier"):
            self.assertIn(key, entry)

    def test_plan_object_and_units_list_match_classifier(self):
        units = [unit("A"), unit("B", traits=["security"], depends_on=["A"])]
        self.assertEqual(self.trace({"units": units}), expected(units))
        self.assertEqual(self.trace(units), expected(units))

    def test_signals_report_name_points_and_reason(self):
        item = unit(scope={"files": ["migrations/0001_init.sql"]})
        entry = self.trace(item)["classifications"][0]
        self.assertEqual(entry["signals"], [{"name": "database", "points": 3,
                                             "reason": "path migrations/0001_init.sql"}])
        self.assertEqual(entry["score"], 3)
        self.assertEqual(entry["score_tier"], 1)

    def test_mechanical_scoring(self):
        item = unit(traits=["mechanical"])
        entry = self.trace(item)["classifications"][0]
        self.assertEqual(entry, expected([item])["classifications"][0])
        self.assertEqual([s["name"] for s in entry["signals"]], ["mechanical_small"])
        self.assertEqual(entry["score"], -2)
        self.assertEqual(entry["chosen_tier"], 0)

    def test_default_policy_forces_risk_high(self):
        item = unit(scope={"files": ["src/payments/charge.py"]})
        before = self.trace(item)["classifications"][0]
        self.assertEqual(before["risk"], "low")
        config = self.repo / ".orchestration" / "config.yaml"
        config.parent.mkdir()
        config.write_text(json.dumps(POLICY), encoding="utf-8")
        out = self.trace(item, "--repo", self.repo)
        self.assertEqual(out, expected([item], POLICY))
        entry = out["classifications"][0]
        self.assertEqual(entry["declared_risk"], "low")
        self.assertEqual(entry["risk"], "high")
        self.assertEqual(entry["risk_minimum_tier"], 2)
        self.assertEqual(entry["chosen_tier"], 2)

    def test_explicit_policy_flag(self):
        item = unit(scope={"files": ["src/payments/charge.py"]})
        policy = self.json_file(POLICY, "policy.json")
        out = self.trace(item, "--policy", policy, "--repo", self.repo)
        self.assertEqual(out["classifications"][0]["risk"], "high")

    def test_initial_tier_raises_the_floor(self):
        item = unit(routing={"initial_tier": 1, "maximum_tier": 2,
                             "reviewer_must_differ_from_author": True})
        entry = self.trace(item)["classifications"][0]
        self.assertEqual(entry["initial_tier"], 1)
        self.assertEqual(entry["score_tier"], 0)
        self.assertEqual(entry["minimum_tier"], 1)
        self.assertEqual(entry["chosen_tier"], 1)

    def test_maximum_tier_caps_the_score(self):
        item = unit(traits=["security", "database"],
                    routing={"initial_tier": 0, "maximum_tier": 1,
                             "reviewer_must_differ_from_author": True})
        entry = self.trace(item)["classifications"][0]
        self.assertEqual(entry["score_tier"], 2)
        self.assertEqual(entry["chosen_tier"], 1)
        self.assertIn("capped at maximum_tier 1", entry["reasons"])

    def test_impossible_risk_floor_exits_one(self):
        item = unit(risk="high", routing={"initial_tier": 0, "maximum_tier": 1,
                                          "reviewer_must_differ_from_author": True})
        out = self.trace({"units": [unit("ok"), item]}, code=1)
        self.assertFalse(out["ok"])
        self.assertTrue(out["classifications"][0]["ok"])
        self.assertFalse(out["classifications"][1]["ok"])
        self.assertTrue(out["classifications"][1]["errors"])
        self.assertEqual(out, expected([unit("ok"), item]))

    def test_invalid_unit_exits_one(self):
        item = unit()
        del item["budget"]
        out = self.trace(item, code=1)
        self.assertFalse(out["classifications"][0]["ok"])
        self.assertIsNone(out["classifications"][0]["chosen_tier"])

    def test_malformed_nested_fields_are_reported_not_raised(self):
        item = {"id": "x", "scope": "oops", "routing": "r", "acceptance": 3}
        out = self.trace([item], code=1)
        entry = out["classifications"][0]
        self.assertFalse(entry["ok"])
        self.assertTrue(entry["errors"])
        self.assertIsNone(entry["initial_tier"])

    def test_runs_without_pythonpath(self):
        result = subprocess.run([sys.executable, "-I", str(SCRIPT), str(self.json_file(unit())),
                                 "--repo", str(self.repo)], cwd=str(self.tmp), env=_env(),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_malformed_input_exits_two(self):
        bad_json = self.tmp / "bad.json"
        bad_json.write_text("[", encoding="utf-8")
        bad_policy = self.json_file({"schema_version": "1.0"}, "bad-policy.json")
        good = self.json_file(unit(), "good.json")
        cases = {
            "missing file": [self.tmp / "absent.json"],
            "bad json": [bad_json],
            "scalar": [self.json_file(7, "scalar.json")],
            "unrelated object": [self.json_file({"name": "x"}, "obj.json")],
            "units not a list": [self.json_file({"units": {"id": "x"}}, "units.json")],
            "non-object unit": [self.json_file([unit(), "x"], "mixed.json")],
            "invalid policy": [good, "--policy", bad_policy],
            "missing policy": [good, "--policy", self.tmp / "nope.json"],
            "missing repo": [good, "--repo", self.tmp / "gone"],
            "usage": [],
        }
        for label, args in cases.items():
            with self.subTest(case=label):
                result = self.run_script(*args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
