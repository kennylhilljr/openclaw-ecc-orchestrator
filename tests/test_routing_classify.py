"""Slice 4a: complexity classification, table driven over every signal and override."""

import copy
import unittest

from openclaw_ecc_orchestrator import schemas
from openclaw_ecc_orchestrator.routing import classify as C


def unit(**overrides):
    doc = {
        "schema_version": "1.0", "id": "U-1", "title": "t", "depends_on": [],
        "scope": {"files": ["src/pkg/a.py", "tests/test_a.py"]},
        "acceptance": {"commands": ["python -m unittest tests.test_a"]},
        "risk": "low",
        "capabilities": {"network": False, "secrets": []},
        "routing": {"initial_tier": 0, "maximum_tier": 2, "reviewer_must_differ_from_author": True},
        "budget": {"attempts": 2, "minutes": 30, "maximum_cost_usd": 2.0},
        "rollback": "revert",
    }
    doc.update(overrides)
    return doc


POLICY = {
    "schema_version": "1.0", "high_risk_paths": ["src/payments/**", "**/security/*.py"],
    "required_checks": ["unit-tests"], "protected_commands": ["git push*"],
    "allowed_providers": ["codex", "gemini"],
    "budget": {"max_cost_usd_per_unit": 5, "max_cost_usd_total": 50, "max_minutes_per_unit": 60},
}


def signal_names(result):
    return {s["name"]: s["points"] for s in result.signals}


class SignalTableTests(unittest.TestCase):
    def test_baseline_has_no_signals(self):
        result = C.classify(unit(), POLICY)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.score, 0)
        self.assertEqual(result.signals, [])
        self.assertEqual(result.chosen_tier, 0)

    def test_each_signal(self):
        cases = [
            # (label, unit overrides, diff_stats, kwargs, expected signal, points)
            ("mechanical small", {"traits": ["mechanical"]}, None, {}, "mechanical_small", -2),
            ("fixture generation", {"traits": ["fixture_generation"]}, None, {},
             "fixture_generation", -2),
            ("more than 3 files",
             {"scope": {"files": ["src/pkg/a.py", "src/pkg/b.py", "src/pkg/c.py",
                                  "src/pkg/d.py"]}}, None, {}, "many_files", 1),
            ("3 modules", {"scope": {"files": ["src/a/x.py", "src/b/y.py", "docs/z.md"]}}, None,
             {}, "many_modules", 2),
            ("large diff", {}, {"lines_changed": 301}, {}, "large_diff", 1),
            ("database trait", {"traits": ["database"]}, None, {}, "database", 3),
            ("database path", {"scope": {"files": ["migrations/0001_init.sql"]}}, None, {},
             "database", 3),
            ("security trait", {"traits": ["security"]}, None, {}, "security", 4),
            ("security path", {"scope": {"files": ["src/auth/login.py"]}}, None, {},
             "security", 4),
            ("secret capability",
             {"capabilities": {"network": True, "secrets": ["GROQ_API_KEY"]}}, None, {},
             "security", 4),
            ("concurrency trait", {"traits": ["concurrency"]}, None, {}, "concurrency", 3),
            ("concurrency path", {"scope": {"files": ["src/pkg/scheduler.py"]}}, None, {},
             "concurrency", 3),
            ("architecture trait", {"traits": ["architecture"]}, None, {}, "architecture", 3),
            ("architecture path", {"scope": {"files": ["src/pkg/contracts/api.py"]}}, None, {},
             "architecture", 3),
            ("no acceptance test", {"acceptance": {"commands": []}}, None, {},
             "no_acceptance_test", 2),
            ("previous failure kwarg", {}, None, {"previous_attempt_failed": True},
             "previous_attempt_failed", 2),
            ("previous failure trait", {"traits": ["previous_attempt_failed"]}, None, {},
             "previous_attempt_failed", 2),
        ]
        for label, overrides, diff, kwargs, name, points in cases:
            with self.subTest(label):
                result = C.classify(unit(**overrides), POLICY, diff, **kwargs)
                self.assertTrue(result.ok, result.errors)
                self.assertEqual(signal_names(result).get(name), points, result.signals)
                self.assertEqual(result.score, sum(s["points"] for s in result.signals))

    def test_boundaries_not_triggered(self):
        cases = [
            ("exactly 300 lines", {}, {"lines_changed": 300}, "large_diff"),
            ("3 files only", {"scope": {"files": ["src/pkg/a.py", "src/pkg/b.py",
                                                  "src/pkg/c.py"]}}, None, "many_files"),
            ("2 modules only", {}, None, "many_modules"),
            ("mechanical but 4 files", {"traits": ["mechanical"], "scope": {"files": [
                "src/pkg/a.py", "src/pkg/b.py", "src/pkg/c.py", "src/pkg/d.py"]}}, None,
             "mechanical_small"),
            ("clock is not a lock", {"scope": {"files": ["src/pkg/clock.py"]}}, None,
             "concurrency"),
            ("author is not auth", {"scope": {"files": ["src/pkg/author.py"]}}, None, "security"),
        ]
        for label, overrides, diff, name in cases:
            with self.subTest(label):
                result = C.classify(unit(**overrides), POLICY, diff)
                self.assertNotIn(name, signal_names(result))

    def test_diff_stats_files_override_scope_count(self):
        result = C.classify(unit(), POLICY, {"files_changed": 5, "lines_changed": 10})
        self.assertIn("many_files", signal_names(result))

    def test_diff_stats_paths_feed_path_signals(self):
        result = C.classify(unit(scope={"files": ["src/**"]}), POLICY,
                            {"files": ["src/db/migrations/0002.py"], "lines_changed": 20})
        self.assertIn("database", signal_names(result))


