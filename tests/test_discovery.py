"""Slice 2: dynamic model discovery with injected HTTP and environment."""

import json
import os
import tempfile
import unittest

from openclaw_ecc_orchestrator.runners import discovery as d

NOW = 1_790_000_000.0  # fixed epoch for tests
GROQ_KEY = "gsk_" + "TestOnlyKey0123456789abcdefABCDEF"
OR_KEY = "sk-or-v1-" + "0123456789abcdef0123456789abcdef"
GEMINI_KEY = "AIza" + "TestOnlyKey0123456789abcdefABCDE"

GROQ_BODY = {"object": "list", "data": [
    {"id": "open-small-8b-instant", "object": "model", "active": True, "context_window": 131072},
    {"id": "open-small-9b-instant", "object": "model", "active": True, "context_window": 131072},
    {"id": "open-large-70b-versatile", "object": "model", "active": True},
    {"id": "legacy-mix-8x7b", "object": "model", "active": False},
    {"id": "sunset-model-1b", "object": "model", "active": True, "deprecated": True},
]}
OPENROUTER_BODY = {"data": [
    {"id": "vendor-a/coder-large", "context_length": 200000,
     "pricing": {"prompt": "0.000003", "completion": "0.000015"}},
    {"id": "vendor-b/reviewer-mid", "pricing": {"prompt": "0.0000005", "completion": "0.0000015"}},
    {"id": "vendor-c/expired", "pricing": {"prompt": "0", "completion": "0"},
     "expiration_date": "2020-01-01"},
]}
GEMINI_PAGE_1 = {"models": [
    {"name": "models/gemini-flash-next", "supportedGenerationMethods": ["generateContent"],
     "inputTokenLimit": 1048576},
    {"name": "models/text-embedding-x", "supportedGenerationMethods": ["embedContent"]},
], "nextPageToken": "page2"}
GEMINI_PAGE_2 = {"models": [
    {"name": "models/gemini-pro-next", "supportedGenerationMethods": ["generateContent"]},
]}


class FakeHttp:
    def __init__(self, routes=None, raise_with=None, status=200):
        self.routes = routes or {}
        self.calls = []
        self.raise_with = raise_with
        self.status = status

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers), timeout))
        if self.raise_with:
            raise self.raise_with
        for prefix, body in self.routes.items():
            if url.startswith(prefix):
                return d.HttpResponse(self.status, json.dumps(body))
        return d.HttpResponse(404, "{}")


def routes():
    return {
        d.PROVIDER_ENDPOINTS["groq"]["url"]: GROQ_BODY,
        d.PROVIDER_ENDPOINTS["openrouter"]["url"]: OPENROUTER_BODY,
        d.PROVIDER_ENDPOINTS["gemini"]["url"] + "?pageToken=page2": GEMINI_PAGE_2,
        d.PROVIDER_ENDPOINTS["gemini"]["url"]: GEMINI_PAGE_1,
    }


ENV = {"GROQ_API_KEY": GROQ_KEY, "OPENROUTER_API_KEY": OR_KEY, "GEMINI_API_KEY": GEMINI_KEY}


def snapshot(providers=("groq", "openrouter", "gemini"), env=ENV, http=None):
    return d.fetch_catalog(providers, http_get=http or FakeHttp(routes()), env=env,
                           clock=lambda: NOW)


class FetchTests(unittest.TestCase):
    def test_openai_compatible_listing(self):
        snap = snapshot(("groq",))
        entry = snap["providers"]["groq"]
        self.assertEqual(entry["status"], "ok")
        ids = {m["id"]: m for m in entry["models"]}
        self.assertFalse(ids["open-small-8b-instant"]["deprecated"])
        self.assertTrue(ids["legacy-mix-8x7b"]["deprecated"])
        self.assertTrue(ids["sunset-model-1b"]["deprecated"])

    def test_auth_header_uses_env_value_and_not_url(self):
        http = FakeHttp(routes())
        snapshot(("groq", "gemini"), http=http)
        for url, headers, timeout in http.calls:
            self.assertNotIn(GROQ_KEY, url)
            self.assertNotIn(GEMINI_KEY, url)
            self.assertGreater(timeout, 0)
        groq_call = [c for c in http.calls if "groq" in c[0]][0]
        self.assertEqual(groq_call[1]["Authorization"], "Bearer " + GROQ_KEY)
        gem_call = [c for c in http.calls if "generativelanguage" in c[0]][0]
        self.assertEqual(gem_call[1]["x-goog-api-key"], GEMINI_KEY)

    def test_openrouter_pricing_normalized(self):
        snap = snapshot(("openrouter",))
        ids = {m["id"]: m for m in snap["providers"]["openrouter"]["models"]}
        self.assertAlmostEqual(ids["vendor-a/coder-large"]["input_usd_per_mtok"], 3.0)
        self.assertAlmostEqual(ids["vendor-a/coder-large"]["output_usd_per_mtok"], 15.0)
        self.assertTrue(ids["vendor-c/expired"]["deprecated"])

    def test_gemini_pagination_and_generation_filter(self):
        snap = snapshot(("gemini",))
        ids = [m["id"] for m in snap["providers"]["gemini"]["models"]]
        self.assertIn("gemini-flash-next", ids)
        self.assertIn("gemini-pro-next", ids)
        self.assertNotIn("text-embedding-x", ids)

    def test_missing_key_is_not_configured(self):
        http = FakeHttp(routes())
        snap = snapshot(("groq",), env={}, http=http)
        self.assertEqual(snap["providers"]["groq"]["status"], "not_configured")
        self.assertEqual(http.calls, [])
        self.assertIn("GROQ_API_KEY", snap["providers"]["groq"]["error"])

    def test_http_error_status_recorded_without_body(self):
        http = FakeHttp(routes(), status=401)
        snap = snapshot(("groq",), http=http)
        entry = snap["providers"]["groq"]
        self.assertEqual(entry["status"], "error")
        self.assertEqual(entry["models"], [])
        self.assertIn("401", entry["error"])

    def test_exception_message_never_leaks_key(self):
        http = FakeHttp(raise_with=RuntimeError("connect failed for key " + GROQ_KEY[:20] +
                                                GROQ_KEY[20:]))
        snap = snapshot(("groq",), http=http)
        blob = json.dumps(snap)
        self.assertNotIn(GROQ_KEY, blob)
        self.assertEqual(snap["providers"]["groq"]["status"], "error")

    def test_invalid_json(self):
        def http(url, headers, timeout):
            return d.HttpResponse(200, "<html>")
        snap = d.fetch_catalog(("groq",), http_get=http, env=ENV, clock=lambda: NOW)
        self.assertEqual(snap["providers"]["groq"]["status"], "error")

    def test_unknown_or_retired_provider_rejected(self):
        with self.assertRaises(ValueError):
            d.fetch_catalog(("windsurf",), http_get=FakeHttp(), env=ENV, clock=lambda: NOW)
        with self.assertRaises(ValueError):
            d.fetch_catalog(("openai-api",), http_get=FakeHttp(), env=ENV, clock=lambda: NOW)

    def test_snapshot_shape(self):
        snap = snapshot()
        self.assertEqual(snap["schema_version"], "1.0")
        self.assertIn("fetched_at", snap)
        self.assertEqual(snap["fetched_at_epoch"], NOW)


