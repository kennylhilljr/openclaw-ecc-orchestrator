#!/usr/bin/env bash
# Isolated OpenClaw gateway integration test for the ecc-orchestrator plugin.
#
# Everything lives under one `mktemp -d` root: HOME, OPENCLAW_STATE_DIR,
# OPENCLAW_CONFIG_PATH, the runtime state and the plugin state. The gateway
# runs in the foreground process group of this script on a non production
# loopback port with a disposable token, loads the plugin from this checkout
# through plugins.load.paths (no install record), and is stopped on exit. Only
# the temp root created here is removed. The production gateway, its service
# and ~/.openclaw are never touched.
#
# Requirements: openclaw 2026.9.6 and node on PATH, python3 (3.11+), git,
# openssl, curl. Usage: bash tests/integration/isolated-gateway.sh
# Env: ECC_TEST_PORT (default 19917), ECC_TEST_KEEP=1 keeps the temp root.

set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "$0")/../.." && pwd -P)"
RUNTIME_SRC="${RUNTIME_SRC:-$(cd "$PLUGIN_DIR/../../src" && pwd -P)}"
OPENCLAW_BIN="$(command -v openclaw)"
NODE_BIN="$(command -v node)"
PORT="${ECC_TEST_PORT:-19917}"
REAL_HOME="$HOME"
SESSION_KEY="agent:main:main"

fail() { echo "FAIL: $*" >&2; exit 1; }
[ -n "$OPENCLAW_BIN" ] || fail "openclaw not on PATH"
[ "$PORT" != "18789" ] || fail "refusing the production default port"
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then fail "port $PORT is in use"; fi

ROOT="$(mktemp -d "${TMPDIR:-/tmp}/ecc-oc-it.XXXXXX")"
ROOT="$(cd "$ROOT" && pwd -P)"
case "$ROOT" in "$REAL_HOME/.openclaw"*) fail "temp root inside ~/.openclaw";; esac
GW_PID=""

descendants() {
  local p
  for p in $(pgrep -P "$1" 2>/dev/null); do echo "$p"; descendants "$p"; done
}

# PIDs of this test's gateway: the `gateway run` wrapper, its descendants
# (the wrapper spawns an `openclaw-gateway` child), and whatever listens on
# the isolated port. Never touches other ports or processes.
gateway_pids() {
  { [ -n "$GW_PID" ] && echo "$GW_PID" && descendants "$GW_PID"; lsof -t -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null; } | sort -u
}

stop_gateway() {
  local pids p
  pids="$(gateway_pids)"
  [ -n "$pids" ] || return 0
  for p in $pids; do kill -TERM "$p" 2>/dev/null || true; done
  for _ in $(seq 1 40); do
    local alive=""
    for p in $pids; do kill -0 "$p" 2>/dev/null && alive=1; done
    [ -n "$alive" ] || break
    sleep 0.5
  done
  for p in $pids; do kill -KILL "$p" 2>/dev/null || true; done
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then echo "WARNING: port $PORT still in use" >&2; fi
}

cleanup() {
  local rc=$?
  stop_gateway
  if [ "${ECC_TEST_KEEP:-0}" = "1" ]; then
    echo "kept temp root: $ROOT"
  else
    rm -rf "$ROOT"
  fi
  exit $rc
}
trap cleanup EXIT INT TERM

export HOME="$ROOT/home"
export OPENCLAW_STATE_DIR="$ROOT/openclaw-state"
export OPENCLAW_CONFIG_PATH="$OPENCLAW_STATE_DIR/openclaw.json"
unset OPENCLAW_PROFILE OPENCLAW_GATEWAY_URL OPENCLAW_GATEWAY_TOKEN OPENCLAW_GATEWAY_PASSWORD OPENCLAW_CONTAINER || true
export OPENCLAW_GATEWAY_PORT="$PORT"
mkdir -p "$HOME" "$OPENCLAW_STATE_DIR" "$ROOT/logs"
chmod 700 "$ROOT" "$OPENCLAW_STATE_DIR"

