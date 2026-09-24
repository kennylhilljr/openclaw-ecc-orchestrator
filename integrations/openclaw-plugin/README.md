# OpenClaw plugin: ECC orchestrator

This plugin connects the `openclaw-ecc-orchestrator` runtime to OpenClaw
2026.9.6. It reads the runtime's event log, shows runs, attention items,
approval requests and merges inside OpenClaw, and turns an operator's
decision into a decision file in the runtime's inbox. The contract it
implements is [docs/openclaw-events.md](../../docs/openclaw-events.md).

OpenClaw owns sessions, identity, scopes and the UI. The runtime owns plans,
hashes and validation. The plugin only relays: it never computes or changes a
plan hash or head sha, never decides on its own, and has no auto approve
setting.

**Option names in this document.** As in the runtime's docs, command line
options are written without their two leading hyphens: the option shown as
`json` is typed as two hyphens followed by `json`.

## What it does

| Part | Behavior |
| :- | :- |
| Event tailing | A background service polls the JSONL log (default every second). Only complete lines are consumed, so a torn last line is retried later. Lines that do not parse or lack the envelope are skipped; unknown event types are ignored. Events are deduplicated by `id`, must have a `seq` above the last processed one, and are applied in `seq` order. A rotated or truncated log is re read from the start without replaying. |
| Restart safety | The read position (byte offset, file identity, last `seq`, recent ids) and the view model are written together, atomically, to `<stateDir>/plugin-state.json`. A restart resumes after the last applied event. An unreadable state file is set aside and the view is rebuilt from the log. |
| Surfaces | Run progress and unit state changes, the last progress line per unit, `attention.required` items, pending `approval.requested` items with summary, plan sha256, head sha, requesting session and time left, `approval.resolved`, and `merge.completed`. |
| Decisions | Copies the pending request's binding fields verbatim from the `approval.requested` event (`request_id`, `run_id`, `unit_id`, `action`, `plan_sha256`, and `requesting_session` as `session_id`) and adds `decision` and `decided_by`. Writes a hidden temp file (`0600`), fsyncs, renames it to `<ms>-<request_id>-<random>.json` and fsyncs the directory. |
| Secrets | Relies on the runtime's redaction, then applies its own: values under secret looking keys (`token`, `secret`, `password`, `api_key`, `auth`, `cookie`, `session_id`, ...) are replaced, known token shapes, credentials in URLs, `name=value` pairs and long mixed case opaque strings are masked, control and bidi characters are stripped, and strings are bounded. Hex strings (plan hashes, commit shas) are kept. Logs contain counts and error codes only, never event text. |

### OpenClaw surfaces registered

| Surface | Name | Scope | Purpose |
| :- | :- | :- | :- |
| Command | `/ecc [status\|pending\|attention\|show <id>\|runtime [run_id]\|help]` | `operator.read` | Read only views as a chat reply. |
| Command | `/ecc-decide approve <request_id> <plan_hash_prefix>` and `/ecc-decide reject <request_id>` | `operator.approvals` | Decide from the requesting session when OpenClaw supplies a sender identity for the command. |
| Gateway method | `ecc-orchestrator.snapshot` | `operator.read` | Sanitized JSON snapshot of runs, approvals, attention and merges. |
| Gateway method | `ecc-orchestrator.decide` with params `{requestId, decision: "approve" or "reject", planHashPrefix, sessionKey}` | `operator.approvals` | Decide as the connection's authenticated OpenClaw profile. |
| HTTP route | `GET /plugins/ecc-orchestrator/panel`, `auth: "gateway"` | gateway auth | Static HTML (no scripts, strict CSP) for the Control UI tab. Other methods get 405. |
| Control UI tab | `ECC Orchestrator` (descriptor `surface: "tab"`, `path` above) | `operator.read` | Sidebar tab rendered by the Control UI in a sandboxed frame. |
| Service | `ecc-orchestrator-tail` | none | Polls the event log. |

### How approvals work

1. The runtime emits `approval.requested`. The request appears in the tab, in
   `/ecc pending` and in `ecc-orchestrator.snapshot`, with the exact commands
   to approve or reject.
