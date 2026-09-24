# Routing

Routing decides which tier, runner and model handle a work unit, who reviews it, and when to escalate. Everything lives in `src/openclaw_ecc_orchestrator/routing/` and uses only the standard library. Every external effect (clock, catalog, breaker) is injected.

## Documents and validation

`schemas.py` is the authoritative validator for eight versioned documents: work unit, handoff, repository policy, routing decision, usage record, run status, verification result and runner certification record. Each carries `schema_version: "1.0"`. The JSON Schema (draft 2020-12) copies in `schemas/` exist for tooling; `tests/test_schemas_json.py` keeps their `required` lists and enum values in step with `schemas.DOCUMENT_SPECS`.

`.orchestration/config.yaml` is loaded with `schemas.load_repository_policy(text)`, which parses JSON only. Write the file as JSON compatible YAML (any JSON document is valid YAML 1.2) or parse it yourself and pass the resulting dict to `validate_repository_policy`.

Validators return a `ValidationReport` with `errors`, `violations` and `to_envelope(operation)`, which produces the standard result envelope (`ok`, `operation`, `changed`, `checks`, `warnings`, `required_user_actions`, `rollback_checkpoint`). Error text is redacted, so a report never echoes a credential that was present in the input.

Rejected inputs include: unknown `schema_version`; ids not matching `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` or that look like credentials; absolute, home relative, drive letter, control character, percent encoded or full width traversal paths in `scope.files`; negative budgets; tiers outside 0..2; `initial_tier` above `maximum_tier`; retired providers anywhere in the policy; OpenRouter without pinned `approved_models` and an explicit `data_policy`.

Handoff checks, when the work unit is passed in: `unit_id` must match, every changed file must be inside `scope.files` (a `file_outside_scope` violation otherwise), `succeeded` requires at least one command and every command exit code 0 with no unresolved failures (a `success_with_failures` violation otherwise), and no string may look like a secret (a `secret_detected` violation naming the field, never the value).

## Classification

```python
classify(unit, policy, diff_stats=None, *, previous_attempt_failed=False) -> Classification
```

`diff_stats` may hold `files` (changed paths), `files_changed`, `lines_changed`, or `lines_added` plus `lines_removed`. Without it the scope list is used.

| Signal | Points | Trigger |
| :- | :- | :- |
| mechanical_small | minus 2 | trait `mechanical` and at most 3 files |
| fixture_generation | minus 2 | trait `fixture_generation` |
| many_files | plus 1 | more than 3 files |
| many_modules | plus 2 | 3 or more distinct directories |
| large_diff | plus 1 | more than 300 changed lines |
| database | plus 3 | trait, or path tokens such as migrations, schema, sql, alembic |
| security | plus 4 | trait, required secrets, or path tokens such as auth, secret, credential, crypto |
| concurrency | plus 3 | trait, or path tokens such as thread, lock, scheduler, subprocess |
| architecture | plus 3 | trait, or path tokens such as contract, interface, protocol |
| no_acceptance_test | plus 2 | empty `acceptance.commands` |
| previous_attempt_failed | plus 2 | keyword argument or trait |

Paths are split into tokens on `/ _ . -`, whitespace, digits and case boundaries (`AuthService.ts` gives `auth` and `service`, `OAuth2Client` gives `o`, `auth` and `client`); each unsplit segment is kept whole too, so compound words such as `authmiddleware` are seen. A signal fires when a token equals a keyword, starts with a keyword prefix, or ends with a keyword suffix (`accesstoken`, `basicauth`).

| Signal | Keywords (whole token, prefix or suffix) |
| :- | :- |
| security | auth, authn, authz, oauth, jwt, jwk, rbac, abac, acl, iam, sso, saml, mfa, otp, tls, ssl, login, security, secret(s), credential(s), token(s), crypto, password(s), passwd, session(s), permission(s), privilege(s), cert(s), keystore, apikey |
| database | migration(s), schema(s), sql, db, database, alembic, ddl |

Examples that match: `src/AuthService.ts`, `src/middleware/authMiddleware.ts`, `src/rbac.py`, `app/models/user_token.py`, `lib/OAuth2Client.java`, `db/Migrations/0001.sql`, `src/UserMigration.ts`. Deliberate non matches: `author`, `authority` (different words, though `authorization` does match) and `tokenizer` (text processing, not credentials), so `docs/author.md` and `src/tokenizer_utils.py` are not security paths; `clock.py` is not a lock. Everything else errs toward a false positive, which only raises the tier and review tier; a false negative could route security work to a cheap unreviewed path. `policy.high_risk_paths` globs remain the authoritative override for anything the heuristics miss. Path heuristics can only raise a score; declare traits (`mechanical`, `fixture_generation`, `database`, `security`, `concurrency`, `architecture`, `previous_attempt_failed`) to be explicit.

Score to tier: 0 or below is Tier 0, 1 to 5 is Tier 1, 6 or more is Tier 2.

Overrides, applied in order:

1. A scope or diff path overlapping any `policy.high_risk_paths` glob forces risk high. A scope glob overlaps when either literal prefix contains the other, which errs toward high.
2. Risk sets a floor: high 2, medium 1, low 0.
3. `routing.initial_tier` raises the floor.
4. `routing.maximum_tier` caps the score tier.
5. If the risk floor exceeds `maximum_tier` the classification is invalid (`ok` false). Nothing lowers a floor.

