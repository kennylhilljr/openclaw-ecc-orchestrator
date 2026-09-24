# Operator CLI

One command drives the runtime from a shell:

```
python3 -m openclaw_ecc_orchestrator <subcommand> [options]
ecc-orchestrator <subcommand> [options]
```

`ecc-orchestrator` is the console script declared in `pyproject.toml` under
`[project.scripts]`. Both forms call `openclaw_ecc_orchestrator.cli.main`.
The CLI uses the standard library only and builds everything on the public
runtime APIs (`RunManager`, `Conductor`, `MergeQueue`, `ApprovalBroker`,
`DecisionInbox`, `JsonlEventSink`, `WorktreeManager`, `run_gates`,
`summarize_required_user_actions`).

**Option names in this document.** Every option is written here without its
two leading hyphens. On the command line each one takes the usual long
option prefix: the option shown as `json` is typed as two hyphens followed by
`json`, `dry-run` as two hyphens followed by `dry-run`, and so on. The help
option of any subcommand lists them in full.

## Subcommands

| Subcommand | Kind | What it does |
| :- | :- | :- |
| `create-run` | gated | validate a plan (`plan`, `run-id`) and create a run pinned to the target branch tip |
| `status` | read only | runs, or one run's units with state, tier, runner, attempts, budget use, pending approvals and `required_user_actions` |
| `dispatch` | gated | route, assign, create the worktree, run the runner, validate the handoff and verify; the unit ends in `reviewing` on success |
| `verify` | gated | re-run the gates in the unit worktree and check head, cleanliness and scope; changes no run state |
| `review` | gated | record an independent review; an approving review requests a merge approval bound to the verified head |
| `merge` | gated, destructive | enqueue the reviewed unit (consuming its merge approval) and process the queue until it is merged |
| `cleanup` | gated, destructive when it archives unique work | remove a stopped unit's worktree and branch |
| `approvals list` | read only | pending approval requests |
| `approvals approve` | gated | approve a request through the decision inbox |
| `approvals reject` | gated | reject a request through the decision inbox |
| `events tail` | read only | print the event log; `since-seq`, `run-id`, `limit`, `follow` with `interval` and `max-polls` |
| `doctor` | read only | local self checks |
| `review-plan` | writes review records only | print a plan and its hash; with `approve` write a review record |

Gated subcommands are the mutating ones. Each accepts `dry-run`, `json`,
`verbose`, `backup-dir <path>`, `yes` and `review-id <id>`.

## Output and exit codes

With `json` every command prints one envelope:

```json
{
  "ok": true,
  "operation": "dispatch",
  "changed": true,
  "checks": [{"name": "plan_hash_stable", "ok": true, "detail": "..."}],
  "warnings": [],
  "required_user_actions": [],
  "rollback_checkpoint": null,
  "data": {"plan": {}, "plan_sha256": "..."}
}
```

Without `json` a concise human summary is printed: a status line,
operation specific lines, failed checks, warnings, required user actions and
the rollback checkpoint. `verbose` adds every check and the data, and streams
runner output lines to stderr during `dispatch`. `events tail` with `follow`
and `json` prints one event per line instead of an envelope.

| Exit code | Meaning |
| :- | :- |
| 0 | ok |
| 1 | the operation failed (a check failed, a plan changed, the operator declined) |
| 2 | usage error: bad options, invalid identifiers, path traversal, a bad config file, `yes` without `review-id` |
| 3 | an approval or another user action is required; `required_user_actions` says which |

A read only command that succeeds exits 0 even when it reports required user
actions (for example `status` listing a pending approval).

**Redaction.** Every printed string passes through the runtime's redaction
engine (`handoffs.redaction`): envelopes through `redact_obj`, human lines,
prompts, progress lines and usage errors through `Redactor.redact`. Values
stored under secret looking keys are masked, which includes `session_id`
inside nested runtime records; listings therefore show the session of an
approval request as `requesting_session`. Plan hashes are computed over the
unredacted plan, so a printed plan is not guaranteed to hash to the printed
value when it contained something secret looking.

## Configuration

Sources, highest priority first: options, the JSON file named by `config`,
runtime defaults. Paths in the config file are resolved relative to the
file's directory; paths given as options relative to the working directory.

