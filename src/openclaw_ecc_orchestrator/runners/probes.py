"""Runner readiness probes producing certification records.

Check list (``schemas.CERTIFICATION_CHECKS``): installed, authentication,
live_inference, repo_exercise, cancellation_timeout, log_inspection,
metadata_capture. Every executed step runs under a hard timeout
(``ctx.step_timeout``); a step that does not return in time is a failed
check with reason ``"timeout"`` and the probe moves on. Every byte of runner
output is recorded, scanned for secrets (log_inspection) and redacted before
anything is stored in the record.

Status: ``certified`` only when no check fails and the required checks pass
(API runners may skip repo_exercise, cancellation_timeout and installed).
Missing credentials or policy yield ``not_configured`` (excluded, not an
error). Retired runners raise ``RetiredRunnerError`` before any probing.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import functools
import time
from typing import Callable

from ..handoffs.redaction import contains_secret, redact_text
from ..schemas import CERTIFICATION_CHECKS, SCHEMA_VERSION
from .api_adapters import GeminiAdapter, GroqAdapter, KimiAdapter, OpenRouterAdapter
from .base import (CancelResult, CommandResult, ProbeContext, StepOutcome,
                   default_cancel_runner, default_command_runner, default_http_post,
                   minimal_env, run_with_timeout)
from .cli_adapters import ClaudeAdapter, CodexAdapter
from .discovery import HttpResponse
from .registry import get_profile, model_family

__all__ = ["ADAPTERS", "get_adapter", "probe", "probe_runner", "default_command_runner",
           "default_http_post", "CommandResult", "ProbeContext"]

ADAPTERS = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "gemini": GeminiAdapter,
    "groq": GroqAdapter,
    "openrouter": OpenRouterAdapter,
    "kimi": KimiAdapter,
}

_STEP_ORDER = (
    ("installed", "installed", ()),
    ("authentication", "authentication", ("installed",)),
    ("live_inference", "live_inference", ("installed", "authentication")),
    ("repo_exercise", "repo_exercise", ("installed", "authentication")),
    ("cancellation_timeout", "cancellation", ("installed", "authentication")),
)
_ALWAYS_REQUIRED = ("authentication", "live_inference", "log_inspection", "metadata_capture")
_CODING_REQUIRED = ("installed", "repo_exercise", "cancellation_timeout")
_DETAIL_LIMIT = 500
# A step wraps several bounded commands (the agent run is one of them), so the
# step's own watchdog allows a margin over ``ctx.step_timeout``.
STEP_WATCHDOG_FACTOR = 1.5


def get_adapter(name: str):
    """Adapter instance for ``name``; retired names raise RetiredRunnerError."""
    profile = get_profile(name)
    return ADAPTERS[profile.name]()


def probe_runner(name: str, ctx: ProbeContext) -> dict:
    return probe(get_adapter(name), ctx)


def _iso(epoch: float) -> str:
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _recording_context(ctx: ProbeContext, sink: list[str]) -> ProbeContext:
    run_command, http_get, http_post = ctx.run_command, ctx.http_get, ctx.http_post
    cancel_command = ctx.cancel_command

    def rec_command(argv, timeout, cwd=None):
        result = run_command(list(argv), timeout, cwd) if cwd is not None else run_command(
            list(argv), timeout)
        sink.append(str(result.stdout or ""))
        sink.append(str(result.stderr or ""))
        return result

    def rec_get(url, headers, timeout) -> HttpResponse:
        response = http_get(url, headers, timeout)
        sink.append(str(response.body or ""))
        return response

    def rec_post(url, headers, body, timeout) -> HttpResponse:
        response = http_post(url, headers, body, timeout)
        sink.append(str(response.body or ""))
        return response

    def rec_cancel(argv, cancel_after, cwd=None) -> CancelResult:
        result = cancel_command(list(argv), cancel_after, cwd)
        sink.append(str(result.stdout or ""))
        sink.append(str(result.stderr or ""))
        return result

    return dataclasses.replace(ctx, run_command=rec_command, http_get=rec_get,
                               http_post=rec_post,
                               cancel_command=rec_cancel if cancel_command else None)


def _isolated_context(adapter, ctx: ProbeContext) -> ProbeContext:
    """With the default command runner, probed processes get only the minimal
    allowlisted environment plus the adapter's declared credential names."""
    if ctx.run_command is not default_command_runner:
        return ctx
    env = minimal_env(ctx.env, getattr(adapter, "credential_env", ()))
    cancel = ctx.cancel_command
    if cancel is None or cancel is default_cancel_runner:
        cancel = functools.partial(default_cancel_runner, env=env)
    return dataclasses.replace(ctx, run_command=functools.partial(default_command_runner, env=env),
                               cancel_command=cancel)


