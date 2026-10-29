// Drive the real plugin engine from another process, the way the OpenClaw
// gateway does, for the Python round-trip tests (tests/test_roundtrip_openclaw.py).
//
// Usage:
//   node plugin-driver.mjs <eventLog> <inbox> <stateDir> snapshot
//   node plugin-driver.mjs <eventLog> <inbox> <stateDir> decide <requestId> <approved|rejected> <planPrefix|-> <decidedBy> <sessionKey>
//
// snapshot prints exactly what the ecc-orchestrator.snapshot gateway method
// returns. decide goes through engine.decide (the same path as /ecc-decide
// and the ecc-orchestrator.decide method once identity is established) and
// prints {"ok":true,"file":...} or {"ok":false,"code":...}. Each call is a new
// engine instance on the same stateDir, like a gateway restart.

import path from "node:path";

import { parseConfig } from "../../src/config.js";
import { DecisionError } from "../../src/decision.js";
import { createEngine } from "../../src/engine.js";
import { snapshot } from "../../src/render.js";

const [eventLog, inbox, stateDir, cmd, ...rest] = process.argv.slice(2);
const cfg = parseConfig({ eventLog, inbox, stateDir });
const engine = createEngine(cfg);

function out(obj) {
  process.stdout.write(`${JSON.stringify(obj)}\n`);
}

if (cmd === "snapshot") {
  await engine.pollOnce();
  out(snapshot(engine.state, engine.now()));
} else if (cmd === "decide") {
  const [requestId, decision, planPrefix, decidedBy, sessionKey] = rest;
  try {
    const { file } = await engine.decide({
      requestId,
      decision,
      identity: { decidedBy, sessionKey },
      planPrefix: planPrefix === "-" ? undefined : planPrefix,
    });
    out({ ok: true, file: path.basename(file) });
  } catch (err) {
    if (!(err instanceof DecisionError)) throw err;
    out({ ok: false, code: err.code });
  }
} else {
  process.stderr.write("usage: plugin-driver.mjs <eventLog> <inbox> <stateDir> snapshot|decide ...\n");
  process.exit(2);
}
