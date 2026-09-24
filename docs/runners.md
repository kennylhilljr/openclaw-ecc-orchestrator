# Runners

Runners are the harnesses that do the work: coding CLIs (Claude Code, Codex) and HTTP APIs (Gemini, Groq, OpenRouter, Kimi). Code lives in `src/openclaw_ecc_orchestrator/runners/`.

| Module | Purpose |
| --- | --- |
| `registry.py` | Runner profiles, retired runner rejection, certified record filtering, model families |
| `discovery.py` | Live model catalogs, capability class mapping, snapshots and staleness |
| `base.py` | Command runner, HTTP poster, probe context, hard step timeout |
| `cli_adapters.py` | Claude and Codex argument builders and output parsers |
| `api_adapters.py` | Gemini, Groq, OpenRouter and Kimi HTTP adapters |
| `probes.py` | Certification probe engine producing certification records |
| `certify.py` | Live certification entry point (Phase 3): model selection, work root, evidence files |

## Provider policy

| Runner | Kind | Author classes | Roles | Notes |
| --- | --- | --- | --- | --- |
| claude | coding CLI | standard, advanced | author, review, coordination | haiku only for cheap coordination; production use needs the CLI hang resolved, which a passing cancellation check evidences |
| codex | coding CLI | all | author, review | subscription backed; aliases from `policy.cli_models` |
| gemini | API | all | author, review, validation | Flash class is the principal low cost worker |
| groq | API | economical | author, review, validation | low risk only; never alone for high risk code |
| openrouter | API | all | fallback, review | pinned `approved_models` and an explicit `data_policy` required |
| kimi | API | standard | author, review | optional; only with `large_context`; absent key means not configured |

Retired: windsurf, pi and the direct OpenAI API for routine coding (names `openai`, `openai-api`, `openai_api`, `openai-api-coding`). They have no profile. `registry.get_profile`, `registry.build_registry`, `probes.get_adapter` and `probes.probe_runner` raise `RetiredRunnerError` for them in any case or spacing. The policy validator rejects them, `registry.certified_runners` drops forged records for them, `select_runner` rejects them, and `discovery.fetch_catalog` refuses to query them.

## Model discovery

```python
fetch_catalog(providers, http_get, env, clock, timeout=15.0) -> snapshot
select_model(snapshot, provider, capability_class, preferences, retired_ids=(), now=None,
             max_age_seconds=None) -> str | None
resolve_capabilities(snapshot, preferences, retired_ids=(), now=None, max_age_seconds=None)
save_snapshot(snapshot, path); load_snapshot(path); is_stale(snapshot, now, max_age_seconds)
```

