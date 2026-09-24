"""Dynamic model discovery from live provider catalogs.

Nothing here assumes a model id stays available. Configuration names
capability classes (economical / standard / advanced) and ordered glob
preference patterns per provider; the live catalog decides which ids exist.

* API keys are read from environment variable NAMES and only ever placed in
  request headers, never in URLs, snapshots, errors or logs.
* Models flagged inactive / deprecated / retired / expired, models listed in
  ``retired_ids`` and models absent from the live catalog are never selected.
* All HTTP goes through an injected ``http_get(url, headers, timeout)``.
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import json
import os
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterable, Mapping, NamedTuple

from ..handoffs.redaction import redact_text
from ..schemas import CAPABILITY_CLASSES, KNOWN_PROVIDERS, SCHEMA_VERSION, is_retired


class HttpResponse(NamedTuple):
    status: int
    body: str


HttpGetter = Callable[[str, Mapping[str, str], float], HttpResponse]


class StaleCatalogError(RuntimeError):
    """The catalog snapshot is older than the allowed maximum age."""


PROVIDER_ENDPOINTS: dict[str, dict[str, str]] = {
    "groq": {"url": "https://api.groq.com/openai/v1/models", "env": "GROQ_API_KEY",
             "style": "openai"},
    "openrouter": {"url": "https://openrouter.ai/api/v1/models", "env": "OPENROUTER_API_KEY",
                   "style": "openai"},
    "kimi": {"url": "https://api.moonshot.ai/v1/models", "env": "MOONSHOT_API_KEY",
             "style": "openai"},
    "gemini": {"url": "https://generativelanguage.googleapis.com/v1beta/models",
               "env": "GEMINI_API_KEY", "style": "gemini"},
}

_BAD_STATUSES = {"deprecated", "retired", "disabled", "decommissioned", "inactive", "sunset"}
_MAX_PAGES = 20


# System trust stores used when Python's own default store is empty (the
# python.org macOS build ships without one until "Install Certificates" runs).
SYSTEM_CA_FILES = ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt",
                   "/etc/pki/tls/certs/ca-bundle.crt")


def _default_store_usable(paths) -> bool:
    if paths.cafile and os.path.isfile(paths.cafile):
        return True
    try:
        return bool(paths.capath and os.path.isdir(paths.capath) and os.listdir(paths.capath))
    except OSError:
        return False


def tls_context(env: Mapping[str, str] | None = None,
                candidates: Iterable[str] = SYSTEM_CA_FILES) -> ssl.SSLContext:
    """A verifying TLS context. Verification is never disabled: when neither
    ``SSL_CERT_FILE``/``SSL_CERT_DIR`` nor Python's default store provides
    trust anchors, the first existing system CA file is loaded instead."""
    env = os.environ if env is None else env
    if env.get("SSL_CERT_FILE") or env.get("SSL_CERT_DIR"):
        return ssl.create_default_context()
    if not _default_store_usable(ssl.get_default_verify_paths()):
        for path in candidates:
            if os.path.isfile(path):
                return ssl.create_default_context(cafile=path)
    return ssl.create_default_context()


def default_http_get(url: str, headers: Mapping[str, str], timeout: float) -> HttpResponse:
    """Real HTTP getter (not used by tests)."""
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout,  # noqa: S310
                                    context=tls_context()) as resp:
            return HttpResponse(resp.status, resp.read(8 * 1024 * 1024).decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, "")


def _iso(epoch: float) -> str:
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_date(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.timestamp()


def _is_deprecated(raw: Mapping[str, Any], now: float) -> bool:
    if raw.get("deprecated") is True or raw.get("retired") is True:
        return True
    if raw.get("active") is False:
        return True
    status = raw.get("status") or raw.get("state")
    if isinstance(status, str) and status.strip().lower() in _BAD_STATUSES:
        return True
    for key in ("expiration_date", "deprecation_date", "shutdown_date", "retirement_date"):
        when = _parse_date(raw.get(key))
        if when is not None and when <= now:
            return True
    return False


def _per_mtok(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return round(number * 1_000_000, 6)


def _normalize_openai(body: Mapping[str, Any], now: float) -> list[dict]:
    models = []
    for raw in body.get("data") or []:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            continue
        pricing = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else {}
        models.append({
            "id": raw["id"],
            "deprecated": _is_deprecated(raw, now),
            "input_usd_per_mtok": _per_mtok(pricing.get("prompt")) if pricing else None,
            "output_usd_per_mtok": _per_mtok(pricing.get("completion")) if pricing else None,
            "context_length": raw.get("context_length") or raw.get("context_window"),
        })
    return models


def _normalize_gemini(pages: list[Mapping[str, Any]], now: float) -> list[dict]:
    models = []
    for body in pages:
        for raw in body.get("models") or []:
            if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
                continue
            methods = raw.get("supportedGenerationMethods")
            if isinstance(methods, list) and "generateContent" not in methods:
                continue
            name = raw["name"]
            models.append({
                "id": name[len("models/"):] if name.startswith("models/") else name,
                "deprecated": _is_deprecated(raw, now),
                "input_usd_per_mtok": None,
                "output_usd_per_mtok": None,
                "context_length": raw.get("inputTokenLimit"),
            })
    return models


def _check_provider(provider: str) -> None:
    if is_retired(provider):
        raise ValueError("provider is retired and cannot be queried: %s" % provider)
    if provider not in PROVIDER_ENDPOINTS:
        raise ValueError("no catalog client for provider: %s" % redact_text(provider))


def fetch_provider_models(provider: str, http_get: HttpGetter, env: Mapping[str, str],
                          now: float, timeout: float = 15.0) -> dict:
    """Fetch and normalize one provider's live model listing."""
    _check_provider(provider)
    endpoint = PROVIDER_ENDPOINTS[provider]
    key = env.get(endpoint["env"]) or ""
    if not key:
        return {"status": "not_configured", "models": [],
                "error": "environment variable %s is not set" % endpoint["env"]}
    if endpoint["style"] == "gemini":
        headers = {"x-goog-api-key": key, "Accept": "application/json"}
    else:
        headers = {"Authorization": "Bearer " + key, "Accept": "application/json"}
    pages: list[dict] = []
    url = endpoint["url"]
    try:
        for _ in range(_MAX_PAGES):
            response = http_get(url, headers, timeout)
            if response.status != 200:
                return {"status": "error", "models": [],
                        "error": "HTTP %d from %s catalog" % (int(response.status), provider)}
            try:
                body = json.loads(response.body)
            except ValueError:
                return {"status": "error", "models": [], "error": "invalid JSON from catalog"}
            if not isinstance(body, dict):
                return {"status": "error", "models": [], "error": "unexpected catalog shape"}
            pages.append(body)
            token = body.get("nextPageToken") if endpoint["style"] == "gemini" else None
            if not token or not isinstance(token, str):
                break
            url = endpoint["url"] + "?pageToken=" + urllib.parse.quote(token, safe="")
    except Exception as exc:  # noqa: BLE001 - any transport failure is recorded
        message = redact_text("%s: %s" % (type(exc).__name__, exc), extra_values=[key])
        return {"status": "error", "models": [], "error": "request failed: " + message[:200]}
    if endpoint["style"] == "gemini":
        models = _normalize_gemini(pages, now)
    else:
        models = _normalize_openai(pages[0], now)
    models.sort(key=lambda m: m["id"])
    return {"status": "ok", "models": models, "error": None}


