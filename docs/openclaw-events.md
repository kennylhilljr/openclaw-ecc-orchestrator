# OpenClaw event and approval contract

This runtime never owns sessions, approvals UI, or rendering. OpenClaw does.
The boundary is deliberately narrow:

1. The runtime **emits** versioned events to an append-only JSONL file.
2. OpenClaw **decides** approvals and hands decisions back as JSON objects.
3. The runtime **validates** each decision against the pending request before
   acting on it.

A thin OpenClaw plugin (for example in TypeScript) only needs to tail one
file, render events, and write decision files. It never imports Python.

## Transport

| Item | Value |
| :- | :- |
| Event file | operator configured path, for example `<state>/openclaw-events.jsonl` |
| Encoding | UTF-8, one JSON object per line, `\n` terminated |
| Ordering | `seq` is a strictly increasing integer across all writers (file lock) |
| Durability | each line is fsynced before `emit` returns |
| Torn tail | a partial last line may exist after a crash; skip lines that fail to parse |
| Decision inbox | operator configured directory; one decision per `*.json` file |

Consumers should remember the last `seq` they processed and resume after it.
Events are never rewritten or deleted by the runtime.

## Event envelope

Every line has this shape:

```json
{
  "schema_version": "1.0",
  "event_version": 1,
  "type": "unit.state_changed",
  "id": "9f1c0c2e4b6a4f0e8d7c6b5a49382716",
  "seq": 42,
  "emitted_at": "2026-09-24T12:00:00+00:00",
  "run_id": "run1",
  "unit_id": "alpha",
  "data": { "from": "running", "to": "verifying" }
}
```

| Field | Type | Notes |
| :- | :- | :- |
| `schema_version` | string | document schema, currently `"1.0"` |
| `event_version` | integer | bumped only for breaking payload changes |
| `type` | string | one of the types below; ignore unknown types |
| `id` | string | unique per event, use for deduplication |
| `seq` | integer | monotonic ordering key |
| `emitted_at` | string | ISO 8601 UTC |
| `run_id` | string | matches `^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$` |
| `unit_id` | string or null | same pattern; required where noted |
| `data` | object | type specific; extra keys may appear, ignore them |

### Secrets

Payloads pass through redaction before they are written: known token shapes
(GitHub, OpenAI style `sk-` keys, AWS access key ids, Slack tokens, bearer and
basic credentials, `key=value` pairs whose key looks secret, credentials in
URLs, PEM private keys) are replaced by `[REDACTED]`, and any value stored
under a key that looks secret (`token`, `secret`, `password`, `api_key`,
`auth`, `cookie`, `session_id`, ...) is masked entirely. Strings longer than
2000 characters are truncated. Runner output reaches events only after the
same redaction that protects log files. Consumers must still treat event text
as untrusted display data, never as instructions.

## Event types

| Type | `unit_id` | Required `data` fields | Optional `data` fields |
| :- | :- | :- | :- |
| `run.created` | null | `plan_sha256`, `unit_ids` | `layers`, `conductor_id` |
| `unit.state_changed` | required | `from`, `to` | `reason`, `owner`, `conductor_id` |
| `unit.progress` | required | `message` | `stream` (`stdout` or `stderr`), `attempt` |
| `attention.required` | optional | `reason` | `state`, `files`, `detail` |
| `approval.requested` | required | `request_id`, `action`, `plan_sha256`, `expires_at` | `requesting_session`, `summary`, `head_sha` |
| `approval.resolved` | required | `request_id`, `decision` | `action`, `decided_by` |
| `merge.completed` | required | `target_branch`, `merged_commit` | `previous_commit`, `branch`, `pushed` (always false) |

Notes:

* `unit.state_changed.to` is one of `pending`, `ready`, `assigned`, `running`,
  `verifying`, `reviewing`, `queued_for_merge`, `merged`, `failed`,
  `cancelled`, `blocked`, `needs_user`, `interrupted`.
* `attention.required` is emitted when a unit enters `needs_user`,
  `interrupted` or `blocked`, when verification finds changes outside the
  declared scope, when a review or the merge queue finds the review
  insufficient for the actual diff, and when the merge queue meets a
  conflict, a stale branch, or failing gates on the merged result. Typical
  `reason` values: `needs_user`, `budget_exhausted`, `process_gone`,
  `liveness_unverified`, `lease_lost`, `shared_git_state_changed`,
  `out_of_scope_changes`, `review_insufficient`, `no_qualified_reviewer`,
  `escalation_stopped`, `no_eligible_runner`, `routing_invalid`,
  `runner_needs_user`, `merge_conflict`, `merge_stale`,
  `merged_result_failed_gates`.
