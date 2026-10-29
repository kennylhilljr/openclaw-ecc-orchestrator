"""Regression tests from the 2026-10-18 live certification of the API workers.

Groq's edge rejected Python's default urllib User-Agent (HTTP 403, Cloudflare
error 1010) before any authentication happened, and Gemini's opaque
``thoughtSignature`` blob failed log inspection as a false positive.
"""

import io
import json
import unittest
import urllib.error
from unittest import mock

from openclaw_ecc_orchestrator.handoffs import redaction
from openclaw_ecc_orchestrator.runners import base, discovery, probes
from openclaw_ecc_orchestrator.runners.base import ProbeContext
from openclaw_ecc_orchestrator.runners.discovery import HttpResponse

NOW = 1_790_000_000.0
# High entropy, mixed case and digits: looks like an opaque signature or cursor.
BLOB = "Cq0BAdHtim9xZ3k7Lw2PqR8sT1uV4yX6zA9bC2dE5fG8hJ0kL3mN7pQ1rS4tU6vW9xY2zB5cD8eF1gH4"
GEMINI_KEY = "AIza" + "Sy0123456789abcdefghijABCDEFGHIJ"


class _FakeResp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class UserAgentTests(unittest.TestCase):
    def _capture(self, call):
        seen = {}

        def fake_urlopen(request, timeout=None, context=None):
            seen["ua"] = request.get_header("User-agent")
            return _FakeResp(b"{}")
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            call()
        return seen["ua"]

    def test_get_sends_named_user_agent(self):
        ua = self._capture(lambda: discovery.default_http_get("https://example.test/m", {}, 5))
        self.assertTrue(ua and ua.startswith("openclaw-ecc-orchestrator/"), ua)

    def test_post_sends_named_user_agent(self):
        ua = self._capture(lambda: base.default_http_post("https://example.test/c", {}, {}, 5))
        self.assertTrue(ua and ua.startswith("openclaw-ecc-orchestrator/"), ua)

    def test_caller_user_agent_wins(self):
        ua = self._capture(lambda: discovery.default_http_get(
            "https://example.test/m", {"User-Agent": "custom/1"}, 5))
        self.assertEqual(ua, "custom/1")

    def test_error_body_is_kept_for_diagnosis(self):
        def boom(request, timeout=None, context=None):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {},
                                         io.BytesIO(b"error code: 1010\n"))
        with mock.patch("urllib.request.urlopen", boom):
            get = discovery.default_http_get("https://example.test/m", {}, 5)
            post = base.default_http_post("https://example.test/c", {}, {}, 5)
        for resp in (get, post):
            self.assertEqual(resp.status, 403)
            self.assertIn("1010", resp.body)


class LogInspectionTests(unittest.TestCase):
    def test_opaque_known_fields_are_not_findings(self):
        text = json.dumps({"candidates": [{"content": {"parts": [
            {"text": "OK", "thoughtSignature": BLOB}]}}],
            "nextPageToken": BLOB, "responseId": BLOB}, indent=2)
        self.assertEqual(redaction.inspection_findings([text]), [])

    def test_same_blob_elsewhere_is_still_a_finding(self):
        self.assertTrue(redaction.inspection_findings(["signature " + BLOB]))
        self.assertTrue(redaction.inspection_findings([json.dumps({"note": BLOB})]))

    def test_credential_prefix_inside_allowed_field_is_a_finding(self):
        text = json.dumps({"thoughtSignature": GEMINI_KEY})
        self.assertTrue(redaction.inspection_findings([text]))

    def test_registered_secret_is_always_a_finding(self):
        literal = "plainvalue-without-entropy"
        text = json.dumps({"thoughtSignature": literal})
        self.assertTrue(redaction.inspection_findings([text], secrets=[literal]))

    def test_findings_never_echo_the_value(self):
        findings = redaction.inspection_findings(["x " + GEMINI_KEY])
        self.assertTrue(findings)
        self.assertNotIn(GEMINI_KEY, json.dumps(findings))