class PathTokenTests(unittest.TestCase):
    """Security and database path detection across naming styles.

    Decision: a keyword matches as a whole token (after splitting on case
    boundaries, digits and ``/ _ . -``), as a compound prefix (``authmiddleware``),
    or as a compound suffix (``accesstoken``). ``author`` and ``tokenizer`` are
    deliberately not matched: they are different words, and the high risk
    path globs remain the explicit override for anything path heuristics miss.
    """

    SECURITY = [
        "src/AuthService.ts", "src/middleware/authMiddleware.ts", "src/rbac.py", "src/acl.go",
        "infra/iam/roles.tf", "app/models/user_token.py", "app/sessions.py", "lib/OAuth2Client.java",
        "lib/oauth2_client.py", "src/JwtVerifier.kt", "src/authz/policy.py", "src/authn.py",
        "src/Permissions.cs", "src/credentialStore.ts", "src/SecretsManager.py", "pkg/crypto/aes.go",
        "src/PasswordReset.tsx", "src/accessToken.ts", "src/refresh_tokens.py", "src/basicauth.py",
        "src/Authorization.java", "src/Authentication/handler.cs", "src/security/x.py",
    ]
    DATABASE = ["db/Migrations/0001.sql", "db/migrations/0001_init.py", "src/Schema.graphql",
                "src/UserMigration.ts", "db/schemas/users.json"]
    NEITHER = ["docs/author.md", "src/tokenizer_utils.py", "src/Authority.md", "src/pkg/clock.py",
               "src/sessional_notes.txt", "README.md", "src/Tokenizer.ts"]

    def names(self, path):
        return signal_names(C.classify(unit(scope={"files": [path]}), POLICY))

    def test_security_paths(self):
        for path in self.SECURITY:
            with self.subTest(path):
                self.assertIn("security", self.names(path))

    def test_database_paths(self):
        for path in self.DATABASE:
            with self.subTest(path):
                self.assertIn("database", self.names(path))

    def test_deliberate_non_matches(self):
        for path in self.NEITHER:
            with self.subTest(path):
                names = self.names(path)
                self.assertNotIn("security", names)
                self.assertNotIn("database", names)

    def test_security_path_forces_tier2_review(self):
        for path in ("src/AuthService.ts", "src/middleware/authMiddleware.ts", "db/Migrations/0001.sql"):
            with self.subTest(path):
                self.assertEqual(C.classify(unit(scope={"files": [path]}), POLICY).review_tier, 2)


class ScoreToTierTests(unittest.TestCase):
    def test_table(self):
        for score, tier in ((-4, 0), (-2, 0), (0, 0), (1, 1), (3, 1), (5, 1), (6, 2), (12, 2)):
            with self.subTest(score=score):
                self.assertEqual(C.score_to_tier(score), tier)

    def test_signal_combinations(self):
        cases = [
            ({"traits": ["mechanical"]}, 0),
            ({"traits": ["concurrency"]}, 1),
            ({"traits": ["security", "database"]}, 2),
            ({"traits": ["security"], "acceptance": {"commands": []}}, 2),
        ]
        for overrides, tier in cases:
            with self.subTest(overrides=overrides):
                self.assertEqual(C.classify(unit(**overrides), POLICY).score_tier, tier)