| Config key | Option | Default |
| :- | :- | :- |
| `state_dir` | `state-dir` | `$XDG_STATE_HOME/openclaw-ecc-orchestrator`, else `~/.local/state/openclaw-ecc-orchestrator` |
| `worktree_root` | `worktree-root` | `$XDG_DATA_HOME/openclaw-ecc-orchestrator/worktrees`, else `~/.local/share/openclaw-ecc-orchestrator/worktrees` |
| `repo` | `repo` | the working directory |
| `target_branch` | `target-branch` | `main` |
| `policy_file` | `policy-file` | `<repo>/.orchestration/config.yaml` when it exists, else no policy |
| `event_log` | `event-log` | `<state_dir>/openclaw-events.jsonl` |
| `decision_inbox` | `decision-inbox` | `<state_dir>/decisions` |
| `certifications` | `certifications` | none (no runner is certified, so assignment fails closed) |
| `catalog` | | none (API runners then cannot resolve a model) |
| `repo_checks` | | none: `{"required": [...], "commands": {"name": "command"}}` |
| `conductor_id` | | `ecc-cli` |
| `lease_ttl`, `runner_timeout`, `gate_timeout` | | 300, 3600, 600 seconds |
| `approval_ttl`, `review_ttl` | | 3600 seconds each (merge or cleanup approvals; review records) |
| `tier_costs` | | none: `{"0": 0.05, "1": 0.5, "2": 2.0}` for the escalation controller |

Defaults are derived at run time from `HOME` and the XDG variables of the
invoking environment; no personal path is built in. Unknown config keys are a
usage error.

Other state lives under the state dir: `runs/` (run store), `approvals.json`
(broker), `merge-queue.json`, `reviews/` (review records), `logs/` (runner
and gate logs). The merge queue scratch root is `<worktree_root>/_merge-scratch`.

**Path rules.** Every path, from an option or the config file, is refused
when it contains a `..` component or a NUL byte, before anything resolves
it. The state dir, event log and decision inbox must not be inside the
repository, a `.git` directory, any `.openclaw` directory or `~/.openclaw`.
The worktree root must pass `worktrees.manager.validate_root` (outside every
repository, no `.openclaw` or `.git` component). The backup dir follows the
state dir rules. Run, unit, review and request ids must match
`^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$`, must not start with a dot and must not
contain `..`.

**Policy file limitation.** `.orchestration/config.yaml` is parsed with
`schemas.load_repository_policy`, which reads JSON only. Write it as JSON
compatible YAML (any JSON document is valid YAML 1.2). Real YAML syntax such
as `key: value` lines is rejected (`policy_valid` fails). The policy is
validated at `create-run` and stored with the run; later commands use the
stored policy, never the file. Every check the policy lists in
`required_checks` is added to the configured `repo_checks`, so a config
cannot drop a policy check; a required check without a configured command
fails the gates.

**Certifications.** `certifications` names a JSON list (or
`{"records": [...]}`) of runner certification records. Only valid,
unexpired, `certified` records for known runners survive
(`runners.registry.certified_runners`).

## Plans and hashes

Every gated subcommand first builds a plan: a deterministic JSON document
(no timestamps) that records the operation, its arguments, the relevant
state (unit state, verified head, branch and target tips, approval status,
queue contents, the predicted routing and the exact runner argv), the
resolved context (state dir, repository, target branch, conductor id, event
log) and the backup dir. The plan hash is the SHA-256 of its canonical JSON
(`plugin.approvals.plan_digest`: sorted keys, compact separators, ASCII).

* `dry-run` prints the plan and hash and changes nothing: no state file, no
  lock file, no git ref, no worktree, no backup. The tests compare the state
  dir, the worktree root, every git ref, the worktree list and HEAD before
  and after each gated subcommand's dry run. The merge queue's own dry run
  may write git objects through merge-tree; the CLI dry run does not call it.
* Before executing, the command takes the run lease where it changes run
  state (`dispatch`, `review`, `merge`), rebuilds the plan and refuses with
  `plan_hash_stable` failed if the hash differs from the one that was
  confirmed or approved.

## The confirmation gate

A gated subcommand executes only through one of these paths.

| Plan | Allowed through |
| :- | :- |
| non destructive | an interactive confirmation on a TTY (`y` or `yes`), or `yes` with `review-id` naming a valid review record |
| destructive | an approval admitted through the `ApprovalBroker`, or (cleanup only) an interactive confirmation where the operator types the word `yes`. The `yes` option is ignored with a warning |