class PersistenceAndStalenessTests(unittest.TestCase):
    def test_round_trip(self):
        snap = snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "catalog.json")
            d.save_snapshot(snap, path)
            self.assertEqual(d.load_snapshot(path), snap)
            self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(tmp)))

    def test_saved_snapshot_contains_no_keys(self):
        snap = snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "catalog.json")
            d.save_snapshot(snap, path)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        for key in ENV.values():
            self.assertNotIn(key, text)

    def test_staleness(self):
        snap = snapshot()
        self.assertFalse(d.is_stale(snap, now=NOW + 60, max_age_seconds=3600))
        self.assertTrue(d.is_stale(snap, now=NOW + 7200, max_age_seconds=3600))
        self.assertTrue(d.is_stale({"schema_version": "1.0"}, now=NOW, max_age_seconds=3600))

    def test_load_rejects_wrong_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "catalog.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"schema_version": "9.9", "providers": {}}, fh)
            with self.assertRaises(ValueError):
                d.load_snapshot(path)


PREFS = {
    "groq": {"economical": ["removed-model-x", "open-small-*-instant"],
             "standard": ["legacy-mix-8x7b", "sunset-model-1b"]},
    "openrouter": {"standard": ["vendor-b/*"], "advanced": ["vendor-a/coder-large"]},
    "gemini": {"economical": ["gemini-flash-*"], "advanced": ["gemini-pro-*"]},
}


class CapabilityMappingTests(unittest.TestCase):
    def test_removed_model_in_preferences_never_selected(self):
        snap = snapshot()
        chosen = d.select_model(snap, "groq", "economical", PREFS)
        self.assertNotEqual(chosen, "removed-model-x")
        self.assertEqual(chosen, "open-small-9b-instant")  # newest match first

    def test_deprecated_models_never_selected(self):
        snap = snapshot()
        self.assertIsNone(d.select_model(snap, "groq", "standard", PREFS))

    def test_retired_ids_denylist(self):
        snap = snapshot()
        chosen = d.select_model(snap, "groq", "economical", PREFS,
                                retired_ids=["open-small-9b-instant"])
        self.assertEqual(chosen, "open-small-8b-instant")

    def test_no_fallback_to_arbitrary_model(self):
        snap = snapshot()
        self.assertIsNone(d.select_model(snap, "groq", "economical",
                                         {"groq": {"economical": ["removed-model-x"]}}))
        self.assertIsNone(d.select_model(snap, "groq", "advanced", PREFS))

    def test_resolve_capabilities_map(self):
        snap = snapshot()
        mapping = d.resolve_capabilities(snap, PREFS)
        self.assertEqual(mapping["gemini"]["economical"], ["gemini-flash-next"])
        self.assertEqual(mapping["openrouter"]["advanced"], ["vendor-a/coder-large"])
        self.assertEqual(mapping["groq"]["standard"], [])

    def test_stale_snapshot_refused(self):
        snap = snapshot()
        with self.assertRaises(d.StaleCatalogError):
            d.select_model(snap, "groq", "economical", PREFS, now=NOW + 10_000,
                           max_age_seconds=3600)
        self.assertEqual(d.select_model(snap, "groq", "economical", PREFS, now=NOW + 10,
                                        max_age_seconds=3600), "open-small-9b-instant")

    def test_provider_error_yields_no_models(self):
        snap = snapshot(("groq",), http=FakeHttp(routes(), status=500))
        self.assertIsNone(d.select_model(snap, "groq", "economical", PREFS))

    def test_model_pricing_lookup(self):
        snap = snapshot()
        price = d.model_pricing(snap, "openrouter", "vendor-b/reviewer-mid")
        self.assertAlmostEqual(price["input_usd_per_mtok"], 0.5)
        self.assertIsNone(d.model_pricing(snap, "openrouter", "missing/model"))


if __name__ == "__main__":
    unittest.main()
