# Orchestrator runtime

Standard library only (Python 3.11). This document covers the execution side
of `openclaw_ecc_orchestrator`: durable run state, worktrees, runner
supervision, quality gates, conflict prediction, the merge queue, and the
OpenClaw boundary. The event and approval contract is in
[openclaw-events.md](openclaw-events.md).

## Module map

| Module | Responsibility |
| :- | :- |
| `runs.dag` | plan validation: unique ids, known dependencies, cycle report, topological layers, plan sha256 |
| `runs.store` | file backed run store: snapshot, per run lock, append only event log |
| `runs.fsutil` | atomic JSON write, durable JSONL append, flock based `FileLock` |
| `runs.state` | unit state machine and transition table |
| `runs.manager` | `RunManager`: leases, transitions, assignment, handoffs, budgets, resume |
| `runs.liveness` | pid liveness with zombie and pid reuse detection (Linux `/proc`) |
| `runs.conductor` | `Conductor` facade tying one unit's lifecycle together |
| `runs.envelope` | the boundary result envelope |
| `worktrees.manager` | one branch plus one worktree per unit, root validation, safe cleanup |
| `worktrees.git` | argument list git wrapper |
| `process.supervisor` | runner processes: env allowlist, streaming, redaction, timeout, cancel |
| `process.redact` | secret redaction for text and nested payloads |
| `gates.runner` | acceptance commands plus required checks, structured verification |
| `merge_queue.conflicts` | overlap prediction from declared scope and from changed files; actual diff stats; out of scope check |
| `merge_queue.dispatch` | dispatcher that never runs overlapping units in parallel |
| `merge_queue.queue` | sequential local merge queue; resolves approvals only through the broker |
| `tasks.scope` | repository relative path safety and scope glob matching |
| `worktrees.guard` | snapshot and diff of git state shared by all worktrees |
| `plugin.events` | versioned event emitter, JSONL and memory sinks |
| `plugin.approvals` | `ApprovalBroker`: bound approval requests and decision validation |
| `plugin.inbox` | file based decision inbox for non Python plugins |
| `plugin.summary` | `required_user_actions` summarizer |

## Invariants and where they are enforced

| Invariant | Enforcement |
| :- | :- |
| one unit = one branch + one worktree | `WorktreeManager.create` refuses an existing branch, path or marker |
| parallel workers never share a checkout | per unit worktree; `Dispatcher` defers overlapping scopes and re-checks under the run lock |
| no silent takeover | `assign_unit` refuses a unit owned by someone else; only `reassign_unit` changes owner and it records a handoff that the unit references |
| worktrees outside every repository and outside `.openclaw` | `validate_root` resolves symlinks, rejects the repo, any enclosing repo, and any `.openclaw` or `.git` component |
| cleanup never loses unique work | `cleanup` refuses uncommitted changes, untracked files, a detached or moved HEAD, and commits unreachable from the target branch or any remote, unless those commits are archived to a verified bundle first |
| gates block merges, warnings do not | a unit passes only when every required gate ran and exited 0; missing checks and refused commands fail |
| merges are sequential | one queue, one file lock, FIFO, compare and swap on the target ref |
| never push | no code path calls push; tests assert the remote ref is unchanged |
| approvals bound and single use | the merge queue accepts only a broker request id; `ApprovalBroker.consume` requires status `approved`, an unexpired request (injected clock), matching run, unit, action, session, plan hash and verified head, and marks it `consumed` |
| merged code is the reviewed code | verification records `head`; the review and the approval name that sha; the branch tip must equal it at enqueue and at merge |
| only declared files change | changed files outside `scope.files` fail verification and block enqueue |
| review matches the actual risk | the queue re-classifies with the actual changed files and line counts; the review must meet the resulting review tier and model family independence |
| plans and handoffs are validated | `create_run` runs `validate_work_unit` on every unit and `validate_repository_policy` on the policy; `verifying` requires a handoff that passed `validate_handoff` for the current attempt |
| runners do not touch shared git state | the conductor snapshots shared refs, config and hooks before a runner starts and diffs after it exits; any change blocks the unit |
| restart and conductor change | leases with expiry renewed while runners work, snapshot plus log replay, `resume` marks dead `assigned` or `running` units `interrupted` |

