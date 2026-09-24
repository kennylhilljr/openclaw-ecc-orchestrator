// Call one Gateway method on the isolated gateway the way the OpenClaw
// terminal UI connects: an operator UI connection (client id openclaw-tui,
// mode ui) with operator.read, operator.write and operator.approvals, using
// OpenClaw's own public client (openclaw/plugin-sdk/gateway-runtime,
// callGatewayFromCli). Connection settings come from OPENCLAW_CONFIG_PATH
// (the isolated config); the port is explicit.
//
// Usage: node operator-call.mjs <openclaw package dir> <port> <method> <params json>

import path from "node:path";
import { pathToFileURL } from "node:url";

const [pkgDir, port, method, paramsJson] = process.argv.slice(2);
const mod = await import(pathToFileURL(path.join(pkgDir, "dist", "plugin-sdk", "gateway-runtime.js")).href);
try {
  const res = await mod.callGatewayFromCli(
    method,
    { port: String(port), timeout: "20000", json: true },
    JSON.parse(paramsJson),
    { scopes: ["operator.read", "operator.write", "operator.approvals"], clientName: "openclaw-tui", mode: "ui" },
  );
  process.stdout.write(`${JSON.stringify(res)}\n`);
  process.exit(0);
} catch (err) {
  process.stdout.write(`${JSON.stringify({ ok: false, error: { message: String(err && err.message), code: err && err.gatewayCode } })}\n`);
  process.exit(1);
}