2. An operator decides through one of two OpenClaw mechanisms:
   * `/ecc-decide` typed in the requesting session. `decided_by` is the
     sender OpenClaw attributes to the command (`PluginCommandContext.senderId`)
     and the session is the host bound conversation (`sessionKey`).
   * `ecc-orchestrator.decide` called on an authenticated operator connection
     (for example `openclaw gateway call ecc-orchestrator.decide` with the
     `params` option). `decided_by` is the connection's authenticated profile
     id (`GatewayClient.authenticatedUserProfile.profileId`); request data
     never sets it.
3. The plugin refuses, and writes nothing, when: the request id is unknown,
   the request is expired, already resolved, invalid or already decided from
   OpenClaw, the identity is missing, looks like an email or secret, or is an
   OpenClaw client program id such as `cli`, the session differs from the
   request's requesting session, or (for approve) the plan hash prefix of at
   least 8 hex characters is missing or does not match. Rejecting does not
   need the prefix.
4. The runtime validates the file (`DecisionInbox`, `ApprovalBroker.resolve`)
   and emits `approval.resolved`; the plugin shows the final status and the
   runtime's result envelope from `processed/`.

Both paths require the `operator.approvals` scope, enforced by OpenClaw
before the plugin runs.

## Configuration

Set under `plugins.entries.ecc-orchestrator.config` in the OpenClaw config.