## Run store

```
<store root>/<run_id>/run.json      snapshot, replaced atomically
<store root>/<run_id>/events.jsonl  append only log, seq 1, 2, 3, ...
<store root>/<run_id>/.lock         per run flock
```

Every mutation takes the run lock, appends its events (each fsynced), then
atomically replaces the snapshot (temp file in the same directory, fsync,
`os.replace`, directory fsync) with `last_event_seq` set. Unit events carry
the full unit record, so loading is: read the snapshot, then replay any
events newer than its `last_event_seq`. If the snapshot is missing or
corrupt, the whole run is rebuilt from the `run.created` event onward. A torn
last log line is skipped on read and repaired on the next append.

## Unit state machine

```
pending -> ready -> assigned -> running -> verifying -> reviewing -> queued_for_merge -> merged
```

| From | Allowed targets |
| :- | :- |
| pending | ready, blocked, cancelled |
| ready | assigned, blocked, cancelled, needs_user |
| assigned | running, interrupted, failed, cancelled, needs_user |
| running | verifying, failed, cancelled, needs_user, interrupted |
| verifying | reviewing, failed, cancelled, needs_user, interrupted |
| reviewing | queued_for_merge, ready (changes requested), failed, cancelled, needs_user, interrupted |
| queued_for_merge | merged, failed, blocked, cancelled, needs_user |
| failed | ready, cancelled, needs_user |
| blocked | ready, cancelled, needs_user |
| needs_user | ready, cancelled |
| interrupted | ready, cancelled, needs_user |
| merged, cancelled | terminal |

`assigned` is reachable only through `assign_unit` or `reassign_unit`.
Pending units become ready automatically once all dependencies are merged.

## Leases, handoffs and resume

* A run has one lease: `{conductor_id, acquired_at, expires_at}`. Every
  mutating call names its conductor and fails unless that conductor holds an
  unexpired lease. Time comes from the injected clock.
* `transfer_lease(from, to)` is the explicit conductor handoff and records a
  `conductor` handoff entry.
* While a runner works, `Conductor.wait_unit` renews the lease every
  `lease_renew_interval` seconds of wall time and whenever half of the lease
  has passed on the injected clock. A lease that merely expired and was not
  claimed is taken back. If another conductor holds it, `surrender_unit`
  (allowed without the lease, only for the conductor recorded on the unit's
  process) moves the unit to `needs_user` with reason `lease_lost`, keeps the
  pid in the process record, and returns a `verify_worker_process` action.
* `wait_unit(timeout=...)` returning "still running", and a transient failure
  (`OSError`, `StoreError`, `TimeoutError`) while processing the result, keep
  the process handle, so the call can simply be repeated. Completed steps
  (exit record, git state check, usage) are not repeated.
* When the runner exits, `record_process_exit` marks the process record
  `finished` before anything else happens.
* `resume(run_id, conductor_id)` succeeds when the lease is free, expired, or
  already held by the caller. It records a `conductor` handoff when the holder
  changes. It examines only units in `assigned` or `running`: a unit whose
  process record is missing, finished, or dead becomes `interrupted`
  (reason `process_gone`, action `reassign_interrupted_unit`); a unit whose
  process identity cannot be verified becomes `needs_user` (reason
  `liveness_unverified`, action `verify_worker_process`); a live one is left
  alone. `verifying`, `reviewing` and `queued_for_merge` are past the runner
  and are never touched.
* `reassign_unit(unit, new_owner, reason)` works on stopped units only
  (`interrupted`, `failed`, `needs_user`, `blocked`, `ready`). It writes a
  `unit` handoff with the previous owner, state, process and workspace, sets
  `handoff_ref` on the unit, and assigns the new owner. Reviews by any past
  or present owner of the unit are refused as not independent.
