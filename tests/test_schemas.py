"""Slice 1: versioned document validators (Python validator is authoritative)."""

import copy
import json
import unittest

from openclaw_ecc_orchestrator import schemas
from openclaw_ecc_orchestrator.handoffs import redaction


def work_unit(**overrides):
    doc = {
        "schema_version": "1.0",
        "id": "P1-03",
        "title": "Bounded atomic downloader",
        "depends_on": [],
        "scope": {"files": ["research_mcp/security.py", "tests/test_security_foundation.py"]},
        "acceptance": {"commands": ["pytest -q tests/test_security_foundation.py"]},
        "risk": "high",
        "capabilities": {"network": False, "secrets": []},
        "routing": {"initial_tier": 2, "maximum_tier": 2, "reviewer_must_differ_from_author": True},
        "budget": {"attempts": 2, "minutes": 45, "maximum_cost_usd": 3.00},
        "rollback": "Remove the additive module and tests.",
    }
    doc.update(overrides)
    return doc


def handoff(**overrides):
    doc = {
        "schema_version": "1.0",
        "unit_id": "P1-03",
        "status": "succeeded",
        "outcome": "Downloader implemented with size bound and atomic rename.",
        "files_changed": ["research_mcp/security.py", "tests/test_security_foundation.py"],
        "behavior": "Downloads are capped and written atomically.",
        "commands": [
            {"command": "pytest -q tests/test_security_foundation.py", "exit_code": 0,
             "result": "12 passed"},
        ],
        "unresolved_failures": [],
        "assumptions": ["Callers pass a writable directory."],
        "risks": [],
        "next_action": "Review and merge.",
        "commit": {"sha": "0123456789abcdef0123456789abcdef01234567",
                   "branch": "unit/P1-03", "worktree": ".worktrees/P1-03"},
        "usage": {"input_tokens": 1000, "output_tokens": 200, "cost_usd": 0.12, "model": "m"},
        "user_input_required": False,
    }
    doc.update(overrides)
    return doc


def policy(**overrides):
    doc = {
        "schema_version": "1.0",
        "high_risk_paths": ["**/security*.py", "migrations/**"],
        "required_checks": ["unit-tests"],
        "protected_commands": ["git push*", "rm -rf*"],
        "allowed_providers": ["claude", "codex", "gemini", "groq"],
        "budget": {"max_cost_usd_per_unit": 5.0, "max_cost_usd_total": 50.0,
                   "max_minutes_per_unit": 120},
    }
    doc.update(overrides)
    return doc