Clients: OpenAI compatible model listings for Groq, OpenRouter and Kimi, and the Gemini models list with pagination (only models supporting generateContent). `http_get(url, headers, timeout)` returns `HttpResponse(status, body)`. Keys come from the environment variable names `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `MOONSHOT_API_KEY` and `GEMINI_API_KEY`, travel only in headers (Bearer or `x-goog-api-key`), and never appear in URLs, snapshots or errors. A missing key gives the provider status `not_configured`.

TLS: `default_http_get` and `default_http_post` always verify certificates with `discovery.tls_context()`. When neither `SSL_CERT_FILE`/`SSL_CERT_DIR` nor Python's default store provides trust anchors, it loads the first existing system CA file (`/etc/ssl/cert.pem` on macOS). This matters on the operator's Mac: the python.org Python 3.13 build ships without a CA store, and every API request failed with `CERTIFICATE_VERIFY_FAILED` until this fallback (live finding, 2026-09-24).

Models flagged inactive, deprecated, retired, disabled, or past an expiration or deprecation date are kept in the snapshot with `deprecated: true` but are never selected. Preferences are ordered glob patterns per provider and class; within a pattern, ids sort in descending order, so newer versions usually win. Make patterns specific enough that a lite or preview variant does not outrank the model you want. `select_model` never falls back to an arbitrary model: if no live id matches, it returns None. Pass `now` and `max_age_seconds` to refuse a stale snapshot (`StaleCatalogError`); snapshots dated in the future are also treated as stale.

Snapshot shape: `{"schema_version": "1.0", "fetched_at": ISO, "fetched_at_epoch": float, "providers": {name: {"status": "ok" | "error" | "not_configured", "error": str | null, "models": [{"id", "deprecated", "input_usd_per_mtok", "output_usd_per_mtok", "context_length"}]}}}`. Snapshots are written atomically.

## Certification probes

```python
probe_runner(name, ctx: ProbeContext) -> certification record
```

`ProbeContext` injects `run_command(argv, timeout, cwd=None)`, `http_get`, `http_post(url, headers, body, timeout)`, `env`, `clock`, `step_timeout`, `cancel_timeout`, `make_workdir`, `cleanup_workdir`, `model` (catalog id or CLI alias to exercise), `policy` and `certification_ttl_seconds`. The default command runner accepts argument lists only, never a shell string, and closes stdin. It starts each probe step in its own process group with a minimal environment, and on timeout sends SIGTERM to the whole group, then SIGKILL after a grace period (`base.KILL_GRACE_SECONDS`, 2 seconds), so a CLI that spawned helpers cannot leave them running. See [Probe and runner environment](#probe-and-runner-environment).

Checks, in order:

1. `installed`: the version command succeeds (skipped for API runners).
2. `authentication`: the CLI status command reports a login (Claude: `loggedIn` in `claude auth status --json`; Codex: a `Logged in` line from `codex login status`), or an authenticated catalog request.
3. `live_inference`: a minimal prompt returns text. A run the CLI itself reports as failed (Claude `is_error`, Codex `turn.failed` or `error` events) fails the check with the CLI's message, even on exit 0.
4. `repo_exercise`: coding CLIs only. The probe writes a tiny Python project with one failing unittest (`calc.py`, `test_calc.py`) into a disposable repository and commits it. The agent must run the test, fix `calc.py`, rerun the test and commit. The probe then verifies everything itself and never trusts the agent's claim: the test failed before the run, passes after it (`python3 -B -m unittest -q`), a new commit exists, the tracked tree is clean, and `test_calc.py` is unchanged.
5. `cancellation_timeout`: coding CLIs only. The agent is asked to run `sleep 120`; after `cancel_timeout` seconds the probe sends SIGTERM, then SIGKILL, to the whole process group and proves that no process of the group remains (`base.default_cancel_runner`, which counts group members before the kill). A CLI that exits before the cancel point fails this check, since cancellation was not exercised. Injected command runners without `ctx.cancel_command` fall back to a timed `run_command`.
6. `log_inspection`: all runner output is scanned for secret looking content and for the literal key values.
7. `metadata_capture`: the inference reported a model and token or cost usage.

Every executed step runs in a worker thread with a watchdog of `STEP_WATCHDOG_FACTOR` (1.5) times `step_timeout`, because a step wraps several bounded commands; each command is itself bounded by `step_timeout` and, with the default command runner, its process group is killed. A hung CLI or HTTP call becomes a failed check with reason `timeout`; the probe never waits longer. Later steps whose prerequisites failed are recorded as `prerequisite failed: ...`. Details stored in the record are redacted and truncated. `ctx.on_step(check, phase)` reports progress (use it to see which step hangs) and `ctx.transcript`, when a list, receives every piece of runner output, redacted.

Certification record shape:

```json
{"schema_version": "1.0", "runner": "codex", "provider": "codex", "family": "openai",
 "kind": "coding_cli", "status": "certified",
 "checks": [{"name": "installed", "status": "pass", "reason": "installed",
             "duration_seconds": 0.01, "detail": "codex-cli 1.2.3"}],
 "certified_at": "2026-09-24T00:00:00Z", "expires_at": "2026-10-01T00:00:00Z",
 "version": "codex-cli 1.2.3", "models": ["alias"],
 "usage": {"model": "alias", "cost_usd": null, "input_tokens": 30, "output_tokens": 3},
 "notes": []}
```

Status is `certified` only when no check fails and the required checks pass (all seven for coding CLIs; authentication, live inference, log inspection and metadata capture for APIs). Missing credentials, or OpenRouter without an approved model and data policy, give `not_configured`. OpenRouter checks policy before the key, so its reason is `policy_not_approved: ...` whether or not a key exists. Anything else is `failed`. `registry.certified_runners(records, now)` keeps only valid, unexpired, certified records; uncertified runners are excluded, never shown as degraded but available.

## CLI invocations

Flags were confirmed on 2026-09-24 against Claude Code `2.1.281` and codex-cli `0.158.0-alpha.7` from each CLI's `--help`.

| | Claude Code | Codex |
| :- | :- | :- |
| non interactive | `claude -p --output-format json` | `codex exec --json` (JSONL events) |
| never block on prompts | `--permission-prompts none` (anything that would prompt is denied) | exec has no interactive approvals |
| no session files | `--no-session-persistence` | `--ephemeral` |
| model | `--model <alias or id>` | `-m <slug>` |
| reasoning effort | `--effort low` | `-c model_reasoning_effort="low"` |
| writable run | `--permission-mode acceptEdits --allowedTools "Read,Edit,Write,Bash(git *),Bash(python3 *)"` | `--sandbox workspace-write --add-dir <repo>/.git` (workspace-write keeps `.git` read only, so without it the agent cannot commit) |
| read only run | default tools, prompts denied | `--sandbox read-only` |
| working directory | process cwd | `-C <dir>` plus process cwd |
| authentication | `claude auth status --json` (`loggedIn`, `authMethod`) | `codex login status` |
| model catalog | none (aliases) | `codex debug models` (JSON, `slug`, `visibility`, `description`) |
| cost | `total_cost_usd`, `usage`, `modelUsage` in the result | token `usage` on `turn.completed`, no cost |

`build_invocation(prompt, model=None, cwd=None, writable=False)` returns these argument lists with the prompt after an end of options marker, so a prompt starting with a dash is never parsed as a flag. `build_cancel_invocation(model, cwd)` builds the cancellation task (Claude may only run `Bash(sleep *)`). Neither adapter ever passes a bypass flag (`--dangerously-skip-permissions`, `bypassPermissions`, `--dangerously-bypass-approvals-and-sandbox`). Pass `effort="low"` to the adapter constructor for the cheapest reasoning setting.

## Live certification

```
python3 -m openclaw_ecc_orchestrator.runners.certify \
    --runners claude,codex,gemini,groq,openrouter,kimi --out DIR \
    [--policy config.json] [--step-timeout 170] [--cancel-after N] [--effort low] [--claude-effort LEVEL] \
    [--claude-model ALIAS] [--codex-model SLUG] [--work-root DIR] [--dry-run]