* Budgets use the work unit schema names. `budget.attempts` is per tier and
  is checked on every assignment against `attempts_by_tier`;
  `budget.minutes` and `budget.maximum_cost_usd` are per unit and are checked
  on every `record_usage` (and cost again on assignment). Exhaustion moves the
  unit to `needs_user` with reason `budget_exhausted`. Runs persisted before
  the rename may carry `max_attempts` (a total cap) and `max_minutes`; these
  are still read as deprecated aliases, but new plans using them fail
  validation.

## Plan validation, handoffs and routing

* `create_run` rejects the plan unless every unit passes
  `schemas.validate_work_unit` (unknown schema version, path traversal or
  absolute paths in `scope.files`, negative budgets, inverted tiers and so
  on), and, when the conductor has a repository policy, unless the policy
  passes `schemas.validate_repository_policy`. The policy is stored with the
  run. Two concurrent `create_run` calls with the same run id produce one
  winner; the others get an envelope with a failed `run_id_unique` check.
* Runners receive `ECC_HANDOFF_PATH` and `ECC_UNIT_ID`. After the runner
  exits, `record_handoff` validates the handoff with
  `schemas.validate_handoff(handoff, unit)`; it must have status `succeeded`
  and its `commit.sha` must name the worktree head. A handoff that claims
  success with a failing command, lists files outside the scope, or is
  missing moves the unit to `failed`; a runner asking for input moves it to
  `needs_user`. `verifying` is refused without a valid handoff for the
  current attempt.
* `Conductor.assign_unit` classifies the unit (`routing.classify`), asks the
  unit's `EscalationController` for the attempt tier, picks the cheapest
  eligible certified runner with `select_runner`, and records the routing
  decision (a validated routing decision document plus runner, model,
  family, tier, risk and review tier) on the unit. Assignments through the
  plain dispatcher record the classification with no runner. Each finished
  attempt is fed back to the controller and its record is persisted, so a
  new conductor rebuilds the controller after a restart. A `stop` from the
  controller moves the unit to `needs_user` (`escalation_stopped`).
* `Conductor.select_reviewer` re-classifies with the verified diff and calls
  `routing.select_reviewer` with the author's routing record. When no
  qualified reviewer exists for Tier 2 or high risk work, the unit moves to
  `needs_user` with reason `no_qualified_reviewer`.

## Worktrees

```
<root>/<repo name>-<hash>/<run_id>/<unit_id>             worktree
<root>/<repo name>-<hash>/<run_id>/<unit_id>.owner.json  ownership marker
<root>/<repo name>-<hash>/_archive/*.bundle              archived unique commits
```

The operator configures `<root>` (for example `~/agent-worktrees`). The
marker sits beside the worktree so it never appears as an untracked file.
Branches are named `ecc/<run_id>/<unit_id>`. Run and unit ids must match
`^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$`, may not start with a dot, and may not
contain `..`; `create_run` rejects plans with other unit ids.

Cleanup order: inspect (read only), optionally bundle the unique commits and
verify the bundle, remove the worktree without force, delete the branch,
delete the marker. The envelope's `rollback_checkpoint` names the bundle and
a restore command (a git fetch from the bundle into the branch). Every
mutating call accepts `dry_run=True` and then only reports its plan.

## Runner supervision

