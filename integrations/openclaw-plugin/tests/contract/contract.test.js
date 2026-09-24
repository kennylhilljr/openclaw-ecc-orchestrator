// Contract test against the real Python runtime.
//
// 1. The runtime's JsonlEventSink and ApprovalBroker write real events.
// 2. The plugin engine tails them and builds decisions exactly as the
//    OpenClaw command would.
// 3. The runtime's DecisionInbox and ApprovalBroker process the plugin's
//    decision file (accepted) and tampered copies (rejected, not consumed).
//
// Requires python3 (3.11+) and git. RUNTIME_SRC overrides the runtime src dir.

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { parseConfig } from "../../src/config.js";
import { identityFromCommandContext, writeDecisionFile } from "../../src/decision.js";
import { createEngine } from "../../src/engine.js";
import { renderPendingText } from "../../src/render.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const runtimeSrc = process.env.RUNTIME_SRC || path.resolve(here, "../../../../src");
const SESSION = "agent:main:main";

function py(script, args) {
  const out = execFileSync("python3", [path.join(here, "py", script), ...args], {
    env: { PATH: process.env.PATH, PYTHONPATH: runtimeSrc, PYTHONDONTWRITEBYTECODE: "1" },
    encoding: "utf8",
  });
  return JSON.parse(out);
}

test("plugin decisions round trip through the Python ApprovalBroker and DecisionInbox", async () => {
  const work = await fs.mkdtemp(path.join(os.tmpdir(), "ecc-contract-"));
  try {
    const gen = py("gen_events.py", [work, SESSION]);
    const cfg = parseConfig({
      eventLog: gen.event_log,
      inbox: path.join(work, "state", "decisions"),
      stateDir: path.join(work, "plugin-state"),
    });
    const engine = createEngine(cfg);
    const n = await engine.pollOnce();
    assert.ok(n >= 7, `expected runtime events, got ${n}`);

    const approval = engine.state.approvals[gen.request_id];
    assert.ok(approval, "approval.requested surfaced");
    assert.equal(approval.status, "pending");
    assert.equal(approval.plan_sha256, gen.plan_sha256, "plan hash verbatim");
    assert.equal(approval.head_sha, gen.head_sha, "head sha verbatim");
    assert.equal(approval.requesting_session, SESSION);
    assert.equal(engine.state.runs.run1.units.alpha.state, "reviewing");
    assert.equal(engine.state.attention[0].reason, "out_of_scope_changes");
    const progress = engine.state.runs.run1.units.alpha.progress[0].message;
    assert.ok(!progress.includes(gen.fake_token), "runtime and plugin redaction hide the token");
    assert.ok(!renderPendingText(engine.state, Date.now() / 1000).includes(gen.fake_token));

    // Expired request is refused by the plugin.
    const identity = identityFromCommandContext({ isAuthorizedSender: true, senderId: "owner-profile-1", sessionKey: SESSION });
    await assert.rejects(
      engine.decide({ requestId: gen.expired_request_id, decision: "rejected", identity }),
      (err) => err.code === "expired",
    );
    await assert.rejects(engine.decide({ requestId: "apr-unknown", decision: "rejected", identity }), (err) => err.code === "unknown_request");

    // Tampered copies of the genuine decision, written first so the runtime
    // processes them before the genuine file (name order).
    const genuine = {
      request_id: approval.request_id,
      run_id: approval.run_id,
      unit_id: approval.unit_id,
      action: approval.action,
      plan_sha256: approval.plan_sha256,
      session_id: approval.requesting_session,
      decision: "approved",
      decided_by: identity.decidedBy,
    };
    const tampered = [
      { ...genuine, plan_sha256: "0".repeat(64) },
      { ...genuine, session_id: "agent:main:intruder" },
      { ...genuine, unit_id: "beta" },
    ];
    for (const t of tampered) await writeDecisionFile(cfg.inbox, t, { now: 0 });

    const { file } = await engine.decide({
      requestId: gen.request_id,
      decision: "approved",
      identity,
      planPrefix: approval.plan_sha256.slice(0, 12),
    });
    const written = JSON.parse(await fs.readFile(file, "utf8"));
    assert.deepEqual(written, genuine, "plugin decision equals the verbatim binding plus decision and decided_by");

    const out = py("process_inbox.py", [gen.approvals_state, gen.event_log, cfg.inbox]);
    assert.equal(out.results.length, 4);
    for (const r of out.results.slice(0, 3)) {
      assert.equal(r.ok, false, `tampered ${r.file} must be rejected`);
      assert.ok(r.failed.includes("binding_matches"), r.failed.join(","));
    }
    assert.equal(out.results[3].ok, true, `genuine decision accepted: ${out.results[3].failed}`);
    assert.equal(out.results[3].file, path.basename(file));
    assert.equal(out.requests[gen.request_id].status, "approved");
    assert.equal(out.requests[gen.request_id].decided_by, "owner-profile-1");
    assert.equal(out.rejected_attempts, 3);

    // Replaying the genuine decision is rejected by the runtime.
    await fs.copyFile(path.join(cfg.inbox, "processed", path.basename(file)), path.join(cfg.inbox, `replay-${path.basename(file)}`));
    const replay = py("process_inbox.py", [gen.approvals_state, gen.event_log, cfg.inbox]);
    assert.equal(replay.results[0].ok, false);
    assert.ok(replay.results[0].failed.includes("request_pending"));

    // The plugin sees approval.resolved and the runtime's result envelope.
    await engine.pollOnce();
    const after = engine.state.approvals[gen.request_id];
    assert.equal(after.status, "approved");
    assert.equal(after.decided_by, "owner-profile-1");
    assert.deepEqual(after.runtime_result, { ok: true, failed: [] });
  } finally {
    await fs.rm(work, { recursive: true, force: true });
  }
});