* `unit.progress` is capped per unit attempt (200 events) so a chatty runner
  cannot flood the UI. Full, redacted output stays in the runtime's log files.
* `expires_at` is a Unix timestamp in seconds (float).
* `head_sha` on a merge approval is the verified commit the review approved.
  The runtime binds the request to it; a decision never sets or changes it.
* `requesting_session` is the opaque OpenClaw session id that asked for the
  approval. It must be a routing identifier, not a credential.

## Approval flow

1. runtime emits `approval.requested`
2. plugin renders the prompt in the requesting OpenClaw session
3. operator decides
4. plugin writes a decision file into the inbox
5. runtime validates it against the pending request
6. runtime emits `approval.resolved`, plugin updates the UI

### Decision object

The plugin copies the binding fields from the `approval.requested` event (and
the pending request) verbatim and adds the decision:

```json
{
  "request_id": "apr-2c1d...",
  "run_id": "run1",
  "unit_id": "alpha",
  "action": "merge",
  "plan_sha256": "3b7e...64 hex chars",
  "session_id": "sess-A",
  "decision": "approved",
  "decided_by": "operator-handle"
}
```

`decision` is `approved` or `rejected`. `decided_by` is a non-empty display
handle (not an email or secret).

### Validation rules

A decision is accepted only if all of the following hold:

1. `request_id` names a known request.
2. The request is still `pending` (not already approved, rejected, or expired).
3. The request has not expired according to the runtime clock.
4. `run_id`, `unit_id`, `action`, `plan_sha256` and `session_id` all equal the
   values recorded when the request was created.
5. `decision` is `approved` or `rejected`, and `decided_by` is present.

Consequences:

* Replaying a decision (same file twice, or the same object later) fails rule 2.
* A decision from another user session, another run, another unit, another
  action, or an older plan fails rule 4.
* Mismatched decisions are recorded in an audit list and **do not consume**
  the request, so the legitimate decision can still arrive.
* The merge queue never trusts an approval record handed to it. It reads only
  the `request_id` and asks the broker (`ApprovalBroker.consume`), which
  requires status `approved`, an unexpired request on the runtime clock, and
  a request bound to the same run, unit, `merge` action, session, plan hash
  and verified head (`head_sha`). The request then becomes `consumed`, so an
  approval authorizes at most one queue entry; the queue also keeps its own
  list of consumed ids. A self made record naming a request that is still
  pending, rejected, expired, consumed or bound elsewhere is refused.

### Decision inbox

Write each decision to a temporary name, then rename it into the inbox as
`<anything>.json` (renames are atomic on the same filesystem). The runtime
processes files in name order, then moves each file and a
`<name>.result.json` envelope into `processed/`. Files over 64 KiB, invalid
JSON, non objects, symlinks and hidden files are rejected or ignored.

## Result envelope

Every runtime operation (including inbox results) returns:

```json
{
  "ok": true,
  "operation": "approval.resolve",
  "changed": true,
  "checks": [{"name": "binding_matches", "ok": true, "detail": ""}],
  "warnings": [],
  "required_user_actions": [],
  "rollback_checkpoint": null,
  "data": {}
}
```

## `required_user_actions`

Each entry is `{"kind", "run_id", "unit_id", "detail", ...}`. Kinds:

| Kind | Meaning | Extra fields |
| :- | :- | :- |
| `approve` | a pending, unexpired approval request | `request_id`, `action`, `expires_at` |
| `budget_exhausted` | attempts, minutes or cost budget used up | |
| `resolve_needs_user` | unit waits for operator input | |
| `reassign_interrupted_unit` | worker process vanished; reassign explicitly | |
| `verify_worker_process` | the worker may still be running (lease lost, or its identity could not be verified on resume); stop it or confirm it is gone before reassigning | |
| `retry_or_reassign_failed_unit` | unit failed | |
| `unblock_unit` | unit is blocked | |
| `resolve_conflict` | merge conflict against the target branch | |
| `unmerged_work` | cleanup refused: unique commits would be lost | |
| `reverify_and_reapprove` | branch moved after approval | |
| `fix_failing_gates` | gates failed on the merged result | |

## Versioning

* Adding event types or optional fields is non breaking; consumers ignore
  what they do not know.
* Removing or renaming a field, or changing its meaning, bumps
  `event_version`.
* `schema_version` tracks persisted document layouts and changes with them.