RT="$ROOT/runtime"
mkdir -p "$RT"
TOKEN="$(openssl rand -hex 24)"

echo "== generate runtime events with the Python runtime"
GEN="$(PYTHONPATH="$RUNTIME_SRC" PYTHONDONTWRITEBYTECODE=1 python3 "$PLUGIN_DIR/tests/contract/py/gen_events.py" "$RT/work" "$SESSION_KEY")"
echo "$GEN" > "$ROOT/logs/gen.json"
REQ="$("$NODE_BIN" -e 'const g=JSON.parse(process.argv[1]);process.stdout.write(g.request_id)' "$GEN")"
PLAN="$("$NODE_BIN" -e 'const g=JSON.parse(process.argv[1]);process.stdout.write(g.plan_sha256)' "$GEN")"
EVENT_LOG="$("$NODE_BIN" -e 'const g=JSON.parse(process.argv[1]);process.stdout.write(g.event_log)' "$GEN")"
APPROVALS="$("$NODE_BIN" -e 'const g=JSON.parse(process.argv[1]);process.stdout.write(g.approvals_state)' "$GEN")"
INBOX="$RT/work/state/decisions"
PLUGIN_STATE="$RT/plugin-state"

echo "== write the isolated OpenClaw config"
"$NODE_BIN" -e '
const [file, port, token, pluginDir, eventLog, inbox, stateDir, logFile] = process.argv.slice(1);
const cfg = {
  gateway: {
    mode: "local",
    port: Number(port),
    bind: "loopback",
    auth: { mode: "token", token },
  },
  // Keep the gateway file log inside the temp root (the default is shared /tmp/openclaw).
  logging: { file: logFile },
  plugins: {
    // Only this plugin loads: no LAN advertising (bonjour), no memory cron jobs.
    allow: ["ecc-orchestrator"],
    load: { paths: [pluginDir] },
    entries: {
      bonjour: { enabled: false },
      "ecc-orchestrator": { enabled: true, config: { eventLog, inbox, stateDir, pollIntervalMs: 500 } },
    },
  },
};
require("fs").writeFileSync(file, JSON.stringify(cfg, null, 2), { mode: 0o600 });
' "$OPENCLAW_CONFIG_PATH" "$PORT" "$TOKEN" "$PLUGIN_DIR" "$EVENT_LOG" "$INBOX" "$PLUGIN_STATE" "$ROOT/logs/openclaw-file.log"

oc() { "$OPENCLAW_BIN" "$@"; }
call() {
  local params="${2:-}"
  [ -n "$params" ] || params='{}'
  oc gateway call "$1" --port "$PORT" --json --timeout 20000 --params "$params"
}

echo "== start the isolated gateway on 127.0.0.1:$PORT"
oc gateway run --port "$PORT" --bind loopback --auth token --allow-unconfigured > "$ROOT/logs/gateway.log" 2>&1 &
GW_PID=$!
for i in $(seq 1 90); do
  if call health >/dev/null 2>&1; then break; fi
  kill -0 "$GW_PID" 2>/dev/null || { tail -40 "$ROOT/logs/gateway.log"; fail "gateway exited"; }
  sleep 1
  [ "$i" -lt 90 ] || { tail -40 "$ROOT/logs/gateway.log"; fail "gateway did not become healthy"; }
done
echo "gateway healthy (pid $GW_PID)"

echo "== inspect the plugin runtime registration"
oc plugins inspect ecc-orchestrator --runtime --json > "$ROOT/logs/inspect.json" 2>"$ROOT/logs/inspect.err" || { cat "$ROOT/logs/inspect.err"; fail "plugins inspect failed"; }
"$NODE_BIN" -e '
const j = JSON.parse(require("fs").readFileSync(process.argv[1], "utf8"));
const s = JSON.stringify(j);
for (const needle of ["ecc-orchestrator.snapshot", "ecc-decide", "ecc-orchestrator-tail"]) {
  if (!s.includes(needle)) { console.error("missing registration: " + needle); process.exit(1); }
}
if (!(j.httpRouteCount >= 1)) { console.error("panel http route not registered"); process.exit(1); }
if (j.plugin && j.plugin.status !== "loaded") { console.error("plugin status " + j.plugin.status); process.exit(1); }
console.log("runtime registration ok");
' "$ROOT/logs/inspect.json"

