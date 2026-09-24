"""Live runner certification entry point (MIGRATION_SPEC Phase 3).

    python3 -m openclaw_ecc_orchestrator.runners.certify \\
        --runners claude,codex,gemini,groq,openrouter,kimi --out DIR [--dry-run]

Runs the seven certification checks (``probes.probe``) for each runner, one
runner at a time, and writes ``DIR/<runner>.json``, a redacted
``DIR/<runner>.transcript.log`` and ``DIR/summary.json``.

* Coding CLIs run with the minimal environment allowlist, every command under
  a hard wall clock bound (``--step-timeout``, at most 180 seconds), and the
  cheapest adequate model: Claude uses the ``haiku`` alias (or the policy's
  ``cli_models.claude.economical``); Codex picks from its live catalog
  (``codex debug models``) by economical preference patterns, never a
  hard-coded id.
* API runners discover models from the live provider catalog and pick an
  economical model by preference patterns (``discovery.select_model``).
  Missing keys give ``not_configured``.
* OpenRouter is certified only for operator approved pinned models with an
  explicit data policy (policy ``openrouter`` block). Without that it ends
  ``not_configured`` with reason ``policy_not_approved``; the public catalog
  is still checked for reachability without sending any key.
* Disposable repositories live in a fresh temporary work root outside any git
  repository; each is removed after its result is recorded.

``--dry-run`` executes nothing and prints the plan (argv, environment variable
names, model selection method).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shutil
import sys
import tempfile
import time
from typing import Any, Callable, Mapping

from ..handoffs.redaction import redact_text
from ..schemas import SCHEMA_VERSION
from .base import ProbeContext, default_command_runner, minimal_env
from .cli_adapters import ClaudeAdapter, CodexAdapter
from .discovery import (PROVIDER_ENDPOINTS, default_http_get, fetch_catalog, rank_models,
                        select_model)
from .probes import ADAPTERS, probe
from .registry import certified_runners, get_profile

DEFAULT_RUNNERS = ("claude", "codex", "gemini", "groq", "openrouter", "kimi")
MAX_STEP_TIMEOUT = 180.0
DEFAULT_STEP_TIMEOUT = 170.0
DEFAULT_CANCEL_AFTER = {"claude": 60.0, "codex": 30.0}
DEFAULT_EFFORT = "low"
DEFAULT_CLAUDE_MODEL = "haiku"
# Economical preference patterns (globs over live catalog ids, never ids).
DEFAULT_CODEX_PREFERENCES = ["gpt-*-luna", "*-luna", "*mini*"]
DEFAULT_API_PREFERENCES = {
    "gemini": ["gemini-*flash-lite", "gemini-*flash-lite-*", "gemini-*flash", "gemini-*flash-*"],
    "groq": ["*instant*", "*-8b*", "*small*", "*mini*"],
    "kimi": ["kimi-*", "moonshot-v1-8k*"],
    "openrouter": [],
}
ECONOMICAL_WORKERS = ("gemini", "groq", "kimi", "openrouter")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_TRANSCRIPT_LIMIT = 200_000


# ---------------------------------------------------------------- sanitizing

def sanitize_text(text: object, home: str | None = None, extra_values=()) -> str:
    """Redact secrets, mask e-mail addresses and the home directory."""
    out = redact_text(str(text if text is not None else ""), extra_values=list(extra_values))
    out = _EMAIL_RE.sub("[EMAIL]", out)
    if home and len(home) > 1:
        out = out.replace(home, "~")
    return out


def sanitize_obj(obj: Any, home: str | None = None) -> Any:
    if isinstance(obj, dict):
        return {k: sanitize_obj(v, home) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_obj(v, home) for v in obj]
    if isinstance(obj, str):
        return sanitize_text(obj, home)
    return obj


# ---------------------------------------------------------------- policy

def load_policy(path: str | None) -> dict:
    if not path:
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def _policy_value(value: object) -> str | None:
    if isinstance(value, str) and value.strip() and not value.startswith("REPLACE_"):
        return value.strip()
    return None


def claude_model(policy: Mapping, override: str | None) -> tuple[str, str]:
    if override:
        return override, "command line"
    configured = _policy_value(((policy.get("cli_models") or {}).get("claude") or {})
                               .get("economical"))
    if configured:
        return configured, "policy cli_models.claude.economical"
    return DEFAULT_CLAUDE_MODEL, "default economical alias"


def api_preferences(policy: Mapping, provider: str) -> list[str]:
    configured = ((policy.get("model_preferences") or {}).get(provider) or {}).get("economical")
    if isinstance(configured, list) and configured:
        return [p for p in configured if isinstance(p, str)]
    return list(DEFAULT_API_PREFERENCES.get(provider, []))


def codex_preferences(policy: Mapping) -> list[str]:
    configured = ((policy.get("model_preferences") or {}).get("codex") or {}).get("economical")
    if isinstance(configured, list) and configured:
        return [p for p in configured if isinstance(p, str)]
    return list(DEFAULT_CODEX_PREFERENCES)


def select_codex_model(catalog_json: str, patterns: list[str]) -> tuple[str | None, dict]:
    """Pick the Codex model from ``codex debug models`` output; listed models only."""
    try:
        data = json.loads(catalog_json)
    except (ValueError, TypeError):
        return None, {"error": "codex model catalog is not JSON"}
    raw = data.get("models") if isinstance(data, dict) else data
    models = []
    for entry in raw or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("slug"), str):
            continue
        if entry.get("visibility") not in (None, "list"):
            continue
        models.append({"id": entry["slug"], "description": entry.get("description")})
    ranked = rank_models(models, patterns)
    info = {"listed_models": len(models), "patterns": list(patterns)}
    if not ranked:
        return None, info
    chosen = ranked[0]
    info["description"] = next((m["description"] for m in models if m["id"] == chosen), None)
    return chosen, info


# ---------------------------------------------------------------- work root

def _inside_git_repo(path: str) -> bool:
    current = os.path.abspath(path)
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def make_work_root(base: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """Fresh ``mkdtemp`` directory outside every git repository and ~/.openclaw."""
    root = tempfile.mkdtemp(prefix="ecc-cert-work-", dir=base)
    real = os.path.realpath(root)
    home = (env or os.environ).get("HOME") or ""
    forbidden = os.path.realpath(os.path.join(home, ".openclaw")) if home else None
    if _inside_git_repo(real) or (forbidden and (real + os.sep).startswith(forbidden + os.sep)):
        os.rmdir(root)
        raise ValueError("work root must be outside git repositories and ~/.openclaw")
    return root


# ---------------------------------------------------------------- runner

@dataclasses.dataclass
class Options:
    runners: tuple[str, ...] = DEFAULT_RUNNERS
    out: str = "."
    dry_run: bool = False
    policy: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    step_timeout: float = DEFAULT_STEP_TIMEOUT
    cancel_after: float | None = None
    effort: str | None = DEFAULT_EFFORT          # Codex reasoning effort
    claude_effort: str | None = None             # Claude --effort; None keeps the default
    claude_model: str | None = None
    codex_model: str | None = None
    work_root: str | None = None


@dataclasses.dataclass
class Deps:
    """Injected effects; the defaults touch the real system."""
    run_command: Callable = default_command_runner
    http_get: Callable = default_http_get
    http_post: Callable | None = None
    cancel_command: Callable | None = None
    env: Mapping[str, str] = dataclasses.field(default_factory=lambda: os.environ)
    clock: Callable[[], float] = time.time
    log: Callable[[str], None] = lambda line: (sys.stderr.write(line + "\n"), sys.stderr.flush())
    make_workdir: Callable[[], str] | None = None
    cleanup_workdir: Callable[[str], None] | None = None
    write_file: Callable[[str, str], None] | None = None


def _adapter(name: str, opts: Options):
    cls = ADAPTERS[get_profile(name).name]
    if cls is ClaudeAdapter:
        return cls(effort=opts.claude_effort)
    if cls is CodexAdapter:
        return cls(effort=opts.effort)
    return cls()


def _discover_codex(adapter: CodexAdapter, opts: Options, deps: Deps) -> tuple[str | None, dict]:
    if opts.codex_model:
        return opts.codex_model, {"source": "command line"}
    argv = list(adapter.models_argv)
    if deps.run_command is default_command_runner:
        env = minimal_env(deps.env, adapter.credential_env)
        result = default_command_runner(argv, min(opts.step_timeout, 60.0), None, env)
    else:
        result = deps.run_command(argv, min(opts.step_timeout, 60.0))
    if result.timed_out or result.returncode != 0:
        return None, {"source": "codex debug models", "error": "catalog command failed (exit %s)"
                      % result.returncode}
    model, info = select_codex_model(result.stdout, codex_preferences(opts.policy))
    info["source"] = "codex debug models"
    return model, info


def _api_model(name: str, opts: Options, deps: Deps) -> tuple[str | None, dict]:
    if name == "openrouter":
        approved = ((opts.policy.get("openrouter") or {}).get("approved_models") or [])
        catalog = _openrouter_reachability(deps, opts)
        return (approved[0] if approved else None), {"source": "policy approved_models",
                                                     "catalog": catalog}
    snapshot = fetch_catalog([name], deps.http_get, deps.env, deps.clock,
                             timeout=min(opts.step_timeout, 30.0))
    entry = snapshot["providers"][name]
    info = {"source": "live catalog", "catalog_status": entry["status"],
            "catalog_error": entry.get("error"),
            "live_models": len([m for m in entry["models"] if not m.get("deprecated")]),
            "patterns": api_preferences(opts.policy, name)}
    if entry["status"] != "ok":
        return None, info
    prefs = {name: {"economical": info["patterns"]}}
    model = select_model(snapshot, name, "economical", prefs,
                         retired_ids=opts.policy.get("retired_model_ids") or ())
    return model, info


def _openrouter_reachability(deps: Deps, opts: Options) -> dict:
    """Unauthenticated catalog request: no key is ever sent for this."""
    try:
        response = deps.http_get(PROVIDER_ENDPOINTS["openrouter"]["url"],
                                 {"Accept": "application/json"}, min(opts.step_timeout, 30.0))
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "error": sanitize_text(type(exc).__name__)}
    count = None
    if response.status == 200:
        try:
            count = len(json.loads(response.body).get("data") or [])
        except (ValueError, AttributeError):
            count = None
    return {"reachable": response.status == 200, "http_status": response.status,
            "model_count": count}


def plan_runner(name: str, opts: Options, deps: Deps) -> dict:
    """What a live run would do; executes nothing."""
    profile = get_profile(name)
    adapter = _adapter(name, opts)
    plan: dict[str, Any] = {"runner": profile.name, "kind": profile.kind}
    if profile.kind == "coding_cli":
        model = (claude_model(opts.policy, opts.claude_model)[0] if name == "claude"
                 else opts.codex_model or "<discovered: codex debug models>")
        plan.update({
            "env_names": sorted(minimal_env(deps.env, adapter.credential_env)),
            "version_argv": adapter.version_argv, "auth_argv": adapter.auth_argv,
            "inference_argv": adapter.build_invocation("<probe prompt>", model, cwd="<workdir>"),
            "repo_argv": adapter.build_invocation("<exercise prompt>", model, cwd="<workdir>",
                                                  writable=True),
            "cancel_argv": adapter.build_cancel_invocation(model, cwd="<workdir>"),
            "step_timeout": opts.step_timeout,
            "cancel_after": opts.cancel_after or DEFAULT_CANCEL_AFTER.get(name, 30.0)})
        if name == "codex":
            plan["model_discovery_argv"] = adapter.models_argv
    else:
        env_name = PROVIDER_ENDPOINTS[profile.name]["env"]
        plan.update({"key_env_name": env_name, "key_present": bool(deps.env.get(env_name)),
                     "catalog_url": PROVIDER_ENDPOINTS[profile.name]["url"],
                     "patterns": api_preferences(opts.policy, profile.name)})
        if name == "openrouter":
            block = opts.policy.get("openrouter") or {}
            plan["policy_approved"] = bool(block.get("approved_models")
                                           and str(block.get("data_policy") or "").strip())
    return plan


def certify_runner(name: str, opts: Options, deps: Deps, work_root: str) -> dict:
    profile = get_profile(name)
    adapter = _adapter(name, opts)
    started = time.monotonic()
    selection: dict[str, Any]
    if name == "claude":
        model, source = claude_model(opts.policy, opts.claude_model)
        selection = {"source": source}
    elif name == "codex":
        model, selection = _discover_codex(adapter, opts, deps)
    else:
        model, selection = _api_model(name, opts, deps)
    selection["model"] = model
    deps.log("[%s] model selection: %s" % (name, sanitize_text(json.dumps(selection))))

    counter = {"n": 0}

    def make_workdir() -> str:
        counter["n"] += 1
        path = os.path.join(work_root, "%s-%d" % (name, counter["n"]))
        os.mkdir(path, 0o700)
        return path

    def cleanup_workdir(path: str) -> None:
        real = os.path.realpath(path)
        if os.path.dirname(real) == os.path.realpath(work_root):
            shutil.rmtree(real, ignore_errors=True)

    transcript: list[str] = []
    ctx_kwargs: dict[str, Any] = dict(
        run_command=deps.run_command, http_get=deps.http_get, env=deps.env, clock=deps.clock,
        step_timeout=min(float(opts.step_timeout), MAX_STEP_TIMEOUT),
        cancel_timeout=float(opts.cancel_after or DEFAULT_CANCEL_AFTER.get(name, 30.0)),
        make_workdir=deps.make_workdir or make_workdir,
        cleanup_workdir=deps.cleanup_workdir or cleanup_workdir,
        model=model, policy=opts.policy, transcript=transcript,
        on_step=lambda check, phase: deps.log("[%s] %s: %s" % (name, check, phase)))
    if deps.http_post is not None:
        ctx_kwargs["http_post"] = deps.http_post
    if deps.cancel_command is not None:
        ctx_kwargs["cancel_command"] = deps.cancel_command
    if deps.write_file is not None:
        ctx_kwargs["write_file"] = deps.write_file
    record = probe(adapter, ProbeContext(**ctx_kwargs))
    return {"runner": profile.name, "record": record, "model_selection": selection,
            "duration_seconds": round(time.monotonic() - started, 3),
            "transcript": transcript}


def evaluate_gate(records: list[dict]) -> dict:
    by_runner = {r["runner"]: r["status"] for r in records}
    coding = all(by_runner.get(n) == "certified" for n in ("claude", "codex"))
    workers = [n for n in ECONOMICAL_WORKERS if by_runner.get(n) == "certified"]
    return {"coding_runners_certified": coding, "economical_workers_certified": workers,
            "gate_met": coding and len(workers) >= 2}


def _write_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def run(opts: Options, deps: Deps | None = None) -> dict:
    deps = deps or Deps()
    names = [get_profile(n).name for n in opts.runners]    # retired names raise here
    os.makedirs(opts.out, exist_ok=True)
    home = deps.env.get("HOME")
    if opts.dry_run:
        plans = [plan_runner(n, opts, deps) for n in names]
        summary = {"schema_version": SCHEMA_VERSION, "dry_run": True, "plans": plans}
        _write_json(os.path.join(opts.out, "plan.json"), sanitize_obj(summary, home))
        return summary
    work_root = opts.work_root or make_work_root(env=deps.env)
    results = []
    try:
        for name in names:
            deps.log("[%s] certification start" % name)
            result = certify_runner(name, opts, deps, work_root)
            transcript = result.pop("transcript")
            result = sanitize_obj(result, home)
            _write_json(os.path.join(opts.out, "%s.json" % name), result)
            with open(os.path.join(opts.out, "%s.transcript.log" % name), "w",
                      encoding="utf-8") as fh:
                fh.write(sanitize_text("\n".join(transcript), home)[:_TRANSCRIPT_LIMIT])
            deps.log("[%s] status: %s" % (name, result["record"]["status"]))
            results.append(result)
    finally:
        if not opts.work_root:
            try:
                os.rmdir(work_root)       # only if empty: everything inside was ours
            except OSError:
                pass
    records = [r["record"] for r in results]
    included, excluded = certified_runners(records, float(deps.clock()))
    summary = {"schema_version": SCHEMA_VERSION, "dry_run": False, "results": results,
               "certified": sorted(included), "excluded": excluded,
               "gate": evaluate_gate(records)}
    _write_json(os.path.join(opts.out, "summary.json"), summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m openclaw_ecc_orchestrator.runners.certify",
                                     description="Live runner certification.")
    parser.add_argument("--runners", default=",".join(DEFAULT_RUNNERS))
    parser.add_argument("--out", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--policy")
    parser.add_argument("--step-timeout", type=float, default=DEFAULT_STEP_TIMEOUT)
    parser.add_argument("--cancel-after", type=float)
    parser.add_argument("--effort", default=DEFAULT_EFFORT, help="Codex reasoning effort")
    parser.add_argument("--claude-effort", help="Claude --effort (default: CLI default)")
    parser.add_argument("--claude-model")
    parser.add_argument("--codex-model")
    parser.add_argument("--work-root")
    args = parser.parse_args(argv)
    if not 0 < args.step_timeout <= MAX_STEP_TIMEOUT:
        parser.error("--step-timeout must be in (0, %d]" % MAX_STEP_TIMEOUT)
    runners = tuple(n.strip() for n in args.runners.split(",") if n.strip())
    opts = Options(runners=runners, out=args.out, dry_run=args.dry_run,
                   policy=load_policy(args.policy), step_timeout=args.step_timeout,
                   cancel_after=args.cancel_after, effort=args.effort or None,
                   claude_effort=args.claude_effort or None,
                   claude_model=args.claude_model, codex_model=args.codex_model,
                   work_root=args.work_root)
    summary = run(opts)
    if opts.dry_run:
        print(json.dumps(sanitize_obj(summary, os.environ.get("HOME")), indent=2))
        return 0
    for result in summary["results"]:
        rec = result["record"]
        failed = [c["name"] + ": " + c["reason"] for c in rec["checks"] if c["status"] == "fail"]
        print("%-11s %-15s %s" % (rec["runner"], rec["status"], "; ".join(failed)[:300]))
    print("gate: %s" % json.dumps(summary["gate"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