| Key | Required | Meaning |
| :- | :- | :- |
| `eventLog` | yes | Absolute path of the runtime event log, for example the runtime's `event_log`. |
| `inbox` | yes | Absolute path of the runtime's `decision_inbox` directory. |
| `stateDir` | yes | Absolute directory for the plugin's own state, separate from the inbox. |
| `runtimeCli` | no | Absolute path of an executable used only for `status` with the `json` option (for example the venv's `ecc-orchestrator`, or `python3`). |
| `runtimeCliArgs` | no | Up to 16 arguments placed before `status`, for example `["-m", "openclaw_ecc_orchestrator"]` plus the runtime's `config` option and its absolute path. |
| `pollIntervalMs` | no | 250 to 60000, default 1000. |

Every path must be absolute, contain no `.` or `..` component or NUL byte, and
must not be inside `~/.openclaw` (also through a symlink) or any `.openclaw`
or `.git` directory. Unknown keys are refused. With an invalid or missing
configuration the plugin registers only `/ecc`, which explains the problem.
The runtime CLI runs without a shell, with a 20 second timeout, a 1 MiB output
cap and only `PATH`, `HOME`, `LANG`, `LC_ALL`, `XDG_STATE_HOME`,
`XDG_DATA_HOME` and `TMPDIR` in its environment.

The plugin has no dependencies and no build step: plain ESM loaded directly by
the OpenClaw plugin loader (Node 24.16 or later, as OpenClaw requires).

## Install on the production gateway (manual, requires operator approval)

> **Approval required.** Nothing below has been run against the production
> gateway or `~/.openclaw`. Each step changes production configuration and
> must be approved and run by the operator. Take an OpenClaw backup first
> (the Phase 0 procedure in `agent-control-plane-infra`).

1. Choose paths outside `~/.openclaw`: the runtime's event log and decision
   inbox (from the runtime config), and a plugin state directory such as
   `~/.local/state/openclaw-ecc-orchestrator/openclaw-plugin`.
2. Check existing plugin load paths, so the next step keeps them:

   ```
   openclaw config get plugins.load.paths
   openclaw config get plugins.allow
   ```

3. Add this directory to the load paths (include any existing entries in the
   list), then the plugin entry:

   ```
   openclaw config set plugins.load.paths '["<existing entries>", "/ABS/PATH/openclaw-ecc-orchestrator/integrations/openclaw-plugin"]'
   openclaw config set plugins.entries.ecc-orchestrator '{enabled: true, config: {eventLog: "/ABS/STATE/openclaw-events.jsonl", inbox: "/ABS/STATE/decisions", stateDir: "/ABS/PLUGIN_STATE"}}'
   ```

   Set the load path first: the entry is only accepted once the plugin can be
   discovered. If `plugins.allow` is set, add `ecc-orchestrator` to it; if it
   is unset, leave it unset (setting it would disable every plugin not
   listed). Without an allow entry OpenClaw loads the plugin with a warning
   that its origin cannot be verified.
4. The default hybrid reload applies the change without a restart. Verify:

   ```
   openclaw plugins inspect ecc-orchestrator
   ```

   with the `runtime` and `json` options to see the registered command,
   methods, route and service.
5. In the Control UI open the **ECC Orchestrator** tab. Like every plugin tab
   it needs the Control UI on HTTPS or a loopback URL such as
   `http://127.0.0.1:18789/`.
6. Grant `operator.approvals` only to operators who may approve merges and
   cleanups.

This sequence was rehearsed against a disposable config (temporary
`OPENCLAW_STATE_DIR` and `OPENCLAW_CONFIG_PATH`): the plugin inspected as
`loaded`, and the rollback below left `plugins` empty.

## Rollback and uninstall

Order matters: remove the entry while the plugin is still discoverable.

```
openclaw config unset plugins.entries.ecc-orchestrator
openclaw config set plugins.load.paths '["<previous entries>"]'
```

Use `openclaw config unset plugins.load.paths` instead of the second command
when there were no previous entries, and remove `ecc-orchestrator` from
`plugins.allow` if you added it. Hybrid reload removes the command, methods,
route, tab and service. Optionally delete the plugin `stateDir`. Decision
files already written stay in the runtime's inbox and `processed/`
directories and remain valid audit records. Restoring the pre install
OpenClaw backup is the full rollback.

## Tests

From this directory, with Node 22 or later (OpenClaw itself needs 24.16):

| Suite | Command | What it proves |
| :- | :- | :- |
| Unit | `npm test` | Tailing, partial lines, dedupe, seq order, rotation, restart position, rendering, masking, config validation, decision construction, atomic write, identity sources and refusals. |
| Contract | `npm run test:contract` | Real events from the Python runtime (`JsonlEventSink`, `ApprovalBroker`, a throwaway git repo for the head sha); the plugin's decision file is accepted by `DecisionInbox` and `ApprovalBroker`, tampered copies (plan hash, session, unit) are rejected without consuming the request, and a replay is rejected. Needs `python3` 3.11 or later and `git`. |
| Isolated gateway | `npm run test:integration` | Starts a disposable gateway (see below) and exercises one approval end to end. Needs `openclaw` 2026.9.6, `python3`, `git`, `openssl`, `curl`, `lsof`. |

The isolated gateway test creates one `mktemp -d` root holding `HOME`,
`OPENCLAW_STATE_DIR`, `OPENCLAW_CONFIG_PATH`, the gateway file log
(`logging.file`) and all runtime and plugin state. It runs
`openclaw gateway run` on loopback port 19917 (refusing 18789 or a busy port)
with a random token, `plugins.allow` limited to this plugin and Bonjour
disabled, and loads the plugin through `plugins.load.paths` without an
install record. It checks: runtime registration, the snapshot, the panel
(200 with the token, 401 without), a write only connection refused by
OpenClaw's scope check, an operator UI command refused for missing identity,
a wrong session refused, an approval through `ecc-orchestrator.decide`
accepted by the Python runtime, `approval.resolved` surfaced, and a replay
refused. On exit it stops the gateway process tree and the port listener
and deletes only its temp root. `ECC_TEST_KEEP=1` keeps the root for
inspection; `ECC_TEST_PORT` changes the port.

## OpenClaw APIs used

All paths are inside the installed package
(`$(npm root -g)/openclaw`, OpenClaw 2026.9.6).

| API | Where it is defined or documented |
| :- | :- |
| Plugin object `{id, name, description, register}` (what `definePluginEntry` returns) | `dist/plugin-entry-BOulgRcx.mjs`, `docs/plugins/sdk-entrypoints/define-plugin-entry.md` |
| Manifest `openclaw.plugin.json`, `configSchema`, `activation`, `commandAliases` | `docs/plugins/manifest.md`, `docs/plugins/building-plugins.md` |
| `api.pluginConfig`, `api.logger`, `api.registerService`, `api.registerCommand`, `api.registerGatewayMethod`, `api.registerHttpRoute`, `api.session.controls.registerControlUiDescriptor` | `OpenClawPluginApi` in `dist/agent-harness-runtime-wMciqZ6Z.d.ts` |
| `PluginCommandContext` (`senderId`, `sessionKey`, `isAuthorizedSender`) and command `requiredScopes` | same file, `PluginCommandContext` and `OpenClawPluginCommandDefinition`; `docs/plugins/sdk-overview/tools-and-commands.md` |
| `GatewayRequestHandlerOptions` (`params`, `client`, `respond`) and `GatewayClient.authenticatedUserProfile` | same file |
| `PluginControlUiDescriptor` with `surface: "tab"` and `path`, gateway protected route rendered in a sandboxed frame, GET and HEAD only | same file; `docs/plugins/sdk-overview/host-hooks.md` |
| `OpenClawPluginHttpRouteParams` (`auth: "gateway"`, `match: "exact"`) | same file |
| Operator scopes `operator.read` and `operator.approvals` | `docs/gateway/operator-scopes.md` |
| Sender attribution for chat commands | `resolveChatSendCallerContext` and `gatewayClientSenderFields` in `dist/session-sharing-HsRoUX6K.mjs` |
| Canonical client ids (`cli`, `openclaw-tui`, ...) | `GATEWAY_CLIENT_IDS` in `dist/client-info-B_ICKCYw.mjs` |
| Test client: `callGatewayFromCli` with explicit scopes, client name and mode | `dist/plugin-sdk/gateway-runtime.js`, `dist/gateway-rpc-JavFiplO.d.ts` |
| Isolation: `OPENCLAW_STATE_DIR`, `OPENCLAW_CONFIG_PATH`, port; `logging.file`; `plugins.load.paths`, `plugins.allow`, `plugins.entries` | `docs/gateway/multiple-gateways.md`, `docs/logging.md`, `docs/gateway/config-extensions.md` |

## Gaps

* **Command identity for Control UI users.** In 2026.9.6 a plugin command's
  context carries `senderId` only for channel senders and non UI gateway
  clients (where it is the client program id, for example `cli`, which the
  plugin refuses). For Control UI and terminal UI connections it is absent,
  so `/ecc-decide` fails closed there and operators use
  `ecc-orchestrator.decide`. A command context field with the authenticated
  profile would let the command work everywhere.
* **Session on the gateway method.** Plugin RPC handlers receive the
  authenticated profile but no host bound session. The caller names the
  session and the plugin requires it to equal the request's requesting
  session; the runtime checks the binding again. This matches the spec's
  position that multiplayer ownership is coordination, not a security
  boundary.
* **Shared owner identity.** On a single user gateway with token auth the
  profile is the shared owner (`gateway-owner` in the isolated test), not a
  person. Per person `decided_by` needs per person sign in (Phase 5).
* **No native approval prompt.** Plugin permission requests
  (`plugin.approval.*`, `docs/plugins/plugin-permission-requests.md`) are
  tied to tool calls and report only the decision, not who decided, so they
  cannot carry `decided_by`. Decisions go through the command or method.
* **No push notification.** Without native Control UI code (a lab setting,
  `docs/plugins/feature-plugins.md`) or injecting runtime text into an agent
  prompt, the plugin cannot push an alert into a session. Operators see new
  items in the tab (refreshes every 10 seconds), `/ecc` or the snapshot.
* **Tab is read only.** Gateway protected tab frames accept only GET and
  HEAD, so approve buttons cannot live in the tab.
* **Runtime inbox consumer.** The runtime processes inbox files only inside
  `approvals approve` or `approvals reject` or through
  `DecisionInbox.process`. A decision written by the plugin waits until one of
  those runs; the runtime needs a standing inbox consumer or a CLI subcommand
  that only processes the inbox.
* **Plugin state location.** The plugin keeps its position in its own
  `stateDir` instead of OpenClaw's keyed store, to keep it outside
  `~/.openclaw`.