echo "== snapshot shows the pending approval"
SNAP="$(call ecc-orchestrator.snapshot)"
echo "$SNAP" > "$ROOT/logs/snapshot-before.json"
"$NODE_BIN" -e '
const s = JSON.parse(process.argv[1]); const req = process.argv[2];
const p = (s.pending_approvals || []).find((a) => a.request_id === req);
if (!p) { console.error("pending approval missing"); process.exit(1); }
if (!p.plan_sha256 || !p.head_sha) { console.error("plan hash or head sha missing"); process.exit(1); }
if (JSON.stringify(s).includes(process.argv[3])) { console.error("secret leaked into snapshot"); process.exit(1); }
console.log("pending approval visible with plan hash and head sha");
' "$SNAP" "$REQ" "$("$NODE_BIN" -e 'process.stdout.write(JSON.parse(process.argv[1]).fake_token)' "$GEN")"

echo "== panel route over gateway auth"
CODE="$(curl -s -o "$ROOT/logs/panel.html" -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/plugins/ecc-orchestrator/panel")"
echo "panel http $CODE"
[ "$CODE" = "200" ] && grep -q "$REQ" "$ROOT/logs/panel.html" || fail "panel did not render the request"
UNAUTH="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/plugins/ecc-orchestrator/panel")"
echo "panel without auth http $UNAUTH"
[ "$UNAUTH" != "200" ] || fail "panel served without gateway auth"

send() {
  local msg="$1" idem
  idem="$(openssl rand -hex 8)"
  call chat.send "$("$NODE_BIN" -e 'process.stdout.write(JSON.stringify({sessionKey:process.argv[1],message:process.argv[2],idempotencyKey:process.argv[3]}))' "$SESSION_KEY" "$msg" "$idem")"
}

history() { call chat.history "{\"sessionKey\":\"$SESSION_KEY\",\"limit\":20}"; }
last_reply() {
  "$NODE_BIN" -e '
const h = JSON.parse(require("fs").readFileSync(0, "utf8"));
const msgs = (h.messages || []).filter((m) => m.role === "assistant");
const m = msgs[msgs.length - 1] || {};
const c = Array.isArray(m.content) ? m.content.map((x) => x.text || "").join(" ") : String(m.content || "");
process.stdout.write(c);
'
}

