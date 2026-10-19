"""The CLI's `certifications` option also reads a certification run summary
(`runners.certify --out` summary.json, and bin/recertify-weekly's
cert-latest.json), so a pilot can point at the weekly result directly."""

import unittest

try:
    from . import test_cli_support as S
except ImportError:  # discovered as a top level module
    import test_cli_support as S


class CertificationSummaryTest(S.CliTestBase):
    def write_summary(self, *records):
        self.write_json("certs.json", {
            "schema_version": "1.0", "dry_run": False, "excluded": [],
            "certified": [r["runner"] for r in records if r["status"] == "certified"],
            "gate": {"gate_met": True},
            "results": [{"record": r} for r in records],
        })

    def test_summary_results_are_accepted(self):
        self.write_summary(S.certification("codex"), S.certification("claude"))
        self.create_run()
        doc = self.ok(self.dispatch("alpha", "alpha.txt=a"))
        self.assertEqual(doc["data"]["state"], "reviewing")
        self.assertEqual(self.unit("alpha")["runner"], "codex")

    def test_failed_records_in_a_summary_are_not_certified(self):
        # Codex failed and Claude has no economical class, so tier 0 has no
        # eligible runner: proof that the failed record was not used.
        self.write_summary(dict(S.certification("codex"), status="failed"), S.certification("claude"))
        self.create_run()
        code, doc = self.review_plan(*self.dispatch_args("alpha", "alpha.txt=a"))
        self.assertNotEqual(code, 0)
        failed = {c["name"]: c.get("detail") for c in doc["checks"] if not c["ok"]}
        self.assertIn("runner_selected", failed)
        self.assertIn("no eligible certified runner", failed["runner_selected"])

if __name__ == "__main__":
    unittest.main()