class UsageNumberRedactionTests(unittest.TestCase):
    def test_numeric_token_counts_and_limits_stay_visible(self):
        for line in ('"promptTokenCount": 12,', '"inputTokenLimit": 1048576,',
                     '"totalTokenCount": 13', '"max_tokens": 64'):
            self.assertEqual(redaction.redact_text(line), line)

    def test_structural_values_are_not_masked(self):
        line = '"promptTokensDetails": ['
        self.assertEqual(redaction.redact_text(line), line)

    def test_real_secret_names_still_masked(self):
        for line in ('"api_token": "123456"', '"password": 123456', 'ACCESS_TOKEN=abc123def'):
            self.assertIn(redaction.REDACTED, redaction.redact_text(line), line)


class GeminiProbeTests(unittest.TestCase):
    def test_thought_signature_does_not_fail_certification(self):
        def post(url, headers, body, timeout):
            return HttpResponse(200, json.dumps({
                "candidates": [{"content": {"parts": [{"text": "OK", "thoughtSignature": BLOB}]}}],
                "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 1},
                "modelVersion": "gemini-flash-next", "responseId": BLOB}))
        record = probes.probe_runner("gemini", ProbeContext(
            http_get=lambda u, h, t: HttpResponse(200, '{"models": [], "nextPageToken": "%s"}' % BLOB),
            http_post=post, env={"GEMINI_API_KEY": GEMINI_KEY}, clock=lambda: NOW,
            step_timeout=2.0, model="gemini-flash-next"))
        checks = {c["name"]: c["status"] for c in record["checks"]}
        self.assertEqual(checks["log_inspection"], "pass", record)
        self.assertEqual(record["status"], "certified", record)
        self.assertNotIn(GEMINI_KEY, json.dumps(record))

    def test_key_echoed_in_output_still_fails(self):
        def post(url, headers, body, timeout):
            return HttpResponse(200, json.dumps({
                "candidates": [{"content": {"parts": [{"text": "OK " + GEMINI_KEY}]}}],
                "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 1},
                "modelVersion": "gemini-flash-next"}))
        record = probes.probe_runner("gemini", ProbeContext(
            http_get=lambda u, h, t: HttpResponse(200, '{"models": []}'),
            http_post=post, env={"GEMINI_API_KEY": GEMINI_KEY}, clock=lambda: NOW,
            step_timeout=2.0, model="gemini-flash-next"))
        checks = {c["name"]: c["status"] for c in record["checks"]}
        self.assertEqual(checks["live_inference"], "pass", record)
        self.assertEqual(checks["log_inspection"], "fail")
        self.assertNotIn(GEMINI_KEY, json.dumps(record))


class GroqDefaultPreferenceTests(unittest.TestCase):
    # Groq's live catalog as listed on 2026-10-18 (ids only).
    CATALOG = ["meta-llama/llama-prompt-guard-2-86m", "openai/gpt-oss-120b", "qwen/qwen3.8-27b",
               "canopylabs/orpheus-v1-english", "meta-llama/llama-prompt-guard-2-22m",
               "openai/gpt-oss-20b", "whisper-large-v3-turbo", "openai/gpt-oss-safeguard-20b",
               "allam-2-7b", "canopylabs/orpheus-arabic-saudi", "whisper-large-v3"]

    def test_default_patterns_pick_a_small_general_model(self):
        from openclaw_ecc_orchestrator.runners.certify import DEFAULT_API_PREFERENCES
        ranked = discovery.rank_models([{"id": i} for i in self.CATALOG],
                                       DEFAULT_API_PREFERENCES["groq"])
        self.assertEqual(ranked[0], "openai/gpt-oss-20b")
        for excluded in ("guard", "whisper", "orpheus", "safeguard", "120b"):
            self.assertFalse(any(excluded in m for m in ranked), (excluded, ranked))