Without `yes`, on a TTY the plan and its hash are printed to stderr and the
operator is asked. Not on a TTY the command refuses: exit 3, `ok` false,
`operator_confirmed` failed and a `confirm_plan` required action carrying the
plan hash. `yes` without `review-id` (and `review-id` without `yes`) is a
usage error.

**Destructive plans.** Every merge is destructive. A cleanup is destructive
when it archives unique work (unmerged commits, detached HEAD commits or
ignored files go into a bundle or tarball) before removing the worktree and
branch. A cleanup with unique work and no `archive` option is refused
outright with an `unmerged_work` action.

* `merge` proceeds only when the unit's merge approval, requested by
  `review`, is approved, unexpired and bound to the run, unit, `merge`
  action, run plan hash, review session and verified head
  (`ApprovalBroker.usable`); the queue consumes it on enqueue. Otherwise it
  exits 3 with an `approve` (pending), `reverify_and_reapprove` (rejected,
  expired or consumed) or `record_review` (no approving review) action. An
  interactive yes cannot replace the broker approval, because the queue
  accepts nothing else.
* A destructive `cleanup` on a TTY asks for the word `yes`. Not on a TTY it
  needs `session`: the first call creates a broker request for action
  `cleanup` bound to the run, unit, cleanup plan hash, session and branch tip
  and exits 3 with an `approve` action naming the request id. After
  `approvals approve`, the same cleanup with `approval-request <id>` and the
  same `session` consumes the approval (`ApprovalBroker.consume`) and runs.
  A changed plan changes the hash, so the approval no longer binds.

## Review records

`review-plan [approve operator <handle> session <id> [review-id <id>]] <subcommand> [args]`
builds the plan of the inner subcommand exactly as that subcommand would
(options such as `config` belong to the inner command; an end of options
marker before the inner subcommand is accepted). Without `approve` it prints
the plan and hash and writes nothing. With `approve` it writes
`<state_dir>/reviews/<review_id>.json`:

| Field | Value |
| :- | :- |
| `schema_version` | `"1.0"` |
| `review_id` | the record id (generated as `rev-<hex>` unless given) |
| `operation` | the gated operation, for example `dispatch` or `approvals.approve` |
| `plan_sha256` | SHA-256 of the exact plan |
| `destructive` | always `false` |
| `approved_by` | the `operator` handle |
| `session` | the `session` id |
| `approved_at`, `expires_at` | ISO 8601 UTC; `expires_at` is `approved_at` plus `review_ttl` |
| `plan` | the redacted plan, for audit |

Destructive plans are refused (`plan_not_destructive` failed, exit 1) and no
record is written. Using a record with `yes` checks: the file exists and was
not consumed, `schema_version` is `"1.0"`, `review_id` and `operation`
match, `plan_sha256` equals the recomputed plan hash, `destructive` is
exactly `false`, `approved_by` and `session` are valid handles, `approved_at`
is not in the future and the record has not expired. Any failure exits 3 with
a `review_plan` action. On success the record is moved atomically to
`reviews/consumed/` before execution, so it authorizes one run only.

## Operator identity

Identity is never inferred: not from `USER`, `LOGNAME`, git configuration or
the operating system. `approvals approve` and `approvals reject` require
`operator` and `session`; `review-plan approve` requires both too. Handles
must match `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$` (so an email address is
refused) and must not look like a secret.

## Approvals

`approvals approve <request_id>` builds the decision from the pending
request's binding (run, unit, action, plan hash) plus the operator's own
`session` and `operator`, writes it into the decision inbox (write to a
temporary name, then rename) and processes the inbox through
`DecisionInbox.process`, which resolves it with `ApprovalBroker.resolve`.
Every broker check applies: known request, still pending (so a replay fails
with `request_pending`), not expired, and run, unit, action, plan hash and
session equal to the request (a different session fails with
`binding_matches`, is audited and does not consume the request). The head
sha is bound by the runtime when the request is created and checked when the
approval is used. Other decision files waiting in the inbox are processed in
the same pass and reported as a warning. `approval.resolved` is emitted with
`decided_by`.

## Subcommand notes

* `create-run` requires `run-id` and `plan` (a JSON file holding
  `{"units": [...]}` or a list of work units; duplicate keys are refused).
  The plan pins the target tip as the base commit.