class WorkUnitTests(unittest.TestCase):
    def test_valid_unit_passes(self):
        report = schemas.validate_work_unit(work_unit())
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.errors, [])

    def test_missing_required_field(self):
        doc = work_unit()
        del doc["rollback"]
        report = schemas.validate_work_unit(doc)
        self.assertFalse(report.ok)
        self.assertTrue(any("rollback" in e for e in report.errors))

    def test_unknown_schema_version_rejected(self):
        for version in ("2.0", "1", 1.0, None, ""):
            with self.subTest(version=version):
                report = schemas.validate_work_unit(work_unit(schema_version=version))
                self.assertFalse(report.ok)
                self.assertTrue(any("schema_version" in e for e in report.errors))

    def test_bad_ids_rejected(self):
        for bad in ("", "-leading", ".hidden", "a" * 65, "has space", "semi;colon", "ü", 7):
            with self.subTest(bad=bad):
                self.assertFalse(schemas.validate_work_unit(work_unit(id=bad)).ok)
        self.assertTrue(schemas.validate_work_unit(work_unit(id="a" * 64)).ok)

    def test_bad_depends_on_ids_rejected(self):
        self.assertFalse(schemas.validate_work_unit(work_unit(depends_on=["../x"])).ok)
        self.assertFalse(schemas.validate_work_unit(work_unit(depends_on="P1-01")).ok)

    def test_unit_cannot_depend_on_itself(self):
        self.assertFalse(schemas.validate_work_unit(work_unit(depends_on=["P1-03"])).ok)

    def test_scope_paths_rejected(self):
        for bad in ("/etc/passwd", "../outside.py", "a/../../b.py", "C:\\x.py", "~/x",
                    "a\\..\\..\\b", "", "a\x00b"):
            with self.subTest(bad=bad):
                report = schemas.validate_work_unit(work_unit(scope={"files": [bad]}))
                self.assertFalse(report.ok)

    def test_scope_must_be_nonempty(self):
        self.assertFalse(schemas.validate_work_unit(work_unit(scope={"files": []})).ok)

    def test_scope_globs_allowed(self):
        self.assertTrue(schemas.validate_work_unit(work_unit(scope={"files": ["src/pkg/**/*.py"]})).ok)

    def test_negative_budgets_rejected(self):
        for field, value in (("attempts", -1), ("attempts", 0), ("minutes", -5),
                             ("maximum_cost_usd", -0.01), ("attempts", True),
                             ("maximum_cost_usd", "3")):
            with self.subTest(field=field, value=value):
                budget = {"attempts": 2, "minutes": 45, "maximum_cost_usd": 3.0}
                budget[field] = value
                self.assertFalse(schemas.validate_work_unit(work_unit(budget=budget)).ok)

    def test_tier_bounds(self):
        for init, maxi in ((-1, 2), (0, 3), (2, 1), (True, 2)):
            with self.subTest(init=init, maxi=maxi):
                routing = {"initial_tier": init, "maximum_tier": maxi,
                           "reviewer_must_differ_from_author": True}
                self.assertFalse(schemas.validate_work_unit(work_unit(routing=routing, risk="low")).ok)

    def test_initial_tier_above_maximum_message(self):
        routing = {"initial_tier": 2, "maximum_tier": 1, "reviewer_must_differ_from_author": True}
        report = schemas.validate_work_unit(work_unit(routing=routing, risk="low"))
        self.assertTrue(any("initial_tier" in e for e in report.errors))

    def test_risk_enum(self):
        self.assertFalse(schemas.validate_work_unit(work_unit(risk="critical")).ok)

    def test_capability_secret_names_must_be_env_var_names(self):
        ok = work_unit(capabilities={"network": True, "secrets": ["GROQ_API_KEY"]})
        self.assertTrue(schemas.validate_work_unit(ok).ok)
        bad = work_unit(capabilities={"network": True, "secrets": ["gsk_" + "A1b2" * 10]})
        report = schemas.validate_work_unit(bad)
        self.assertFalse(report.ok)
        self.assertNotIn("A1b2A1b2", json.dumps(report.errors))

    def test_optional_traits_enum(self):
        self.assertTrue(schemas.validate_work_unit(work_unit(traits=["mechanical"])).ok)
        self.assertFalse(schemas.validate_work_unit(work_unit(traits=["magic"])).ok)

    def test_non_dict_rejected(self):
        self.assertFalse(schemas.validate_work_unit([]).ok)
        self.assertFalse(schemas.validate_work_unit(None).ok)

    def test_report_envelope(self):
        env = schemas.validate_work_unit(work_unit(risk="nope")).to_envelope("validate_work_unit")
        for key in ("ok", "operation", "changed", "checks", "warnings",
                    "required_user_actions", "rollback_checkpoint"):
            self.assertIn(key, env)
        self.assertFalse(env["ok"])
        self.assertFalse(env["changed"])

    def test_validation_does_not_mutate(self):
        doc = work_unit()
        before = copy.deepcopy(doc)
        schemas.validate_work_unit(doc)
        self.assertEqual(doc, before)


