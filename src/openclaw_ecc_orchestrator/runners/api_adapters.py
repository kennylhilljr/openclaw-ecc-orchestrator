"""HTTP API runner adapters (Gemini, Groq, OpenRouter, Kimi).

Keys come from environment variable names and only travel in headers. The
model exercised is ``ctx.model``, which callers resolve from the live
catalog (``discovery.select_model``); nothing here names a model id.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from .base import ProbeContext, StepOutcome
from .cli_adapters import PROBE_PROMPT
from .discovery import PROVIDER_ENDPOINTS
from .registry import get_profile


class ApiAdapter:
    name = ""
    base_url = ""

    def __init__(self):
        self.profile = get_profile(self.name)
        self.env_name = PROVIDER_ENDPOINTS[self.name]["env"]
        self.models_url = PROVIDER_ENDPOINTS[self.name]["url"]
        # Declared credential names; API adapters start no processes, but the
        # attribute keeps the adapter interface uniform with the CLI adapters.
        self.credential_env = (self.env_name,)

    def _key(self, ctx: ProbeContext) -> str:
        return ctx.env.get(self.env_name) or ""

    def secret_values(self, ctx: ProbeContext) -> list[str]:
        key = self._key(ctx)
        return [key] if key else []

    def headers(self, ctx: ProbeContext) -> dict[str, str]:
        return {"Authorization": "Bearer " + self._key(ctx), "Accept": "application/json"}

    def configured(self, ctx: ProbeContext) -> tuple[bool, str]:
        if not self._key(ctx):
            return False, "environment variable %s is not set" % self.env_name
        return True, ""

    def installed(self, ctx: ProbeContext) -> StepOutcome:
        return StepOutcome("skip", "api runner: no local installation")

    def authentication(self, ctx: ProbeContext) -> StepOutcome:
        response = ctx.http_get(self.models_url, self.headers(ctx), ctx.step_timeout)
        if response.status == 200:
            return StepOutcome("pass", "authenticated")
        if response.status in (401, 403):
            return StepOutcome("fail", "authentication rejected (HTTP %d)" % response.status)
        return StepOutcome("fail", "catalog request failed (HTTP %d)" % response.status)

    def inference_request(self, ctx: ProbeContext) -> tuple[str, dict]:
        return (self.base_url + "/chat/completions",
                {"model": ctx.model, "max_tokens": 8, "temperature": 0,
                 "messages": [{"role": "user", "content": PROBE_PROMPT}]})

    def parse_inference(self, data: dict, ctx: ProbeContext) -> dict[str, Any]:
        choices = data.get("choices") or [{}]
        message = (choices[0] or {}).get("message") or {}
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return {"text": message.get("content") or "", "model": data.get("model") or ctx.model,
                "cost_usd": usage.get("cost"),
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens")}

    def live_inference(self, ctx: ProbeContext) -> StepOutcome:
        if not ctx.model:
            return StepOutcome("fail", "no live model resolved from the catalog")
        url, body = self.inference_request(ctx)
        response = ctx.http_post(url, self.headers(ctx), body, ctx.step_timeout)
        if response.status != 200:
            return StepOutcome("fail", "inference failed (HTTP %d)" % response.status)
        try:
            data = json.loads(response.body)
        except ValueError:
            return StepOutcome("fail", "inference returned invalid JSON")
        if not isinstance(data, dict):
            return StepOutcome("fail", "inference returned an unexpected shape")
        parsed = self.parse_inference(data, ctx)
        if not isinstance(parsed.get("text"), str) or not parsed["text"].strip():
            return StepOutcome("fail", "inference returned no text")
        metadata = {k: parsed.get(k) for k in ("model", "cost_usd", "input_tokens",
                                               "output_tokens")}
        return StepOutcome("pass", "inference ok", parsed["text"][:200], metadata)

    def repo_exercise(self, ctx: ProbeContext) -> StepOutcome:
        return StepOutcome("skip", "not a coding runner")

    def cancellation(self, ctx: ProbeContext) -> StepOutcome:
        return StepOutcome("skip", "api runner: per request timeout enforced by the harness")


class GroqAdapter(ApiAdapter):
    name = "groq"
    base_url = "https://api.groq.com/openai/v1"


class KimiAdapter(ApiAdapter):
    name = "kimi"
    base_url = "https://api.moonshot.ai/v1"


class OpenRouterAdapter(ApiAdapter):
    name = "openrouter"
    base_url = "https://openrouter.ai/api/v1"

    def configured(self, ctx: ProbeContext) -> tuple[bool, str]:
        # Policy first: without an operator approved model list and data
        # policy nothing is sent to OpenRouter, whether or not a key exists.
        block = (ctx.policy or {}).get("openrouter") or {}
        approved = block.get("approved_models") or []
        if not approved or not str(block.get("data_policy") or "").strip():
            return False, ("policy_not_approved: openrouter requires pinned approved_models "
                           "and an explicit data_policy")
        if ctx.model not in approved:
            return False, "policy_not_approved: model is not in openrouter.approved_models"
        return super().configured(ctx)


class GeminiAdapter(ApiAdapter):
    name = "gemini"
    base_url = "https://generativelanguage.googleapis.com/v1beta"

    def headers(self, ctx: ProbeContext) -> dict[str, str]:
        return {"x-goog-api-key": self._key(ctx), "Accept": "application/json"}

    def inference_request(self, ctx: ProbeContext) -> tuple[str, dict]:
        model = urllib.parse.quote(str(ctx.model), safe="")
        return ("%s/models/%s:generateContent" % (self.base_url, model),
                {"contents": [{"parts": [{"text": PROBE_PROMPT}]}],
                 "generationConfig": {"maxOutputTokens": 8, "temperature": 0}})

    def parse_inference(self, data: dict, ctx: ProbeContext) -> dict[str, Any]:
        text = ""
        for cand in data.get("candidates") or []:
            for part in ((cand or {}).get("content") or {}).get("parts") or []:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    text += part["text"]
        usage = data.get("usageMetadata") if isinstance(data.get("usageMetadata"), dict) else {}
        return {"text": text, "model": data.get("modelVersion") or ctx.model, "cost_usd": None,
                "input_tokens": usage.get("promptTokenCount"),
                "output_tokens": usage.get("candidatesTokenCount")}
