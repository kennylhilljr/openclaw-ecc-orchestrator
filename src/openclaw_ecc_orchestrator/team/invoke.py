"""Running one persona turn on a coding CLI (Claude Code, Codex) or an API
provider (Gemini, Kimi, Groq). Text in, text out; the caller parses the
verdict. API keys come from the environment (bin/with-keychain-secrets) and
only travel in request headers."""

import json
import os

from ..runners.base import ProbeContext, default_http_post

API_MAX_OUTPUT_TOKENS = 8192


def _stdout_from_log(path):
    if not path or not os.path.isfile(path):
        return ""
    out = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("[stdout] "):
                out.append(line[len("[stdout] "):])
    return "".join(out)


def run_cli(adapter, model, prompt, *, cwd, writable, supervisor, log_path, timeout=1800.0, argv_override=None):
    """One turn of a coding CLI. `argv_override(prompt) -> argv` replaces the
    adapter command (tests)."""
    argv = argv_override(prompt) if argv_override else adapter.build_invocation(prompt, model=model, cwd=cwd,
                                                                                 writable=writable)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    res = supervisor.run(list(argv), cwd=cwd, env_allow=tuple(adapter.env_allow()), log_path=log_path,
                         timeout=timeout)
    stdout = _stdout_from_log(log_path)
    parsed = adapter.parse_output(stdout) if hasattr(adapter, "parse_output") and not argv_override else {}
    text = parsed.get("text") if parsed.get("text") else stdout
    error = parsed.get("error") or res.get("error")
    if res.get("exit_code") not in (0, None) and not error:
        error = f"exit {res.get('exit_code')}"
    if res.get("timed_out"):
        error = "timed out"
    return {"text": text or "", "error": error, "exit_code": res.get("exit_code"), "model": parsed.get("model") or model,
            "cost_usd": parsed.get("cost_usd"), "log_path": log_path}


def run_api(adapter, model, prompt, *, timeout=300.0, env=None, http_post=None,
            max_output_tokens=API_MAX_OUTPUT_TOKENS):
    """One turn of an API provider through its certified adapter."""
    ctx = ProbeContext(env=env if env is not None else os.environ, model=model, step_timeout=timeout,
                       http_post=http_post or default_http_post)
    ok, why = adapter.configured(ctx)
    if not ok:
        return {"text": "", "error": f"not configured: {why} (run ecc through bin/with-keychain-secrets)",
                "model": model, "cost_usd": None}
    url, body = adapter.inference_request(ctx)
    if "messages" in body:
        body["messages"] = [{"role": "user", "content": prompt}]
        body["max_tokens"] = max_output_tokens
    elif "contents" in body:
        body["contents"] = [{"parts": [{"text": prompt}]}]
        body.setdefault("generationConfig", {})["maxOutputTokens"] = max_output_tokens
    else:  # pragma: no cover - every certified API adapter uses one of the two shapes
        return {"text": "", "error": "unsupported request shape", "model": model, "cost_usd": None}
    response = ctx.http_post(url, adapter.headers(ctx), body, timeout)
    if response.status != 200:
        return {"text": "", "error": f"HTTP {response.status}", "model": model, "cost_usd": None}
    try:
        data = json.loads(response.body)
    except ValueError:
        return {"text": "", "error": "invalid JSON from provider", "model": model, "cost_usd": None}
    parsed = adapter.parse_inference(data, ctx)
    return {"text": parsed.get("text") or "", "error": None if parsed.get("text") else "empty reply",
            "model": parsed.get("model") or model, "cost_usd": parsed.get("cost_usd"),
            "input_tokens": parsed.get("input_tokens"), "output_tokens": parsed.get("output_tokens")}