def _notify(ctx: ProbeContext, check: str, phase: str) -> None:
    if ctx.on_step is None:
        return
    try:
        ctx.on_step(check, phase)
    except Exception:  # noqa: BLE001 - progress reporting never breaks a probe
        pass


def probe(adapter, ctx: ProbeContext) -> dict:
    profile = adapter.profile
    ctx = _isolated_context(adapter, ctx)
    now = float(ctx.clock())
    secrets = list(adapter.secret_values(ctx))

    def clean(text: object) -> str:
        return redact_text(text, extra_values=secrets)[:_DETAIL_LIMIT]

    checks: list[dict] = []
    outcomes: dict[str, StepOutcome] = {}
    configured, why = adapter.configured(ctx)

    if not configured:
        for name in CERTIFICATION_CHECKS:
            checks.append({"name": name, "status": "skip", "reason": clean(why),
                           "duration_seconds": 0.0})
        status = "not_configured"
    else:
        sink: list[str] = []
        rctx = _recording_context(ctx, sink)
        for check_name, method, prereqs in _STEP_ORDER:
            blocked = [p for p in prereqs if outcomes.get(p) and outcomes[p].status == "fail"]
            started = time.monotonic()
            if blocked:
                outcome = StepOutcome("fail", "prerequisite failed: %s" % ", ".join(blocked))
            else:
                _notify(ctx, check_name, "start")
                fn: Callable[[], StepOutcome] = (lambda m=getattr(adapter, method): m(rctx))
                outcome = run_with_timeout(fn, ctx.step_timeout * STEP_WATCHDOG_FACTOR)
            _notify(ctx, check_name, outcome.status)
            outcomes[check_name] = outcome
            checks.append({"name": check_name, "status": outcome.status,
                           "reason": clean(outcome.reason),
                           "duration_seconds": round(time.monotonic() - started, 3),
                           "detail": clean(outcome.detail)})

        if isinstance(ctx.transcript, list):
            ctx.transcript.extend(redact_text(t, extra_values=secrets) for t in sink if t)
        leaked = any(contains_secret(t) or any(s and s in t for s in secrets)
                     for t in list(sink) + [o.detail for o in outcomes.values()])
        checks.append({"name": "log_inspection", "status": "fail" if leaked else "pass",
                       "reason": "secret-like content found in runner output (redacted)"
                       if leaked else "no secret-like content in runner output",
                       "duration_seconds": 0.0})

        meta = outcomes["live_inference"].metadata if outcomes["live_inference"].status == \
            "pass" else {}
        has_usage = any(meta.get(k) is not None for k in ("cost_usd", "input_tokens",
                                                            "output_tokens"))
        meta_ok = bool(meta.get("model")) and has_usage
        checks.append({"name": "metadata_capture", "status": "pass" if meta_ok else "fail",
                       "reason": "model and usage captured" if meta_ok
                       else "model or usage/cost metadata missing",
                       "duration_seconds": 0.0})

        by_name = {c["name"]: c["status"] for c in checks}
        required = _ALWAYS_REQUIRED + (_CODING_REQUIRED if profile.kind == "coding_cli" else ())
        certified = (all(s != "fail" for s in by_name.values())
                     and all(by_name.get(n) == "pass" for n in required))
        status = "certified" if certified else "failed"

    installed_meta = outcomes.get("installed").metadata if outcomes.get("installed") else {}
    inference_meta = (outcomes.get("live_inference").metadata
                      if outcomes.get("live_inference") else {})
    model = inference_meta.get("model") or ctx.model
    usage = None
    if inference_meta:
        usage = {k: inference_meta.get(k) for k in ("model", "cost_usd", "input_tokens",
                                                     "output_tokens")}
        usage["model"] = clean(usage["model"]) if usage["model"] else None
    return {
        "schema_version": SCHEMA_VERSION,
        "runner": profile.name,
        "provider": profile.provider,
        "family": model_family(profile.name, model),
        "kind": profile.kind,
        "status": status,
        "checks": checks,
        "certified_at": _iso(now),
        "expires_at": _iso(now + ctx.certification_ttl_seconds) if status == "certified" else None,
        "version": clean(installed_meta.get("version")) if installed_meta.get("version") else None,
        "models": [clean(model)] if model and configured else [],
        "usage": usage,
        "notes": [profile.notes] if profile.notes else [],
    }
