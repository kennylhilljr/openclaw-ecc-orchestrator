"""Adversarial inputs against the schema validators and loaders."""

import json
import unittest

from openclaw_ecc_orchestrator import schemas
from openclaw_ecc_orchestrator.tasks.scope import path_in_scope, unsafe_path_reason

SECRET = "sk-ant-api03-" + "Qm7Rt5Yu3Io1Pa9Sd8Fg6Hj4Kl2Zx0Cv"


def unit(**overrides):
    doc = {
        "schema_version": "1.0", "id": "ADV-1", "title": "t", "depends_on": [],
        "scope": {"files": ["src/pkg/**"]}, "acceptance": {"commands": ["make test"]},
        "risk": "low", "capabilities": {"network": False, "secrets": []},
        "routing": {"initial_tier": 0, "maximum_tier": 2, "reviewer_must_differ_from_author": True},
        "budget": {"attempts": 2, "minutes": 10, "maximum_cost_usd": 1.0}, "rollback": "revert",
    }
    doc.update(overrides)
    return doc


def handoff(**overrides):
    doc = {
        "schema_version": "1.0", "unit_id": "ADV-1", "status": "succeeded", "outcome": "done",
        "files_changed": ["src/pkg/a.py"], "behavior": "b",
        "commands": [{"command": "make test", "exit_code": 0, "result": "ok"}],
        "unresolved_failures": [], "assumptions": [], "risks": [], "next_action": "merge",
        "commit": {"sha": "abcdef1", "branch": "b", "worktree": "w"}, "usage": None,
        "user_input_required": False,
    }
    doc.update(overrides)
    return doc


TRAVERSALS = [
    "..", "../x", "a/..", "./../x", "a/./../../x", "a/b/../../../x", "..\\x", "a\\..\\..\\x",
    "%2e%2e/x", "%2E%2E%2Fx", "..%2fx", "%252e%252e/x", "\uff0e\uff0e/x", "/etc/passwd",
    "//server/share", "\\\\server\\share", "C:/x", "c:x", "C:\\x", "~/x", "~root/x",
    "a/\x00/b", "a\n/b", "a\t/b", "\x7f", "", "   ",
]


class PathTraversalTests(unittest.TestCase):
    def test_every_variant_rejected_in_scope(self):
        for bad in TRAVERSALS:
            with self.subTest(bad=repr(bad)):
                self.assertIsNotNone(unsafe_path_reason(bad))
                self.assertFalse(schemas.validate_work_unit(unit(scope={"files": [bad]})).ok)

    def test_every_variant_is_a_handoff_violation(self):
        for bad in TRAVERSALS:
            with self.subTest(bad=repr(bad)):
                report = schemas.validate_handoff(handoff(files_changed=[bad]),
                                                  unit=unit(scope={"files": ["**"]}))
                self.assertFalse(report.ok)

    def test_traversal_never_in_scope_even_for_catch_all(self):
        for bad in TRAVERSALS:
            with self.subTest(bad=repr(bad)):
                self.assertFalse(path_in_scope(bad, ["**", "*"]))

    def test_benign_dot_names_allowed(self):
        for ok in ("a/...", ".github/workflows/ci.yml", "a/.b/c", "./src/x.py"):
            with self.subTest(ok=ok):
                self.assertIsNone(unsafe_path_reason(ok))

    def test_high_risk_policy_globs_cannot_escape(self):
        policy = {"schema_version": "1.0", "high_risk_paths": ["../**"], "required_checks": [],
                  "protected_commands": [], "allowed_providers": ["codex"],
                  "budget": {"max_cost_usd_per_unit": 1, "max_cost_usd_total": 1,
                             "max_minutes_per_unit": 1}}
        self.assertFalse(schemas.validate_repository_policy(policy).ok)


class ForgedHandoffTests(unittest.TestCase):
    def check(self, doc, u=None):
        return schemas.validate_handoff(doc, unit=u or unit())

    def test_forgeries(self):
        cases = [
            ("success with failing exit", handoff(commands=[
                {"command": "make test", "exit_code": 0, "result": "ok"},
                {"command": "make lint", "exit_code": 2, "result": "E501"}])),
            ("exit code as string", handoff(commands=[
                {"command": "make test", "exit_code": "0", "result": "ok"}])),
            ("exit code as bool", handoff(commands=[
                {"command": "make test", "exit_code": False, "result": "ok"}])),
            ("missing exit code", handoff(commands=[{"command": "make test", "result": "ok"}])),
            ("no commands", handoff(commands=[])),
            ("unresolved failures", handoff(unresolved_failures=["x"])),
            ("lookalike sibling dir", handoff(files_changed=["src/pkg2/a.py"])),
            ("prefix trick", handoff(files_changed=["src/pkg"])),
            ("escape through scope", handoff(files_changed=["src/pkg/../../etc/passwd"])),
            ("other unit", handoff(unit_id="ADV-2")),
            ("unknown extra field", dict(handoff(), approved_by_reviewer=True)),
            ("wrong version", handoff(schema_version="1.1")),
            ("user_input_required as string", handoff(user_input_required="no")),
        ]
        for label, doc in cases:
            with self.subTest(label):
                self.assertFalse(self.check(doc).ok)

    def test_outside_scope_reported_as_violation_not_error(self):
        report = self.check(handoff(files_changed=["src/pkg/a.py", "setup.py"]))
        self.assertEqual([v["kind"] for v in report.violations], ["file_outside_scope"])
        self.assertEqual(report.errors, [])
        env = report.to_envelope("validate_handoff")
        self.assertFalse(env["ok"])

    def test_forged_verification_result(self):
        doc = {"schema_version": "1.0", "unit_id": "ADV-1", "passed": True, "checks": [],
               "verified_at": "2026-09-24T00:00:00Z"}
        self.assertFalse(schemas.validate_verification_result(doc).ok)