class ReasoningModelBudgetTests(unittest.TestCase):
    """gpt-oss-20b on Groq spent an 8 token budget on reasoning and returned no text."""

    def _groq(self, post):
        return probes.probe_runner("groq", ProbeContext(
            http_get=lambda u, h, t: HttpResponse(200, '{"data": [{"id": "openai/gpt-oss-20b"}]}'),
            http_post=post, env={"GROQ_API_KEY": "gsk_" + "A1b2C3d4E5f6G7h8J9k0"},
            clock=lambda: NOW, step_timeout=2.0, model="openai/gpt-oss-20b"))

    def test_probe_budget_leaves_room_for_reasoning(self):
        seen = {}

        def post(url, headers, body, timeout):
            seen["body"] = body
            return HttpResponse(200, json.dumps({
                "model": "openai/gpt-oss-20b",
                "choices": [{"message": {"content": "OK", "reasoning": "short"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 81, "completion_tokens": 40}}))
        record = self._groq(post)
        self.assertGreaterEqual(seen["body"]["max_tokens"], 256)
        self.assertEqual(record["status"], "certified", record)

    def test_exhausted_budget_is_reported(self):
        def post(url, headers, body, timeout):
            return HttpResponse(200, json.dumps({
                "model": "openai/gpt-oss-20b",
                "choices": [{"message": {"content": "", "reasoning": "The user says"},
                             "finish_reason": "length"}],
                "usage": {"prompt_tokens": 81, "completion_tokens": 8}}))
        record = self._groq(post)
        live = [c for c in record["checks"] if c["name"] == "live_inference"][0]
        self.assertEqual(live["status"], "fail")
        self.assertIn("finish_reason length", live["reason"])

    def test_gemini_budget_also_raised(self):
        from openclaw_ecc_orchestrator.runners.api_adapters import GeminiAdapter
        _, body = GeminiAdapter().inference_request(ProbeContext(model="gemini-x"))
        self.assertGreaterEqual(body["generationConfig"]["maxOutputTokens"], 256)


class MacPathRedactionTests(unittest.TestCase):
    """macOS temp dirs (/private/var/folders/<x>/<mixed case id>/T/tmpXXXX) were
    masked as high entropy tokens, breaking 8 tests and hiding paths in output."""

    MAC_TMP = "/private/var/folders/6j/8_5x46rs3db2852wd7wb16v40000gn/T/tmpl9bzuvm1"

    def test_macos_temp_paths_are_not_masked(self):
        for path in (self.MAC_TMP, "/var/folders/6j/8_5x46rs3db2852wd7wb16v40000gn/T/tmpu88w99xg/xdg-state",
                     "/Users/someone/Documents/GitHub/openclaw-ecc-orchestrator/src"):
            self.assertEqual(redaction.redact_text(path), path)
            self.assertEqual(redaction.redact_text("cwd=%s\n" % path), "cwd=%s\n" % path)
            self.assertFalse(redaction.inspection_findings([path]), path)

    def test_paths_through_hidden_directories_are_not_masked(self):
        """Live finding (rmcp-m0f): a worktree path under ~/.local failed a handoff as secret_detected."""
        path = ("/Users/someone/.local/share/openclaw-ecc-orchestrator/worktrees/"
                "my-research-mcp-66e6cd383a/rmcp-m0f/M0-01")
        self.assertFalse(redaction.contains_secret(path))
        self.assertEqual(redaction.redact_text(path), path)
        self.assertFalse(redaction.inspection_findings([path]))
        hidden = "/Users/someone/.cache/" + BLOB + "/x"
        self.assertNotIn(BLOB, redaction.redact_text(hidden))

    def test_long_random_segment_inside_a_path_is_still_masked(self):
        path = "/tmp/" + BLOB + "/x"
        self.assertIn(redaction.REDACTED, redaction.redact_text(path))
        self.assertNotIn(BLOB, redaction.redact_text(path))

    def test_key_inside_a_path_is_still_masked(self):
        key = "gsk_" + "A1b2C3d4E5f6G7h8J9k0L1m2"
        out = redaction.redact_text("/private/var/folders/" + key + "/T")
        self.assertNotIn(key, out)

    def test_slash_bearing_secret_outside_a_path_is_still_masked(self):
        secret = "Zq3/Lw9KpT2vXr8/Ys4mNb7QcH1dFg6jRk0pAe5u"  # gitleaks:allow
        self.assertNotIn(secret, redaction.redact_text("value " + secret))
        self.assertNotIn(secret, redaction.redact_text("value " + secret + " here"))

    def test_repository_relative_file_paths_are_not_masked(self):
        """SDD unit descriptions cite versioned documents such as
        docs/prd/components/PRD-platform-v3.md; they failed work unit validation
        as secret_detected."""
        for path in ("docs/prd/components/PRD-platform-v3.md", "docs/prd/components/PRD-security-v1.md",
                     "src/research_mcp/platform/migrations/state/0001_init.sql"):
            self.assertFalse(redaction.contains_secret(path), path)
            self.assertFalse(redaction.contains_secret("see %s section 2.4" % path), path)
            self.assertEqual(redaction.redact_text(path), path)

    def test_long_random_segment_in_a_relative_path_is_still_masked(self):
        path = "docs/" + BLOB + ".md"
        self.assertIn(redaction.REDACTED, redaction.redact_text(path))
        self.assertNotIn(BLOB, redaction.redact_text(path))


class ProviderAccountIdTests(unittest.TestCase):
    """Moonshot's quota error echoed the account id and the API key id."""

    def test_moonshot_account_and_key_ids_are_masked(self):
        org = "org-" + "0123456789abcdef0123456789abcdef"
        key_id = "ak-" + "abcdefghij0123456789"
        msg = '{"error":{"message":"Your account %s <%s> is suspended"}}' % (org, key_id)
        out = redaction.redact_text(msg)
        self.assertNotIn(org, out)
        self.assertNotIn(key_id, out)
        self.assertIn("is suspended", out)

    def test_short_lookalikes_are_left_alone(self):
        for text in ("ak-47", "org-chart", "the org-wide policy"):
            self.assertEqual(redaction.redact_text(text), text)


class KimiProbeTests(unittest.TestCase):
    """kimi-k3 rejects any temperature but 1 (HTTP 400), and the economical
    patterns picked that flagship model over the k2 coding model."""

    CATALOG = ["kimi-k2.7-code-highspeed", "kimi-k3", "kimi-k2.7-code", "kimi-k2.6"]

    def test_chat_probe_sends_no_temperature(self):
        from openclaw_ecc_orchestrator.runners.api_adapters import GroqAdapter, KimiAdapter
        for adapter in (GroqAdapter(), KimiAdapter()):
            _, body = adapter.inference_request(ProbeContext(model="m"))
            self.assertNotIn("temperature", body)

    def test_default_patterns_prefer_the_k2_coding_model(self):
        from openclaw_ecc_orchestrator.runners.certify import DEFAULT_API_PREFERENCES
        ranked = discovery.rank_models([{"id": i} for i in self.CATALOG],
                                       DEFAULT_API_PREFERENCES["kimi"])
        self.assertEqual(ranked[0], "kimi-k2.7-code")


class ClaudeMinimumVersionTests(unittest.TestCase):
    """A stale Claude Code (2.1.112) first on PATH failed with an unknown option
    error on 2026-10-18; it should fail the installed check with a clear reason."""

    def _installed(self, version_line):
        from openclaw_ecc_orchestrator.runners.base import CommandResult
        from openclaw_ecc_orchestrator.runners.cli_adapters import ClaudeAdapter
        ctx = ProbeContext(run_command=lambda argv, timeout, cwd=None: CommandResult(0, version_line + "\n", ""),
                           step_timeout=2.0)
        return ClaudeAdapter().installed(ctx)

    def test_old_version_fails_with_clear_reason(self):
        out = self._installed("2.1.112 (Claude Code)")
        self.assertEqual(out.status, "fail")
        self.assertIn("older than the minimum 2.1.281", out.reason)
        self.assertIn("PATH", out.reason)

    def test_minimum_and_newer_pass(self):
        for line in ("2.1.281 (Claude Code)", "2.1.300 (Claude Code)", "2.2.0 (Claude Code)"):
            self.assertEqual(self._installed(line).status, "pass", line)

    def test_unparseable_version_is_not_blocked(self):
        self.assertEqual(self._installed("Claude Code dev build").status, "pass")


if __name__ == "__main__":
    unittest.main()
