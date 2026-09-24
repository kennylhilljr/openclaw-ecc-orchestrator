"""Runner registry: profiles, retired runners and certified filtering."""

import unittest

from openclaw_ecc_orchestrator import schemas
from openclaw_ecc_orchestrator.runners import registry

NOW = 1_790_000_000.0


def record(runner, status="certified", expires_at="2026-12-31T00:00:00Z", **extra):
    doc = {"schema_version": "1.0", "runner": runner, "provider": runner, "status": status,
           "certified_at": "2026-09-01T00:00:00Z", "expires_at": expires_at, "version": "1",
           "models": [], "checks": [{"name": n, "status": "pass", "reason": ""}
                                    for n in schemas.CERTIFICATION_CHECKS]}
    doc.update(extra)
    return doc


class RegistryTests(unittest.TestCase):
    def test_profiles_exist_for_active_runners(self):
        for name in ("claude", "codex", "gemini", "groq", "openrouter", "kimi"):
            profile = registry.get_profile(name)
            self.assertEqual(profile.name, name)

    def test_retired_never_in_profiles(self):
        for name in ("windsurf", "pi", "openai-api", "openai"):
            self.assertNotIn(name, registry.PROFILES)
            with self.assertRaises(registry.RetiredRunnerError):
                registry.get_profile(name)

    def test_build_registry_rejects_retired(self):
        with self.assertRaises(registry.RetiredRunnerError):
            registry.build_registry(["claude", " PI "])
        self.assertEqual([p.name for p in registry.build_registry(["codex", "gemini"])],
                         ["codex", "gemini"])

    def test_certified_filter(self):
        records = [
            record("claude"),
            record("codex", status="failed"),
            record("kimi", status="not_configured"),
            record("gemini", expires_at="2020-01-01T00:00:00Z"),
            record("windsurf"),  # forged: retired runner claiming certified
            record("groq", checks=[]),  # malformed certified record
        ]
        certified, excluded = registry.certified_runners(records, now=NOW)
        self.assertEqual(sorted(certified), ["claude"])
        reasons = {e["runner"]: e["reason"] for e in excluded}
        self.assertIn("retired", reasons["windsurf"])
        self.assertIn("expired", reasons["gemini"])
        self.assertIn("not_configured", reasons["kimi"])
        self.assertIn("invalid", reasons["groq"])

    def test_policy_roles(self):
        self.assertNotIn("author", registry.get_profile("openrouter").roles)
        self.assertIn("fallback", registry.get_profile("openrouter").roles)
        self.assertFalse(registry.get_profile("groq").high_risk_ok)
        self.assertEqual(registry.get_profile("groq").classes, ("economical",))
        self.assertNotIn("economical", registry.get_profile("claude").classes)

    def test_model_family(self):
        self.assertEqual(registry.model_family("openrouter", "anthropic/some-model"), "anthropic")
        self.assertEqual(registry.model_family("groq", "gpt-oss-large"), "openai")
        self.assertEqual(registry.model_family("claude", None), "anthropic")
        self.assertEqual(registry.model_family("codex", "anything"), "openai")
        self.assertTrue(registry.model_family("groq", "zzz-unknown").startswith("unknown"))


if __name__ == "__main__":
    unittest.main()