class OverrideTests(unittest.TestCase):
    def test_overrides(self):
        cases = [
            # label, overrides, expected chosen tier, expected effective risk
            ("risk high forces tier 2", {"risk": "high", "traits": ["mechanical"]}, 2, "high"),
            ("risk medium forces >= 1", {"risk": "medium", "traits": ["mechanical"]}, 1,
             "medium"),
            ("risk medium keeps higher score", {"risk": "medium",
                                                "traits": ["security", "database"]}, 2, "medium"),
            ("risk low permits score", {"risk": "low", "traits": ["mechanical"]}, 0, "low"),
            ("high risk path forces high", {"scope": {"files": ["src/payments/charge.py"]}}, 2,
             "high"),
            ("nested high risk glob", {"scope": {"files": ["lib/security/keys.py"]}}, 2, "high"),
            ("scope glob overlapping high risk path", {"scope": {"files": ["src/**"]}}, 2,
             "high"),
            ("initial tier raises floor", {"routing": {"initial_tier": 1, "maximum_tier": 2,
                                                       "reviewer_must_differ_from_author": True}},
             1, "low"),
            ("maximum tier caps score", {"traits": ["security", "database"],
                                         "routing": {"initial_tier": 0, "maximum_tier": 1,
                                                     "reviewer_must_differ_from_author": True}},
             1, "low"),
        ]
        for label, overrides, tier, risk in cases:
            with self.subTest(label):
                result = C.classify(unit(**overrides), POLICY)
                self.assertTrue(result.ok, result.errors)
                self.assertEqual(result.chosen_tier, tier, result.reasons)
                self.assertEqual(result.risk, risk)
                self.assertGreaterEqual(result.chosen_tier, result.minimum_tier)
                self.assertLessEqual(result.chosen_tier, result.maximum_tier)

    def test_minimum_above_maximum_is_invalid(self):
        capped = {"initial_tier": 0, "maximum_tier": 1, "reviewer_must_differ_from_author": True}
        for label, overrides in (
                ("declared high", {"risk": "high", "routing": capped}),
                ("path derived high", {"scope": {"files": ["src/payments/x.py"]},
                                       "routing": capped}),
                ("medium over tier 0", {"risk": "medium", "routing": {
                    "initial_tier": 0, "maximum_tier": 0,
                    "reviewer_must_differ_from_author": True}})):
            with self.subTest(label):
                result = C.classify(unit(**overrides), POLICY)
                self.assertFalse(result.ok)
                self.assertTrue(any("maximum_tier" in e for e in result.errors))

    def test_nothing_downgrades_path_minimum(self):
        result = C.classify(unit(scope={"files": ["src/payments/x.py"]}, risk="low",
                                 traits=["mechanical", "fixture_generation"]), POLICY)
        self.assertEqual(result.score, -4)
        self.assertEqual(result.chosen_tier, 2)
        self.assertEqual(result.minimum_tier, 2)

    def test_invalid_unit_rejected(self):
        result = C.classify(unit(risk="extreme"), POLICY)
        self.assertFalse(result.ok)
        self.assertIsNone(result.chosen_tier)

    def test_invalid_policy_rejected(self):
        bad = copy.deepcopy(POLICY)
        bad["allowed_providers"] = ["windsurf"]
        self.assertFalse(C.classify(unit(), bad).ok)

    def test_policy_optional(self):
        self.assertTrue(C.classify(unit(), None).ok)


class ReviewTierTests(unittest.TestCase):
    def test_review_tiers(self):
        cases = [
            ("security needs tier 2 review", {"traits": ["security"]}, None, 2),
            ("migration needs tier 2 review", {"scope": {"files": ["migrations/1.sql"]}}, None, 2),
            ("architecture needs tier 2 review", {"traits": ["architecture"]}, None, 2),
            ("high risk needs tier 2 review", {"risk": "high"}, None, 2),
            ("small deterministic diff allows tier 0", {}, {"lines_changed": 40}, 0),
            ("mechanical small allows tier 0", {"traits": ["mechanical"]}, None, 0),
            ("unknown size defaults to tier 1", {}, None, 1),
            ("no acceptance test defaults to tier 1", {"acceptance": {"commands": []}},
             {"lines_changed": 10}, 1),
            ("medium risk defaults to tier 1", {"risk": "medium"}, {"lines_changed": 10}, 1),
        ]
        for label, overrides, diff, tier in cases:
            with self.subTest(label):
                self.assertEqual(C.classify(unit(**overrides), POLICY, diff).review_tier, tier)


class DecisionRecordTests(unittest.TestCase):
    def test_routing_decision_validates(self):
        result = C.classify(unit(traits=["security"]), POLICY)
        doc = result.to_routing_decision("2026-09-24T00:00:00Z", runner="codex",
                                         provider="codex", model="codex-adv")
        report = schemas.validate_routing_decision(doc)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(doc["chosen_tier"], result.chosen_tier)


if __name__ == "__main__":
    unittest.main()