def fetch_catalog(providers: Iterable[str], http_get: HttpGetter = default_http_get,
                  env: Mapping[str, str] | None = None,
                  clock: Callable[[], float] = time.time, timeout: float = 15.0) -> dict:
    """Build a catalog snapshot for ``providers``."""
    env = os.environ if env is None else env
    providers = list(providers)
    for provider in providers:
        _check_provider(provider)
    now = float(clock())
    snapshot = {"schema_version": SCHEMA_VERSION, "fetched_at": _iso(now),
                "fetched_at_epoch": now, "providers": {}}
    for provider in providers:
        snapshot["providers"][provider] = fetch_provider_models(provider, http_get, env, now,
                                                                timeout)
    return snapshot


def save_snapshot(snapshot: Mapping[str, Any], path: str) -> None:
    """Atomically persist a snapshot as JSON."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".catalog-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def load_snapshot(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported catalog snapshot schema_version")
    if not isinstance(data.get("providers"), dict):
        raise ValueError("catalog snapshot has no providers map")
    return data


def snapshot_age(snapshot: Mapping[str, Any], now: float) -> float | None:
    fetched = snapshot.get("fetched_at_epoch")
    if not isinstance(fetched, (int, float)) or isinstance(fetched, bool):
        fetched = _parse_date(snapshot.get("fetched_at"))
    return None if fetched is None else now - float(fetched)


def is_stale(snapshot: Mapping[str, Any], now: float, max_age_seconds: float) -> bool:
    age = snapshot_age(snapshot, now)
    return age is None or age > max_age_seconds or age < -300  # future dated is suspect


def _require_fresh(snapshot, now, max_age_seconds) -> None:
    if now is not None and max_age_seconds is not None and is_stale(snapshot, now,
                                                                     max_age_seconds):
        raise StaleCatalogError("catalog snapshot is stale; refresh discovery before routing")


def live_models(snapshot: Mapping[str, Any], provider: str,
                retired_ids: Iterable[str] = ()) -> list[dict]:
    """Selectable models: present, not deprecated, not denylisted, provider ok."""
    if is_retired(provider):
        return []
    entry = (snapshot.get("providers") or {}).get(provider) or {}
    if entry.get("status") != "ok":
        return []
    denied = set(retired_ids)
    return [m for m in entry.get("models") or []
            if isinstance(m, dict) and not m.get("deprecated") and m.get("id") not in denied]


def rank_models(models: list[dict], patterns: Iterable[str]) -> list[str]:
    """Order model ids by preference pattern, newest (descending id) first per pattern."""
    ranked: list[str] = []
    ids = [m["id"] for m in models]
    for pattern in patterns:
        if not isinstance(pattern, str):
            continue
        for model_id in sorted((i for i in ids if fnmatch.fnmatchcase(i, pattern)), reverse=True):
            if model_id not in ranked:
                ranked.append(model_id)
    return ranked


def resolve_capabilities(snapshot: Mapping[str, Any],
                         preferences: Mapping[str, Mapping[str, list[str]]],
                         retired_ids: Iterable[str] = (), now: float | None = None,
                         max_age_seconds: float | None = None) -> dict:
    """Map provider -> capability class -> ordered live model ids."""
    _require_fresh(snapshot, now, max_age_seconds)
    retired_ids = list(retired_ids)
    mapping: dict[str, dict[str, list[str]]] = {}
    for provider, classes in (preferences or {}).items():
        if provider not in KNOWN_PROVIDERS:
            continue
        models = live_models(snapshot, provider, retired_ids)
        mapping[provider] = {cls: rank_models(models, (classes or {}).get(cls) or [])
                             for cls in CAPABILITY_CLASSES}
    return mapping


def select_model(snapshot: Mapping[str, Any], provider: str, capability_class: str,
                 preferences: Mapping[str, Mapping[str, list[str]]],
                 retired_ids: Iterable[str] = (), now: float | None = None,
                 max_age_seconds: float | None = None) -> str | None:
    """Best live model id for ``provider`` and class, or None. Never falls back."""
    if capability_class not in CAPABILITY_CLASSES:
        raise ValueError("unknown capability class: %s" % capability_class)
    _require_fresh(snapshot, now, max_age_seconds)
    patterns = ((preferences or {}).get(provider) or {}).get(capability_class) or []
    ranked = rank_models(live_models(snapshot, provider, retired_ids), patterns)
    return ranked[0] if ranked else None


def model_pricing(snapshot: Mapping[str, Any], provider: str, model_id: str) -> dict | None:
    for model in live_models(snapshot, provider):
        if model["id"] == model_id:
            return {"input_usd_per_mtok": model.get("input_usd_per_mtok"),
                    "output_usd_per_mtok": model.get("output_usd_per_mtok")}
    return None