class HandoffTests(unittest.TestCase):
    def test_valid_handoff(self):
        report = schemas.validate_handoff(handoff(), unit=work_unit())
        self.assertTrue(report.ok, report.errors + [str(v) for v in report.violations])

    def test_required_fields(self):
        for field in ("status", "outcome", "files_changed", "behavior", "commands",
                      "unresolved_failures", "assumptions", "risks", "next_action",
                      "commit", "usage", "user_input_required"):
            with self.subTest(field=field):
                doc = handoff()
                del doc[field]
                self.assertFalse(schemas.validate_handoff(doc).ok)

    def test_usage_may_be_null(self):
        self.assertTrue(schemas.validate_handoff(handoff(usage=None), unit=work_unit()).ok)

    def test_files_outside_scope_is_violation(self):
        doc = handoff(files_changed=["research_mcp/security.py", "src/other.py"])
        report = schemas.validate_handoff(doc, unit=work_unit())
        self.assertFalse(report.ok)
        self.assertEqual(len(report.violations), 1)
        self.assertEqual(report.violations[0]["kind"], "file_outside_scope")
        self.assertEqual(report.violations[0]["path"], "src/other.py")

    def test_scope_glob_matching(self):
        unit = work_unit(scope={"files": ["src/pkg/**"]})
        doc = handoff(files_changed=["src/pkg/a/b.py"], unit_id="P1-03")
        self.assertTrue(schemas.validate_handoff(doc, unit=unit).ok)

    def test_success_with_failing_command_rejected(self):
        doc = handoff(commands=[{"command": "pytest", "exit_code": 1, "result": "1 failed"}])
        report = schemas.validate_handoff(doc, unit=work_unit())
        self.assertFalse(report.ok)
        self.assertTrue(any(v["kind"] == "success_with_failures" for v in report.violations))

    def test_success_with_unresolved_failures_rejected(self):
        doc = handoff(unresolved_failures=["flaky test"])
        self.assertFalse(schemas.validate_handoff(doc, unit=work_unit()).ok)

    def test_failed_status_with_failing_commands_ok(self):
        doc = handoff(status="failed",
                      commands=[{"command": "pytest", "exit_code": 1, "result": "1 failed"}],
                      unresolved_failures=["test_x fails"])
        self.assertTrue(schemas.validate_handoff(doc, unit=work_unit()).ok)

    def test_success_without_commands_rejected(self):
        self.assertFalse(schemas.validate_handoff(handoff(commands=[]), unit=work_unit()).ok)

    def test_unit_id_mismatch(self):
        self.assertFalse(schemas.validate_handoff(handoff(unit_id="P9-99"), unit=work_unit()).ok)

    def test_commit_sha_format(self):
        doc = handoff(commit={"sha": "not-a-sha", "branch": "b", "worktree": "w"})
        self.assertFalse(schemas.validate_handoff(doc).ok)

    def test_secret_in_handoff_rejected_and_redacted(self):
        secret = "sk-ant-api03-" + "Zx9Qw8Er7Ty6Ui5Op4As3Df2Gh1Jk0Lz"
        doc = handoff(behavior="Configured client with key " + secret)
        report = schemas.validate_handoff(doc, unit=work_unit())
        self.assertFalse(report.ok)
        blob = json.dumps(report.to_envelope("validate_handoff"))
        self.assertNotIn(secret, blob)
        self.assertNotIn("Zx9Qw8Er7Ty6", blob)
        self.assertTrue(any(v["kind"] == "secret_detected" and v["field"] == "behavior"
                            for v in report.violations))

    def test_secret_nested_in_commands(self):
        doc = handoff(commands=[{"command": "curl -H 'Authorization: Bearer "
                                            "ghp_abcdefghijklmnopqrstuvwxyz0123456789'",
                                 "exit_code": 0, "result": "ok"}])
        report = schemas.validate_handoff(doc, unit=work_unit())
        self.assertFalse(report.ok)
        self.assertIn("commands[0].command", [v.get("field") for v in report.violations])