`Classification` fields: `ok`, `unit_id`, `score`, `signals` (list of `{name, points, reason}`), `risk`, `declared_risk`, `risk_minimum_tier`, `minimum_tier`, `score_tier`, `chosen_tier`, `maximum_tier`, `review_tier`, `requires_independent_review`, `small_deterministic`, `reasons`, `errors`. `to_routing_decision(decided_at, runner, provider, model, estimated_cost_usd)` builds a routing decision document.

`review_tier` is 2 for security, database or architecture signals, high risk, or a Tier 2 choice; 0 for a small deterministic diff (low risk, acceptance commands present, at most 3 files, at most 50 changed lines or a mechanical or fixture trait); 1 otherwise.

## Runner selection

```python
select_runner(tier, certified_runners, catalog, policy, exclude_providers=(), *,
              role="author", risk="low", breaker=None, now=None, large_context=False,
              exclude_runners=(), exclude_families=()) -> Selection
```

`certified_runners` may be a list of names, a list of certification records, or the dict returned by `registry.certified_runners`. Tier maps to a capability class: 0 economical, 1 standard, 2 advanced.

A candidate must be: not retired, a known profile, allowed by `policy.allowed_providers`, not excluded, not behind an open breaker, permitted in the role, serving the class, allowed for the risk (Groq never for high risk), satisfying its condition (Kimi only with `large_context`), and resolvable to a model. API runners resolve from the live catalog using `policy.model_preferences` glob patterns, dropping deprecated, absent and `retired_model_ids` entries and refusing a stale catalog (`catalog_max_age_seconds`). OpenRouter additionally requires the model to be in `openrouter.approved_models`. CLI runners resolve an alias from `policy.cli_models` (Claude defaults to the haiku, sonnet and opus aliases).

Cost per attempt comes from catalog pricing times `policy.cost_estimation` tokens, else `policy.runner_costs[runner][class]`, else unknown. Today only OpenRouter listings publish prices, so configure `runner_costs` for Gemini, Groq, Kimi and the CLI runners. Candidates sort by known cost, unknown last, then runner name, then model. OpenRouter is fallback only for authoring: it is chosen only when no primary candidate exists, and the result role becomes `fallback`.

`Selection` fields: `ok`, `tier`, `role`, `runner`, `provider`, `family`, `model`, `capability_class`, `estimated_cost_usd`, `cost_source`, `independent_family`, `blocking`, `reasons`, `warnings`, `rejected` (each `{runner, reason}`), `candidates`.

## Reviewer selection

```python
select_reviewer(author, classification, certified_runners, catalog, policy, *,
                breaker=None, now=None) -> Selection
```

The review tier is `classification.review_tier`, raised to 2 when the author worked at Tier 2. A forged low author tier cannot lower it. The author runner is excluded when independence is required. A reviewer from a different model family is tried first. For Tier 2 or high risk reviews a different family is mandatory; otherwise the same family is accepted with a warning. When no qualified reviewer exists for Tier 2 or high risk work, `blocking` is true and the unit cannot complete. Families come from the runner profile, or for hosting providers (Groq, OpenRouter) from the model id; an unrecognised id yields a unique `unknown:` family.

## Escalation

```python
EscalationController(unit, classification, *, tier_costs, clock, policy=None, breaker=None)
controller.next_action() -> {"action", "tier", "kind", "reason", "attempt"}
controller.record(AttemptOutcome(passed, objective_failure, cost_usd, ...)) -> next action
controller.to_record() -> dict
```

Actions are `attempt`, `repair`, `escalate`, `stop` and `done`. The controller starts at `chosen_tier` and allows `budget.attempts` per tier (capped at 2: one implementation plus one repair). It escalates one tier only after an objective failure, and never past `maximum_tier`. When another attempt at the current tier would push that tier's spend above one attempt at the next tier, it escalates instead of retrying cheaply. It stops when the cost or time budget (the tighter of the unit budget and the policy ceilings) is spent, or when the next attempt's projected cost does not fit. Escalation records carry `from_tier`, `to_tier`, `reason`, `attempts`, `elapsed_seconds`, `spent_usd`, aggregated `usage`, `target_runner` and `target_model` (filled from the first attempt at the new tier).

## Circuit breaker

`CircuitBreaker(failure_threshold, cooldown_seconds, clock)` or `CircuitBreaker.from_policy(policy, clock)`. After `failure_threshold` consecutive failed attempts a provider's breaker opens for `cooldown_seconds`; it then goes half open, where one failure reopens it and one success closes it. Provider names are normalised. Pass the breaker to `select_runner` to skip open providers, and to `EscalationController` to record outcomes automatically.

## Where the runtime applies routing

| Step | Call |
| :- | :- |
| `create_run` | `validate_work_unit` for every unit, `validate_repository_policy` for the policy |
| assignment (`Conductor.assign_unit`) | `classify`, `EscalationController.next_action`, `select_runner`; the routing decision is validated and stored on the unit |
| handoff | `validate_handoff(handoff, unit)` before the unit may enter `verifying` |
| verification | `classify` again with the actual changed files and line counts |
| review (`Conductor.select_reviewer`, `record_review`) | `select_reviewer` and `review_satisfies` against the re-derived classification |
| merge queue enqueue | `classify` with the actual diff at the branch tip and `review_satisfies` again, so a review that no longer meets the review tier blocks the merge |
| after each attempt | `EscalationController.record`; its record is persisted and replayed after a restart |

A high risk unit therefore reaches the merge queue only with a Tier 2 review from a runner outside the author's model family.