echo "== a write only connection is refused by OpenClaw's scope check (operator.approvals)"
send "/ecc-decide approve $REQ ${PLAN:0:12}" > "$ROOT/logs/send-write-only.json" 2>&1 || { cat "$ROOT/logs/send-write-only.json"; fail "chat.send failed"; }
sleep 2
REPLY="$(history | last_reply)"
echo "reply: $REPLY"
case "$REPLY" in *"operator.approvals"*) ;; *) fail "expected a scope refusal";; esac
if ls "$INBOX"/*.json >/dev/null 2>&1; then fail "decision written without operator.approvals"; fi

echo "== an operator UI command without an OpenClaw sender identity is refused (fail closed)"
OPENCLAW_PKG="$(cd "$(dirname "$("$NODE_BIN" -e 'process.stdout.write(require("fs").realpathSync(process.argv[1]))' "$OPENCLAW_BIN")")" && pwd -P)"
operator_call() { "$NODE_BIN" "$PLUGIN_DIR/tests/integration/operator-call.mjs" "$OPENCLAW_PKG" "$PORT" "$1" "$2"; }
operator_send() {
  operator_call chat.send "$("$NODE_BIN" -e 'process.stdout.write(JSON.stringify({sessionKey:process.argv[1],message:process.argv[2],idempotencyKey:require("crypto").randomBytes(8).toString("hex")}))' "$SESSION_KEY" "$1")"
}
operator_send "/ecc-decide approve $REQ ${PLAN:0:12}" > "$ROOT/logs/send-tui.json" 2>&1 || { cat "$ROOT/logs/send-tui.json"; fail "operator chat.send failed"; }
sleep 2
REPLY="$(history | last_reply)"
echo "reply: $REPLY"
case "$REPLY" in *"missing_identity"*) ;; *) fail "expected a missing identity refusal";; esac
if ls "$INBOX"/*.json >/dev/null 2>&1; then fail "decision written without identity"; fi

decide_rpc() {
  operator_call ecc-orchestrator.decide "$("$NODE_BIN" -e 'process.stdout.write(JSON.stringify({requestId:process.argv[1],decision:process.argv[2],planHashPrefix:process.argv[3],sessionKey:process.argv[4]}))' "$1" "$2" "$3" "$4")"
}

echo "== wrong session is refused by the plugin"
decide_rpc "$REQ" approve "${PLAN:0:12}" "agent:main:other" > "$ROOT/logs/decide-wrong-session.json" 2>&1 && fail "wrong session accepted" || true
grep -o "session_mismatch" "$ROOT/logs/decide-wrong-session.json" | head -1 || { cat "$ROOT/logs/decide-wrong-session.json"; fail "expected session_mismatch"; }

echo "== decide through the ecc-orchestrator.decide gateway method (authenticated profile)"
decide_rpc "$REQ" approve "${PLAN:0:12}" "$SESSION_KEY" > "$ROOT/logs/decide.json" 2>&1 || { cat "$ROOT/logs/decide.json"; fail "decide failed"; }
cat "$ROOT/logs/decide.json"
ls "$INBOX"/*.json >/dev/null 2>&1 || fail "no decision file written"
DECISION_FILE="$(ls "$INBOX"/*.json | head -1)"
"$NODE_BIN" -e '
const d = JSON.parse(require("fs").readFileSync(process.argv[1], "utf8"));
console.log("decision fields:", Object.keys(d).join(","));
console.log("decided_by:", d.decided_by, "session_id:", d.session_id, "decision:", d.decision);
' "$DECISION_FILE"

echo "== runtime accepts the decision"
PROC="$(PYTHONPATH="$RUNTIME_SRC" PYTHONDONTWRITEBYTECODE=1 python3 "$PLUGIN_DIR/tests/contract/py/process_inbox.py" "$APPROVALS" "$EVENT_LOG" "$INBOX")"
echo "$PROC" > "$ROOT/logs/process.json"
"$NODE_BIN" -e '
const p = JSON.parse(process.argv[1]); const req = process.argv[2];
if (!p.results.length || !p.results.every((r) => r.ok)) { console.error("runtime refused: " + JSON.stringify(p.results)); process.exit(1); }
if (p.requests[req].status !== "approved") { console.error("request not approved"); process.exit(1); }
console.log("runtime accepted; request status approved");
' "$PROC" "$REQ"

sleep 2
SNAP2="$(call ecc-orchestrator.snapshot)"
echo "$SNAP2" > "$ROOT/logs/snapshot-after.json"
"$NODE_BIN" -e '
const s = JSON.parse(process.argv[1]); const req = process.argv[2];
const a = (s.approvals || []).find((x) => x.request_id === req);
if (!a || a.status !== "approved") { console.error("approval.resolved not surfaced"); process.exit(1); }
console.log("approval.resolved surfaced in OpenClaw: status approved, decided_by " + a.decided_by);
' "$SNAP2" "$REQ"

echo "== a replayed decision is refused by the plugin"
decide_rpc "$REQ" approve "${PLAN:0:12}" "$SESSION_KEY" > "$ROOT/logs/decide-replay.json" 2>&1 && fail "replay accepted" || true
grep -o "not_pending" "$ROOT/logs/decide-replay.json" | head -1 || { cat "$ROOT/logs/decide-replay.json"; fail "expected not_pending"; }
COUNT="$(find "$INBOX" -maxdepth 1 -name '*.json' | wc -l | tr -d ' ')"
[ "$COUNT" = "0" ] || fail "a second decision file was written"
echo "no second decision written"

echo "PASS: isolated gateway integration"