class PolicyTests(unittest.TestCase):
    def test_valid_policy(self):
        self.assertTrue(schemas.validate_repository_policy(policy()).ok)

    def test_retired_provider_rejected(self):
        for retired in ("windsurf", "pi", "openai-api", "Windsurf", " pi "):
            with self.subTest(retired=retired):
                report = schemas.validate_repository_policy(
                    policy(allowed_providers=["claude", retired]))
                self.assertFalse(report.ok)
                self.assertTrue(any("retired" in e for e in report.errors))

    def test_unknown_provider_rejected(self):
        self.assertFalse(schemas.validate_repository_policy(
            policy(allowed_providers=["claude", "mystery"])).ok)

    def test_negative_ceiling_rejected(self):
        self.assertFalse(schemas.validate_repository_policy(
            policy(budget={"max_cost_usd_per_unit": -1, "max_cost_usd_total": 1,
                           "max_minutes_per_unit": 1})).ok)

    def test_glob_lists_must_be_strings(self):
        self.assertFalse(schemas.validate_repository_policy(policy(high_risk_paths="x")).ok)
        self.assertFalse(schemas.validate_repository_policy(policy(high_risk_paths=["/abs/**"])).ok)

    def test_openrouter_requires_pinned_models_and_data_policy(self):
        doc = policy(allowed_providers=["claude", "openrouter"])
        self.assertFalse(schemas.validate_repository_policy(doc).ok)
        doc["openrouter"] = {"approved_models": ["vendor/model-a"], "data_policy": "no-training"}
        self.assertTrue(schemas.validate_repository_policy(doc).ok)
        doc["openrouter"]["approved_models"] = ["vendor/*"]
        self.assertFalse(schemas.validate_repository_policy(doc).ok)

    def test_circuit_breaker_settings(self):
        self.assertTrue(schemas.validate_repository_policy(
            policy(circuit_breaker={"failure_threshold": 3, "cooldown_seconds": 600})).ok)
        self.assertFalse(schemas.validate_repository_policy(
            policy(circuit_breaker={"failure_threshold": 0, "cooldown_seconds": 600})).ok)


class OtherDocumentTests(unittest.TestCase):
    def test_routing_decision(self):
        doc = {"schema_version": "1.0", "unit_id": "P1-03", "score": 4, "risk": "high",
               "minimum_tier": 2, "chosen_tier": 2, "maximum_tier": 2,
               "signals": [{"name": "security", "points": 4, "reason": "path"}],
               "reasons": ["risk high forces tier 2"], "runner": "codex",
               "provider": "codex", "model": None, "decided_at": "2026-09-24T00:00:00Z"}
        self.assertTrue(schemas.validate_routing_decision(doc).ok)
        bad = dict(doc, chosen_tier=3)
        self.assertFalse(schemas.validate_routing_decision(bad).ok)
        bad = dict(doc, chosen_tier=1)
        self.assertFalse(schemas.validate_routing_decision(bad).ok)

    def test_usage_record(self):
        doc = {"schema_version": "1.0", "unit_id": "P1-03", "runner": "gemini", "tier": 0,
               "attempt": 1, "cost_usd": 0.01, "elapsed_seconds": 12.5,
               "input_tokens": 10, "output_tokens": 5, "model": "x",
               "recorded_at": "2026-09-24T00:00:00Z"}
        self.assertTrue(schemas.validate_usage_record(doc).ok)
        self.assertFalse(schemas.validate_usage_record(dict(doc, cost_usd=-1)).ok)
        self.assertFalse(schemas.validate_usage_record(dict(doc, input_tokens=-3)).ok)

    def test_run_status(self):
        doc = {"schema_version": "1.0", "unit_id": "P1-03", "state": "running", "tier": 1,
               "attempt": 1, "updated_at": "2026-09-24T00:00:00Z"}
        self.assertTrue(schemas.validate_run_status(doc).ok)
        self.assertFalse(schemas.validate_run_status(dict(doc, state="exploded")).ok)

    def test_verification_result_consistency(self):
        doc = {"schema_version": "1.0", "unit_id": "P1-03", "passed": True,
               "verified_at": "2026-09-24T00:00:00Z",
               "checks": [{"name": "unit-tests", "command": "pytest", "exit_code": 0,
                           "passed": True}]}
        self.assertTrue(schemas.validate_verification_result(doc).ok)
        forged = copy.deepcopy(doc)
        forged["checks"][0].update(exit_code=2, passed=False)
        self.assertFalse(schemas.validate_verification_result(forged).ok)
        lying_check = copy.deepcopy(doc)
        lying_check["checks"][0]["exit_code"] = 1
        self.assertFalse(schemas.validate_verification_result(lying_check).ok)

    def test_certification_record(self):
        doc = {"schema_version": "1.0", "runner": "groq", "provider": "groq",
               "status": "certified", "certified_at": "2026-09-24T00:00:00Z",
               "version": None, "models": ["m"],
               "checks": [{"name": n, "status": "pass", "reason": ""}
                          for n in schemas.CERTIFICATION_CHECKS]}
        self.assertTrue(schemas.validate_certification_record(doc).ok)
        bad = copy.deepcopy(doc)
        bad["checks"][0]["status"] = "fail"
        self.assertFalse(schemas.validate_certification_record(bad).ok,
                         "certified status with a failing check must be rejected")
        self.assertFalse(schemas.validate_certification_record(dict(doc, runner="windsurf")).ok)