```

Runners are certified one at a time. Output: `DIR/<runner>.json` (record, model selection, duration), `DIR/<runner>.transcript.log` (all runner output, redacted) and `DIR/summary.json` (results, `certified`, `excluded` from `registry.certified_runners`, and the Phase 3 gate: Claude and Codex certified and at least two economical workers). Everything written is passed through the redaction engine, and e-mail addresses and the home directory are masked. Progress lines (`[runner] check: start|pass|fail`) go to stderr. `--dry-run` executes nothing and writes `DIR/plan.json` with argv, allowlisted environment variable names and the model selection method.

* Every CLI command is bounded by `--step-timeout` (default 170, at most 180 seconds). The cancel point defaults to 60 seconds for Claude (its print mode start up alone took 25 to 50 seconds on the operator's Mac) and 30 for Codex.
* `--effort` (default `low`) sets the Codex reasoning effort; Claude keeps its CLI default unless `--claude-effort` is given.
* Models: Claude uses the `haiku` alias unless the policy's `cli_models.claude.economical` or `--claude-model` says otherwise (placeholders starting with `REPLACE_` are ignored). Codex picks from `codex debug models`, listed models only, by `model_preferences.codex.economical` patterns or the defaults `gpt-*-luna`, `*-luna`, `*mini*`. API runners fetch the live catalog and apply `model_preferences.<provider>.economical` patterns (built in defaults otherwise) with `select_model`, so Groq always uses a currently listed id.
* OpenRouter: only the policy's `approved_models` with a `data_policy`; otherwise `not_configured` with `policy_not_approved`. The public catalog is still requested without any key to record reachability.
* Disposable repositories live in a fresh `mkdtemp` work root, refused inside any git repository or `~/.openclaw`; each probe directory is removed after its result is recorded, then the empty root.
* Retired runner names abort the run before anything executes.

## Probe and runner environment

Probed CLIs never see the orchestrator's full environment. With the default command runner, `probe()` builds the child environment with `base.minimal_env(ctx.env, adapter.credential_env)`:

| Source | Names |
| :- | :- |
| base allowlist (`base.PROBE_ENV_ALLOW`) | `PATH`, `HOME`, `USER`, `LANG`, `TERM`, `TMPDIR` |
| `ClaudeAdapter.credential_env` | `HOME`, `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CONFIG_DIR` |
| `CodexAdapter.credential_env` | `HOME`, `CODEX_HOME` |
| API adapters | none (they start no processes; the key travels only in headers) |

Anything else in the parent, for example another provider's key or an orchestrator token, is dropped. Pass `credential_env=[...]` to an adapter constructor to declare more names (HOME is always kept). Values of declared names that look secret are registered with the probe's redactor, so a CLI echoing its key fails `log_inspection` and the value never reaches the record. An injected `ctx.run_command` is used as given; it owns its own environment.

HOME on macOS: both CLIs find their login through HOME (Claude Code reads its keychain entry and config directory, Codex reads `~/.codex`). A runner started under the process supervisor inherits only `PATH`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TZ` and `TMPDIR` by default, so HOME must be allowlisted for CLI runners or authentication fails. Use the adapter's declaration: `Conductor(..., runner_env_allow=ClaudeAdapter().env_allow())` (or the Codex equivalent) allowlists HOME and the adapter's credential names, nothing more. Credentials passed through `extra_env` are always treated as secrets by the supervisor's redactor, whatever the variable name.