* argv lists only; each runner gets its own session and process group.
* Environment: only `PATH`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TZ`, `TMPDIR` plus
  names the caller allowlists, plus explicit `extra_env` values. Unrelated
  credentials in the parent environment are never inherited.
* Values of allowlisted variables whose names look secret are added to the
  redactor, so a runner echoing its token cannot leak it.
* stdout and stderr are streamed line by line, redacted, then passed to the
  callback and appended to the log file.
* Hard timeout and cooperative cancel both send SIGTERM to the group and
  SIGKILL after the grace period. When the main child exits, leftover group
  members are killed while the child is still an unreaped zombie, so the
  group id cannot be recycled underneath us.
* The exit record (`status.json`) has argv (redacted), pid, times, exit code,
  signal, timeout and cancel flags, and line counts.

## Quality gates

Acceptance commands and required check commands arrive as strings. They are
split with `shlex.split` and executed without a shell; shell operator tokens
(`&&`, `|`, `;`, redirections) are rejected as invalid. Protected command
globs (push, hard reset, force flags, recursive delete, sudo, network tools,
publish and deploy commands, plus any configured globs) are refused before
execution. Each gate records argv, status, exit code, duration, timeout flag
and a truncated, redacted output tail. `passed` is true only when at least one
gate ran and every gate passed.

## Conflicts, dispatch and the merge queue

* Before dispatch: declared `scope.files` entries are compared conservatively
  (literal paths, directory prefixes, globs). The dispatcher never assigns a
  unit whose scope overlaps an active unit (`assigned` through
  `queued_for_merge`); the check is repeated under the run lock. A unit with
  an empty or missing `scope.files` means the whole repository: it overlaps
  every unit, so it only runs alone.
* After work: changed files and line counts come from a numstat `git diff`
  between the merge base and the unit branch; overlapping sets are reported.
  Files outside the declared `scope.files` globs (`*` never crosses `/`, a
  literal directory covers its subtree) fail verification with an
  `attention.required` event (`out_of_scope_changes`); they never pass as a
  warning. Verification records `head`, `changed_files`, `diff_stats`,
  `out_of_scope` and the re-classification.
* Review: `record_review` accepts an approving review only when it names the
  verified `head`, the branch tip still equals that head, and it names the
  `runner`, `model` and `tier` that produced it. The unit is re-classified
  with the actual diff; the review must reach the resulting review tier and,
  for Tier 2 or high risk work, come from a runner outside the author's model
  family (`routing.selection.review_satisfies`, the rule `select_reviewer`
  applies). Only then is a merge approval requested, bound to that head.
* The queue (constructed with the `ApprovalBroker`) accepts a unit only when
  every check passes: verification passed for that unit with a valid handoff
  and no out of scope files; the branch tip equals the verified head; an
  independent approving review names the same head and satisfies the review
  tier and family independence derived again from the actual changed files;
  and the approval request id resolves in the broker to an approved,
  unexpired request bound to the run, unit, `merge`, session, plan hash and
  head. The request is consumed on enqueue. Approval records handed in by a
  caller are never trusted; only their request id is read. Refusals emit
  `attention.required` (`out_of_scope_changes`, `review_insufficient`).
* Processing (one item at a time, under a lock): refuse if the branch tip
  differs from the verified head; predict the merge with git merge-tree (write-tree mode), falling
  back to a throwaway worktree; merge for real in a throwaway detached
  worktree under the scratch root; re-run gates on the merged tree; then
  fast forward the target locally. If the target is checked out, the fast
  forward runs in that checkout (which must be clean); otherwise the ref is
  updated with compare and swap. Nothing is pushed.

## Conductor facade

`Conductor` wires the pieces: `create_run` pins the base commit and
validates the plan, `assign_unit` routes and assigns, `start_unit` snapshots
shared git state, creates the worktree and starts the runner, `wait_unit`
keeps the lease, records the exit, checks shared git state, records usage,
validates the handoff and runs verification, `cancel_unit` cancels
cooperatively, `select_reviewer` routes the review, `record_review` enforces
independence, head binding and the review tier and requests a bound
approval, `enqueue_merge` and `process_merge_queue` drive the queue and
update unit state, and `required_user_actions` summarizes what the operator
must do. `tests/test_resume_integration.py` drives a three unit plan through
success, failure, cancellation, reassignment, conflict, budget exhaustion,
and a restart with a conductor change.

Shared git state: when `worktrees.guard` is importable (it is the default
`git_state_guard`; pass `git_state_guard=None` to disable or a pair of
callables to replace it), `start_unit` takes a snapshot before the runner
starts and the completion step diffs it after the runner exits. Allowed
changes are the unit's own branch, the branches of the run's other units
(the conductor creates them and their runners commit to them in parallel),
and target branch moves made by this conductor's own merges (the baseline of
every in flight unit is advanced only when it still shows the pre merge
commit). Anything else moves the unit to `needs_user` with reason
`shared_git_state_changed` and an `attention.required` event. Tampering with
another unit's branch is caught later: the queue refuses a tip that differs
from that unit's verified head.

## Known limitations

* Process liveness and pid reuse detection rely on Linux `/proc`; elsewhere
  only `kill(pid, 0)` is used.
* Locks are advisory `flock` locks and assume a local filesystem.
* A merge queue dry run may write git objects (never refs) through merge-tree.
* Scope overlap matching is intentionally conservative and can defer units
  that would not actually collide.
* Protected command globs are a guard rail, not a sandbox; runners and gates
  still execute with the operator's filesystem permissions.
* The shared git state baseline lives in the conductor's memory. Branches or
  merges made by another process (another run's conductor on the same
  repository, or a person) while a runner works are reported as violations,
  which fails closed but may need an operator to confirm and resume.

## Hardening: process, worktrees, gates and runners

This section covers `process/`, `worktrees/`, `gates/`, `runners/` and
`handoffs/redaction.py`. Where it differs from the sections above, this
section is current.

### Process monitor

* Exit detection prefers `os.waitid` with `WNOWAIT`, so the main child stays
  an unreaped zombie while leftover group members are killed. When `waitid`
  is missing or raises (`NotImplementedError`, `AttributeError`, `OSError`),
  the monitor falls back to kqueue `EVFILT_PROC` with `NOTE_EXIT` (macOS and
  BSD), and then to `Popen.poll()`.
* Any exception in the monitor thread kills the process group, reaps the
  child and finishes the handle with `error` set to `monitor_failed: ...`
  (redacted). `wait()` also notices a monitor thread that died without a
  result, so it never blocks forever.
* `ProcessHandle.wait(timeout=None)` accepts a timeout in seconds and raises
  `TimeoutError` when the process is still running.

### Redaction

There is one engine, `handoffs.redaction`; `process.redact` re-exports it.

| Covered | How |
| :- | :- |
| `sk-`, `sk-ant-`, `sk-or-`, `sk-proj-`, `gsk_`, `AIza`, `ghp_`, `gho_`, `github_pat_`, `glpat-`, `xox*`, `AKIA`, `ASIA`, `hf_`, JWTs, bearer credentials | prefix patterns |
| long mixed case tokens | entropy rule (hex digests up to 64 characters are kept) |
| `NAME=value`, `name: value`, JSON pairs with a secret looking name; `Authorization` headers; URL passwords | value rules that keep the name and mask the value |
| private key blocks | masked whole inside one text; `StreamRedactor` masks every line from BEGIN through END when a key arrives line by line |
| argv | `redact_argv` masks the value of token, password, passphrase, api key, secret, auth, cookie and similar flags in both the separate and the equals form, and `NAME=value` pairs with a secret looking name |
| literal values | `Redactor.add_secret`; the supervisor registers every `extra_env` value, whatever its variable name, for that process only |

Log lines, the line callback, `status.json`, the returned result and error
messages all pass through the process redactor. Errors never echo a value.

### Orchestrator git

`worktrees.git.run_git` is the only way the orchestrator runs git. It passes
a minimal environment (`PATH`, `HOME`, `LANG`, `TMPDIR`, `USER`, `LOGNAME`,
names allowlisted through `env_allow` or `set_git_env_allow`, never a
`GIT_*` name) plus `LC_ALL=C`, `GIT_CONFIG_NOSYSTEM=1`,
`GIT_TERMINAL_PROMPT=0` and `GIT_OPTIONAL_LOCKS=0`. Every command starts with
`-c` overrides: `core.fsmonitor=false`, `core.hooksPath=/dev/null`, an empty
`core.sshCommand`, `credential.helper` and `core.askPass`, and
`protocol.ext.allow=never`. A worker that plants an fsmonitor or hook in
shared config therefore cannot run code inside orchestrator git calls.

### Shared git state guard

A unit worktree shares refs, config, hooks and objects with the repository.
`worktrees.guard` detects worker changes to that shared state:

```python
snapshot_shared_git_state(repo_path) -> dict
diff_shared_git_state(before, after, allow_refs=()) -> list[str]
```

The snapshot records the main worktree HEAD, every ref under `refs/heads`,
`refs/tags`, `refs/remotes` and `refs/replace`, digests of `packed-refs`,
the shared `config` and `config.worktree`, `info/attributes`, `info/exclude`
and `objects/info/alternates`, digests of risky config keys (for example
`core.fsmonitor`, `core.hooksPath`, `alias.*`, `include.*`, `includeIf.*`,
`credential.*`, `remote.*.url`, `url.*.insteadOf`, `filter.*`,
`diff.external`, `gpg.program`), and a listing digest of `hooks/`. The diff
returns readable lines such as `ref moved: refs/heads/main a1b2 -> c3d4`,
`risky config added: config:core.fsmonitor` or `hook added: hooks/post-merge`.
Config values are never echoed. `allow_refs` takes full ref names, globs, or
branch names (the unit branch is usually the only one). Take the snapshot
before the runner starts and diff after it exits; any line is a violation.
Worker set filter drivers would run during a later checkout of the unit
branch, so a non empty diff must stop the unit before its branch is merged.

### Cleanup of ignored files and detached HEAD work

`WorktreeManager.cleanup` also treats as unique work:

* gitignored files (from `git status` with the ignored option), except regenerable caches
  such as `__pycache__`, `*.pyc`, `.pytest_cache`, `.mypy_cache` and
  `.ruff_cache`. Without `archive=True` they block cleanup; with it they are
  written to an `ignored.tar.gz` tarball named after the run, unit and tip beside the bundle,
  verified, and a `tar` restore command is returned.
* commits the worktree HEAD visited (current detached HEAD and HEAD reflog)
  that no branch, tag or remote reaches and that were never the unit branch
  tip. They block cleanup; with `archive=True` they go into the bundle under
  `refs/ecc-archive/<run>/<unit>/detached-head` (or `orphan-<sha>`), and the
  temporary refs are deleted again afterwards.

### Gate command policy and environment

`gates.runner.check_command_policy(argv, extra_globs, allow_wrappers)` is
allowlist first and returns a refusal reason or None:

| Refused | Detail |
| :- | :- |
| shells and wrappers | `sh`, `bash`, `zsh`, `dash`, `fish`, `env`, `xargs`, `eval`, `exec`, `sudo`, `nohup`, `time`, `nice`, `command`, `busybox`, `timeout`, `xcrun`, `sandbox-exec` and similar, matched case insensitively on the basename, unless `repo_checks["allow_wrappers"]` lists them; the inner command of an allowlisted wrapper is checked again |
| shell control sequences in any argument | `;`, `&&`, `||`, `|`, backticks, `$(`, `${`, redirections, newlines; the inline program of `python -c`, `node -e` and similar is code and exempt |
| git | only read only subcommands run; `push`, `remote`, `config`, `update-ref`, `credential`, unknown subcommands (possible aliases) and any `-c` outside a small safe set, such as `alias.*`, are refused |
| recursive delete | `rm` with any recursive spelling (`-r`, `-R`, combined short flags, the long recursive option or its prefixes); `find` with delete or exec actions |
| protected globs | the default list plus configured globs, as a final guard rail |

Gates inherit `HOME` (plus `env_allow`) and get `TMPDIR` set to a fresh per
unit directory under `tmp_root` (default: the system temp dir), removed after
the gates finish.

### Runner and probe environment

Certification probes run each step with `runners.base.minimal_env`: `PATH`,
`HOME`, `USER`, `LANG`, `TERM`, `TMPDIR` plus the adapter's declared
`credential_env` names. Each step gets its own process group; on timeout the
group receives SIGTERM, then SIGKILL after `KILL_GRACE_SECONDS`. Claude and
Codex adapters declare `HOME` by default because on macOS both CLIs find
their login through it; pass `adapter.env_allow()` as the supervisor
allowlist for unit runners. Details are in [runners.md](runners.md).