class LoaderTests(unittest.TestCase):
    def test_load_json_document(self):
        text = json.dumps(work_unit())
        self.assertEqual(schemas.load_document(text)["id"], "P1-03")

    def test_load_rejects_non_json(self):
        with self.assertRaises(schemas.DocumentError):
            schemas.load_document("id: P1-03\nrisk: high\n")

    def test_load_rejects_duplicate_keys(self):
        with self.assertRaises(schemas.DocumentError):
            schemas.load_document('{"risk": "low", "risk": "high"}')

    def test_load_rejects_non_finite(self):
        with self.assertRaises(schemas.DocumentError):
            schemas.load_document('{"budget": NaN}')

    def test_load_work_unit_raises_with_errors(self):
        with self.assertRaises(schemas.DocumentError) as ctx:
            schemas.load_work_unit(json.dumps(work_unit(risk="nope")))
        self.assertTrue(ctx.exception.errors)


class RedactionTests(unittest.TestCase):
    def test_known_prefixes_detected(self):
        samples = [
            "sk-proj-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4",
            "sk-or-v1-" + "0123456789abcdef0123456789abcdef",
            "gsk_" + "Q1w2E3r4T5y6U7i8O9p0A1s2D3f4G5h6",
            "AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q",
            "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789",
            "github_pat_" + "11ABCDEFG0123456789_abcdefghijklmnop",
            "xoxb-" + "123456789012-abcdefghijklmnop",
            "AKIA" + "ABCDEFGHIJKLMNOP",
            "-----BEGIN RSA PRIVATE KEY-----",
        ]
        for s in samples:
            with self.subTest(prefix=s[:6]):
                self.assertTrue(redaction.contains_secret("value " + s + " end"))
                self.assertNotIn(s, redaction.redact_text("value " + s + " end"))

    def test_high_entropy_token_detected(self):
        token = "Xq7Lp2Zr9Vt4Mb8Nc1Kd6Hf3Jg5Ws0Ye"  # gitleaks:allow
        self.assertTrue(redaction.contains_secret("token=" + token))

    def test_benign_text_not_flagged(self):
        for text in ("0123456789abcdef0123456789abcdef01234567",
                     "src/openclaw_ecc_orchestrator/runners/discovery.py",
                     "pytest -q tests/test_security_foundation.py",
                     "GROQ_API_KEY", "The quick brown fox jumps over the lazy dog."):
            with self.subTest(text=text):
                self.assertFalse(redaction.contains_secret(text))


if __name__ == "__main__":
    unittest.main()