class SecretLeakageTests(unittest.TestCase):
    def blob(self, report):
        return json.dumps(report.to_envelope("x"))

    def test_secret_in_id_is_not_echoed(self):
        report = schemas.validate_work_unit(unit(id=SECRET))
        self.assertFalse(report.ok)
        self.assertNotIn(SECRET, self.blob(report))
        self.assertNotIn("Qm7Rt5Yu3Io1", self.blob(report))

    def test_secret_in_unknown_field_name_not_echoed(self):
        doc = unit()
        doc[SECRET] = 1
        report = schemas.validate_work_unit(doc)
        self.assertFalse(report.ok)
        self.assertNotIn(SECRET, self.blob(report))

    def test_secret_in_scope_path_not_echoed(self):
        report = schemas.validate_work_unit(unit(scope={"files": ["/" + SECRET]}))
        self.assertNotIn(SECRET, self.blob(report))

    def test_secret_in_handoff_any_field(self):
        for field in ("outcome", "behavior", "next_action"):
            with self.subTest(field=field):
                report = schemas.validate_handoff(handoff(**{field: "x " + SECRET}), unit=unit())
                self.assertFalse(report.ok)
                self.assertNotIn(SECRET, self.blob(report))
        report = schemas.validate_handoff(handoff(risks=["token " + SECRET]), unit=unit())
        self.assertIn("risks[0]", [v.get("field") for v in report.violations])
        report = schemas.validate_handoff(handoff(files_changed=["src/pkg/" + SECRET]),
                                          unit=unit())
        self.assertFalse(report.ok)
        self.assertNotIn(SECRET, self.blob(report))

    def test_secret_in_usage_block(self):
        report = schemas.validate_handoff(handoff(usage={"model": SECRET}), unit=unit())
        self.assertFalse(report.ok)
        self.assertNotIn(SECRET, self.blob(report))

    def test_secret_in_certification_record(self):
        doc = {"schema_version": "1.0", "runner": "groq", "provider": "groq", "status": "failed",
               "certified_at": "2026-09-24T00:00:00Z", "version": None, "models": [],
               "checks": [{"name": "installed", "status": "fail", "reason": "boom " + SECRET}]}
        report = schemas.validate_certification_record(doc)
        self.assertFalse(report.ok)
        self.assertNotIn(SECRET, self.blob(report))

    def test_loader_errors_do_not_echo(self):
        text = '{"id": "a", "%s": 1, "%s": 2}' % (SECRET, SECRET)
        with self.assertRaises(schemas.DocumentError) as ctx:
            schemas.load_document(text)
        self.assertNotIn(SECRET, str(ctx.exception))
        text = json.dumps(unit(id=SECRET))
        with self.assertRaises(schemas.DocumentError) as ctx:
            schemas.load_work_unit(text)
        self.assertNotIn(SECRET, str(ctx.exception) + json.dumps(ctx.exception.errors))

    def test_loader_survives_deep_nesting(self):
        with self.assertRaises(schemas.DocumentError):
            schemas.load_document("[" * 200000 + "]" * 200000)


class RetiredProviderConfigTests(unittest.TestCase):
    def policy(self, **overrides):
        doc = {"schema_version": "1.0", "high_risk_paths": [], "required_checks": [],
               "protected_commands": [], "allowed_providers": ["codex"],
               "budget": {"max_cost_usd_per_unit": 1, "max_cost_usd_total": 1,
                          "max_minutes_per_unit": 1}}
        doc.update(overrides)
        return doc

    def test_retired_names_everywhere(self):
        variants = ["windsurf", "WINDSURF", " pi", "Pi", "openai-api", "openai_api", "openai",
                    "openai-api-coding"]
        for name in variants:
            for field, value in (
                    ("allowed_providers", ["codex", name]),
                    ("model_preferences", {name: {"economical": ["*"]}}),
                    ("cli_models", {name: {"standard": "x"}}),
                    ("runner_costs", {name: {"standard": 0.1}})):
                with self.subTest(name=name, field=field):
                    report = schemas.validate_repository_policy(self.policy(**{field: value}))
                    self.assertFalse(report.ok)

    def test_retired_runner_in_routing_decision(self):
        doc = {"schema_version": "1.0", "unit_id": "ADV-1", "score": 0, "signals": [],
               "risk": "low", "minimum_tier": 0, "chosen_tier": 0, "maximum_tier": 0,
               "reasons": [], "runner": "Windsurf", "decided_at": "2026-09-24T00:00:00Z"}
        self.assertFalse(schemas.validate_routing_decision(doc).ok)

    def test_openrouter_wildcard_models_rejected(self):
        for model in ("*", "vendor/*", "free/model?", "[a]"):
            with self.subTest(model=model):
                report = schemas.validate_repository_policy(self.policy(
                    allowed_providers=["openrouter"],
                    openrouter={"approved_models": [model], "data_policy": "x"}))
                self.assertFalse(report.ok)


if __name__ == "__main__":
    unittest.main()
