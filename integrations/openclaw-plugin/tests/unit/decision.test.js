import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { test } from "node:test";

import {
  DecisionError,
  buildDecision,
  identityFromCommandContext,
  identityFromGatewayClient,
  writeDecisionFile,
} from "../../src/decision.js";
import { applyEvent, emptyState } from "../../src/state.js";
import { HEAD, PLAN, SESSION, approvalRequested, rmTemp, tempDir } from "./helpers.js";

const NOW = 1_900_000_000;

function pendingApproval(overrides = {}) {
  const s = emptyState();
  applyEvent(s, approvalRequested(1, overrides));
  return s.approvals[overrides.request_id ?? "apr-1"];
}

const ctx = (extra = {}) => ({ isAuthorizedSender: true, senderId: "owner-profile-1", sessionKey: SESSION, ...extra });

function code(fn) {
  try {
    fn();
  } catch (err) {
    assert.ok(err instanceof DecisionError, String(err));
    return err.code;
  }
  assert.fail("expected DecisionError");
}

test("identity comes only from the OpenClaw command context", () => {
  const saved = { USER: process.env.USER, LOGNAME: process.env.LOGNAME };
  process.env.USER = "os-user";
  process.env.LOGNAME = "os-user";
  try {
    assert.deepEqual(identityFromCommandContext(ctx()), { decidedBy: "owner-profile-1", sessionKey: SESSION });
    assert.equal(code(() => identityFromCommandContext(ctx({ senderId: undefined }))), "missing_identity");
    assert.equal(code(() => identityFromCommandContext(ctx({ senderId: "  " }))), "missing_identity");
    assert.equal(code(() => identityFromCommandContext(ctx({ sessionKey: undefined }))), "missing_session");
    assert.equal(code(() => identityFromCommandContext(ctx({ isAuthorizedSender: false }))), "unauthorized");
    assert.equal(code(() => identityFromCommandContext(ctx({ senderId: "a@b.example" }))), "invalid_identity");
    assert.equal(code(() => identityFromCommandContext(ctx({ senderId: "bad handle" }))), "invalid_identity");
    assert.equal(code(() => identityFromCommandContext(null)), "missing_identity");
    // OpenClaw reports the client program id as sender for non UI clients.
    assert.equal(code(() => identityFromCommandContext(ctx({ senderId: "cli" }))), "invalid_identity");
    assert.equal(code(() => identityFromCommandContext(ctx({ senderId: "gateway-client" }))), "invalid_identity");
  } finally {
    process.env.USER = saved.USER;
    process.env.LOGNAME = saved.LOGNAME;
  }
});

test("decision copies binding fields verbatim and adds decision and decided_by", () => {
  const approval = pendingApproval();
  const identity = identityFromCommandContext(ctx());
  const d = buildDecision({ approval, decision: "approved", identity, planPrefix: PLAN.slice(0, 12), nowSeconds: NOW });
  assert.deepEqual(d, {
    request_id: "apr-1",
    run_id: "run1",
    unit_id: "alpha",
    action: "merge",
    plan_sha256: PLAN,
    session_id: SESSION,
    decision: "approved",
    decided_by: "owner-profile-1",
  });
  assert.ok(!("head_sha" in d), "a decision never sets the head sha");
  const r = buildDecision({ approval, decision: "rejected", identity, nowSeconds: NOW });
  assert.equal(r.decision, "rejected");
  assert.equal(approval.head_sha, HEAD, "binding unchanged");
});