* `dispatch` works on a `ready` unit whose scope does not overlap an active
  unit. The plan predicts the routing the conductor will choose (classify,
  the escalation controller rebuilt from the persisted record,
  `select_runner` over the certified runners) and the runner command: the
  selected coding CLI adapter's `build_invocation` with a deterministic
  prompt, the model, the worktree and a writable run, plus the adapter's
  environment allowlist. API runners are not launched by the CLI (exit 3).
  If the actual assignment differs from the plan, the unit goes to
  `needs_user` with reason `routing_changed_since_plan`. `worker` overrides
  the owner id. Interrupting the command cancels the runner cooperatively and
  leaves the unit `failed` (retryable).
* **Test runner override.** `runner-command-json '<JSON argv>'` replaces the
  registry command, and is honored only together with `allow-test-runner`;
  without it the command is a usage error. The value must be a non empty
  JSON list of strings. It exists for tests and local rehearsals; the plan
  records `"source": "test_override"`, so a review record for a registry
  command never matches an overridden one.
* `verify` needs a unit in `verifying` or `reviewing`. It runs the gates
  with the same repository checks and command policy as the conductor,
  checks that the branch tip and worktree HEAD equal the verified head, that
  the worktree is clean and that the diff stays inside `scope.files`. It
  records nothing in the run.
* `review` requires `reviewer`, `runner`, `model`, `tier`, `verdict`,
  `head` and `session`; the conductor enforces independence, head binding,
  the review tier and model family independence for the actual diff. An
  approving review prints the merge approval request id.
* `merge` processes queue items ahead of the unit first (they were approved
  when enqueued) and reports them in `processed_before`. Nothing is pushed.
  The rollback checkpoint carries the previous target commit and the
  `update-ref` command that restores it.
* `cleanup` refuses units that still occupy a checkout (`assigned` through
  `queued_for_merge`). `archive` bundles unique work first and the rollback
  checkpoint names the bundle and the restore command.
* `status` without `run-id` lists runs with unit state counts.
* `doctor` checks the Python version (at least 3.11), git (at least 2.38,
  needed for merge-tree in write-tree mode), the repository, the state dir
  location and writability (without creating it), the worktree root, the
  policy file and the certification records. Bad locations are reported as
  failed checks (exit 1) instead of usage errors.

## Backups

`backup-dir <path>` copies, before execution, the run's store directory,
`approvals.json` and `merge-queue.json` into
`<path>/<operation>-<unix time>-<hex>/state/`, and records every
`refs/heads` ref in `manifest.json`. The envelope's `rollback_checkpoint`
gains a `backup` entry with the path, files and refs. Restoring is manual:
copy the files back and reset refs with `git update-ref`. The backup dir is
part of the plan, so a review record approves one specific backup location.

## Non interactive walkthrough

The end to end test drives this sequence against a real repository:

1. `create-run` with `run-id` and `plan`, after `review-plan approve` for the
   same arguments, run with `yes` and the returned `review-id`.
2. `dispatch` the ready unit the same way (the test uses the test runner
   override); the unit reaches `reviewing`.
3. `verify` the same way.
4. `review` the same way, naming the verified head from `status`; note the
   merge approval request id.
5. `approvals approve <request_id>` with `operator` and `session`, the same
   way.
6. `merge` with no confirmation option: the broker approval admits it.
7. `status` shows the unit `merged` and its dependents `ready`.
8. `cleanup` of the merged unit (not destructive) through a review record.

The event log then holds `run.created`, the unit's `unit.state_changed`
path from `ready` to `merged`, one `approval.requested`, one
`approval.resolved` and one `merge.completed` with `pushed` false.

## Leases and concurrency

All CLI invocations use one conductor id (`conductor_id`, default
`ecc-cli`). A command that changes run state takes the run lease with
`RunManager.acquire_lease`, which succeeds when the lease is free, expired or
already held by that id, and keeps it until it expires (`lease_ttl`).
Mutations are serialized by the run lock. A conductor with a different id
holding an unexpired lease blocks the CLI (exit 1, `lease_held`).

## Limitations

* No `resume`, `retry` or `reassign` subcommand yet: a unit left `running`
  by a crashed `dispatch`, or a `failed` unit, needs the Python API
  (`Conductor.resume`, `RunManager.transition`, `reassign_unit`).
* `dispatch` blocks until the runner exits and verification finishes.
* Review records are files in the state dir; anyone who can write there can
  forge one. They guard against accidental execution of an unreviewed plan,
  not against a local attacker, and they never admit destructive plans.
* The policy file must be JSON compatible YAML.
