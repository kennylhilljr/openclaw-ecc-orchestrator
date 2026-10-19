// Approve or reject ECC orchestrator approval requests from a terminal,
// through the plugin's ecc-orchestrator.decide gateway method as an operator
// UI client (the identity OpenClaw attests for the connection is recorded as
// decided_by). Run by bin/ecc-decide.
//
// Usage:
//   ecc-decide.mjs <openclaw package dir> list
//   ecc-decide.mjs <openclaw package dir> approve|reject [request_id] [--yes]
//
// With no request id, the single pending request is used; with several
// pending, they are listed and nothing is decided. Approving shows the run,
// unit, head and plan hash and asks for confirmation unless --yes is given.
// Env: ECC_GATEWAY_PORT (default 18789). ECC_GATEWAY_CLIENT (tests only): a
// module path exporting callGatewayFromCli, used instead of OpenClaw's.
// Exit codes: 0 decided or listed, 1 refused or error, 2 usage or ambiguous.

import path from "node:path";
import readline from "node:readline/promises";
import { pathToFileURL } from "node:url";

const SCOPES = ["operator.read", "operator.write", "operator.approvals"];

function usage(msg) {
  if (msg) process.stderr.write(`ecc-decide: ${msg}\n`);
  process.stderr.write("usage: bin/ecc-decide list | approve [request_id] [--yes] | reject [request_id] [--yes]\n");
  process.exit(2);
}

const args = process.argv.slice(2);
const pkgDir = args.shift();
const yes = args.includes("--yes");
const rest = args.filter((a) => a !== "--yes");
const verb = rest[0];
const requestArg = rest[1];
if (!pkgDir || !["list", "approve", "reject"].includes(verb) || rest.length > 2) usage();
if (verb === "list" && requestArg) usage("list takes no request id");
if (requestArg && !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(requestArg)) usage("invalid request id");

const port = process.env.ECC_GATEWAY_PORT || "18789";
const clientPath = process.env.ECC_GATEWAY_CLIENT || path.join(pkgDir, "dist", "plugin-sdk", "gateway-runtime.js");
let client;
try {
  client = await import(pathToFileURL(clientPath).href);
} catch {
  process.stderr.write(`ecc-decide: cannot load OpenClaw's gateway client from ${clientPath}\n`);
  process.exit(1);
}

async function call(method, params) {
  return client.callGatewayFromCli(
    method,
    { port: String(port), timeout: "20000", json: true },
    params,
    { scopes: SCOPES, clientName: "openclaw-tui", mode: "ui" },
  );
}

function fmtExpiry(expiresAt) {
  const s = Math.round(Number(expiresAt) - Date.now() / 1000);
  if (!Number.isFinite(s)) return "?";
  return s <= 0 ? "expired" : s >= 120 ? `${Math.round(s / 60)} min` : `${s} s`;
}

function describe(a) {
  return [
    `  request   ${a.request_id}`,
    `  action    ${a.action} of ${a.run_id}/${a.unit_id}`,
    `  head      ${String(a.head_sha || "-").slice(0, 12)}`,
    `  plan      ${String(a.plan_sha256 || "").slice(0, 12)}`,
    `  session   ${a.requesting_session || "-"}`,
    `  expires   in ${fmtExpiry(a.expires_at)}`,
    a.summary ? `  summary   ${a.summary}` : null,
  ].filter(Boolean).join("\n");
}

let snap;
try {
  snap = await call("ecc-orchestrator.snapshot", {});
} catch (err) {
  process.stderr.write(`ecc-decide: snapshot failed: ${err && err.message}\n`);
  process.exit(1);
}
const pending = Array.isArray(snap?.pending_approvals) ? snap.pending_approvals : [];

if (verb === "list") {
  if (!pending.length) {
    process.stdout.write("No pending approval requests.\n");
  } else {
    process.stdout.write(`${pending.length} pending approval request(s):\n\n${pending.map(describe).join("\n\n")}\n`);
  }
  process.exit(0);
}

let target;
if (requestArg) {
  target = pending.find((a) => a.request_id === requestArg);
  if (!target) {
    process.stderr.write(`ecc-decide: ${requestArg} is not a pending request (see: bin/ecc-decide list)\n`);
    process.exit(1);
  }
} else if (pending.length === 1) {
  target = pending[0];
} else {
  process.stderr.write(
    pending.length
      ? `ecc-decide: ${pending.length} requests are pending; name one:\n\n${pending.map(describe).join("\n\n")}\n`
      : "ecc-decide: no pending approval requests\n",
  );
  process.exit(pending.length ? 2 : 1);
}

process.stdout.write(`${verb === "approve" ? "Approve" : "Reject"} this request?\n${describe(target)}\n`);
if (!yes) {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  const answer = (await rl.question("Type yes to continue: ")).trim().toLowerCase();
  rl.close();
  if (answer !== "yes") {
    process.stdout.write("Nothing decided.\n");
    process.exit(1);
  }
}

const params = {
  requestId: target.request_id,
  decision: verb,
  sessionKey: target.requesting_session,
};
if (verb === "approve") params.planHashPrefix = String(target.plan_sha256 || "").slice(0, 12);

try {
  const res = await call("ecc-orchestrator.decide", params);
  if (!res || res.ok === false) {
    process.stderr.write(`ecc-decide: refused: ${JSON.stringify(res?.error ?? res)}\n`);
    process.exit(1);
  }
  process.stdout.write(
    `Decision ${res.decision} for ${res.request_id} written to the runtime inbox by ${res.decided_by}. ` +
      "The inbox processor applies it within a few seconds.\n",
  );
  process.exit(0);
} catch (err) {
  process.stderr.write(`ecc-decide: refused: ${err && err.message}\n`);
  process.exit(1);
}
