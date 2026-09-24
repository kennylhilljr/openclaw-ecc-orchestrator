"""Fixtures in fixtures/ validate (or fail) exactly as their directory says."""

import pathlib
import unittest

from openclaw_ecc_orchestrator import schemas

ROOT = pathlib.Path(__file__).resolve().parents[1] / "fixtures"


def load(path):
    return schemas.load_document(path.read_text(encoding="utf-8"))


class FixtureTests(unittest.TestCase):
    def test_work_units(self):
        valid = sorted((ROOT / "work_units" / "valid").glob("*.json"))
        invalid = sorted((ROOT / "work_units" / "invalid").glob("*.json"))
        self.assertTrue(valid and invalid)
        for path in valid:
            with self.subTest(path=path.name):
                report = schemas.validate_work_unit(load(path))
                self.assertTrue(report.ok, report.errors)
        for path in invalid:
            with self.subTest(path=path.name):
                self.assertFalse(schemas.validate_work_unit(load(path)).ok)

    def test_handoffs(self):
        unit = load(ROOT / "work_units" / "valid" / "p1-03-downloader.json")
        for path in sorted((ROOT / "handoffs" / "valid").glob("*.json")):
            with self.subTest(path=path.name):
                report = schemas.validate_handoff(load(path), unit=unit)
                self.assertTrue(report.ok, report.errors + report.violations)
        expected = {"forged-success.json": "success_with_failures",
                    "out-of-scope.json": "file_outside_scope"}
        for name, kind in expected.items():
            with self.subTest(path=name):
                report = schemas.validate_handoff(load(ROOT / "handoffs" / "invalid" / name),
                                                  unit=unit)
                self.assertIn(kind, [v["kind"] for v in report.violations])

    def test_policy(self):
        text = (ROOT / "policy" / "config.json").read_text(encoding="utf-8")
        policy = schemas.load_repository_policy(text)
        self.assertIn("codex", policy["allowed_providers"])

    def test_fixtures_hold_no_secrets_or_personal_paths(self):
        for path in ROOT.rglob("*.json"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertFalse(schemas.contains_secret(text))
                self.assertNotIn("/Users/", text)
                self.assertNotIn("/home/", text)


if __name__ == "__main__":
    unittest.main()
