"""Running one persona turn on a coding CLI (Claude Code, Codex) or an API
provider (Gemini, Kimi, Groq). Text in, text out; the caller parses the
verdict. API keys come from the environment (bin/with-keychain-secrets) and
only travel in request headers."""

import json
import os

from ..runners.base import ProbeContext, default_http_post

# Reasoning models (kimi-k3, gpt-oss) think before answering; on a large review
# pack 8192 tokens ended in an empty reply (live finding, rmcp-m0f). Billing is
# per token produced, so a high ceiling costs nothing unless used.
API_MAX_OUTPUT_TOKENS = 32768


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
            max_output_tokens=API_MAX_OUTPUT_TOKENS, log_path=None):
    """One turn of an API provider through its certified adapter. An empty
    reply cut off by the output budget is retried once with twice the budget.
    `log_path` receives the turn's metadata and the (redacted) reply."""
    res = _api_once(adapter, model, prompt, timeout, env, http_post, max_output_tokens)
    if (res.get("error") or "").startswith("empty reply") and res.get("finish_reason") in ("length", "MAX_TOKENS"):
        res = _api_once(adapter, model, prompt, timeout, env, http_post, max_output_tokens * 2)
        res["retried_with_tokens"] = max_output_tokens * 2
    if log_path:
        _write_api_log(log_path, adapter, res)
    return res


def _write_api_log(path, adapter, res):
    from ..handoffs.redaction import redact_text
    os.makedirs(os.path.dirname(path), exist_ok=True)
    meta = {k: res.get(k) for k in ("model", "error", "finish_reason", "input_tokens", "output_tokens",
                                    "reasoning_chars", "retried_with_tokens", "cost_usd")}
    meta["provider"] = adapter.name
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(meta) + "\n")  # our own fields: counts, names, error summaries
        fh.write(redact_text(res.get("text") or "") + "\n")


def _api_once(adapter, model, prompt, timeout, env, http_post, max_output_tokens):
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
    message = (((data.get("choices") or [{}])[0] or {}).get("message") or {}) if isinstance(data, dict) else {}
    reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
    out = {"text": parsed.get("text") or "", "model": parsed.get("model") or model,
           "cost_usd": parsed.get("cost_usd"), "input_tokens": parsed.get("input_tokens"),
           "output_tokens": parsed.get("output_tokens"), "finish_reason": parsed.get("finish_reason"),
           "reasoning_chars": len(reasoning) if isinstance(reasoning, str) else None, "error": None}
    if not out["text"]:
        out["error"] = "empty reply (finish_reason %s, %s output tokens)" % (
            out["finish_reason"], out["output_tokens"])
    return out