test("refusal cases: unknown, expired, not pending, submitted, wrong session, missing identity, plan confirmation", () => {
  const identity = { decidedBy: "owner-profile-1", sessionKey: SESSION };
  const planPrefix = PLAN.slice(0, 8);
  assert.equal(code(() => buildDecision({ approval: undefined, decision: "approved", identity, planPrefix, nowSeconds: NOW })), "unknown_request");
  assert.equal(
    code(() => buildDecision({ approval: pendingApproval({ expires_at: NOW - 1 }), decision: "approved", identity, planPrefix, nowSeconds: NOW })),
    "expired",
  );
  const resolved = { ...pendingApproval(), status: "approved" };
  assert.equal(code(() => buildDecision({ approval: resolved, decision: "approved", identity, planPrefix, nowSeconds: NOW })), "not_pending");
  const submitted = { ...pendingApproval(), submitted: { decision: "approved" } };
  assert.equal(code(() => buildDecision({ approval: submitted, decision: "approved", identity, planPrefix, nowSeconds: NOW })), "already_submitted");
  assert.equal(
    code(() => buildDecision({ approval: pendingApproval(), decision: "approved", identity: { ...identity, sessionKey: "agent:main:other" }, planPrefix, nowSeconds: NOW })),
    "session_mismatch",
  );
  assert.equal(
    code(() => buildDecision({ approval: pendingApproval(), decision: "approved", identity: { sessionKey: SESSION }, planPrefix, nowSeconds: NOW })),
    "missing_identity",
  );
  assert.equal(
    code(() => buildDecision({ approval: pendingApproval(), decision: "approved", identity, planPrefix: undefined, nowSeconds: NOW })),
    "plan_confirmation_required",
  );
  assert.equal(
    code(() => buildDecision({ approval: pendingApproval(), decision: "approved", identity, planPrefix: "deadbeef", nowSeconds: NOW })),
    "plan_confirmation_mismatch",
  );
  assert.equal(code(() => buildDecision({ approval: pendingApproval(), decision: "maybe", identity, nowSeconds: NOW })), "invalid_decision");
  const invalid = pendingApproval({ plan_sha256: "not-a-hash" });
  assert.equal(invalid.status, "invalid");
  assert.equal(code(() => buildDecision({ approval: invalid, decision: "rejected", identity, nowSeconds: NOW })), "invalid_request");
  const noSession = pendingApproval({ requesting_session: null });
  assert.equal(code(() => buildDecision({ approval: noSession, decision: "rejected", identity, nowSeconds: NOW })), "no_requesting_session");
});

test("a later approval.requested event never overwrites binding fields", () => {
  const s = emptyState();
  applyEvent(s, approvalRequested(1));
  applyEvent(s, approvalRequested(2, { plan_sha256: "f".repeat(64), requesting_session: "agent:evil:x" }));
  assert.equal(s.approvals["apr-1"].plan_sha256, PLAN);
  assert.equal(s.approvals["apr-1"].requesting_session, SESSION);
});

test("decision files are written atomically as *.json with no leftover temp files", async () => {
  const dir = await tempDir();
  try {
    const inbox = path.join(dir, "inbox");
    const decision = buildDecision({
      approval: pendingApproval(),
      decision: "rejected",
      identity: { decidedBy: "owner-profile-1", sessionKey: SESSION },
      nowSeconds: NOW,
    });
    const file = await writeDecisionFile(inbox, decision, { now: 42 });
    assert.match(path.basename(file), /^42-apr-1-[0-9a-f]{12}\.json$/);
    const names = await fs.readdir(inbox);
    assert.deepEqual(names, [path.basename(file)]);
    assert.deepEqual(JSON.parse(await fs.readFile(file, "utf8")), decision);
    const mode = (await fs.stat(file)).mode & 0o777;
    assert.equal(mode, 0o600);
  } finally {
    await rmTemp(dir);
  }
});

test("writing refuses a symlinked inbox", async () => {
  const dir = await tempDir();
  try {
    const real = path.join(dir, "real");
    await fs.mkdir(real);
    const link = path.join(dir, "inbox");
    await fs.symlink(real, link);
    const decision = buildDecision({
      approval: pendingApproval(),
      decision: "rejected",
      identity: { decidedBy: "owner-profile-1", sessionKey: SESSION },
      nowSeconds: NOW,
    });
    await assert.rejects(writeDecisionFile(link, decision), (err) => err.code === "bad_inbox");
    assert.deepEqual(await fs.readdir(real), []);
  } finally {
    await rmTemp(dir);
  }
});

test("gateway method identity comes only from the authenticated OpenClaw profile", () => {
  const client = { authenticatedUserProfile: { profileId: "prof-7f3a", displayName: "Kenny" }, connect: { client: { id: "cli" } } };
  assert.deepEqual(identityFromGatewayClient(client, { sessionKey: SESSION }), { decidedBy: "prof-7f3a", sessionKey: SESSION });
  // Request data can never supply the identity.
  assert.equal(code(() => identityFromGatewayClient({}, { sessionKey: SESSION, decidedBy: "someone", profileId: "x" })), "missing_identity");
  assert.equal(code(() => identityFromGatewayClient(null, { sessionKey: SESSION })), "missing_identity");
  assert.equal(code(() => identityFromGatewayClient({ ...client, invalidated: true }, { sessionKey: SESSION })), "missing_identity");
  assert.equal(code(() => identityFromGatewayClient({ ...client, internal: { syntheticClient: true } }, { sessionKey: SESSION })), "missing_identity");
  assert.equal(code(() => identityFromGatewayClient({ ...client, internal: { agentToolCaller: {} } }, { sessionKey: SESSION })), "missing_identity");
  assert.equal(code(() => identityFromGatewayClient(client, {})), "missing_session");
  assert.equal(code(() => identityFromGatewayClient({ authenticatedUserProfile: { profileId: "a@b.example" } }, { sessionKey: SESSION })), "invalid_identity");
});
