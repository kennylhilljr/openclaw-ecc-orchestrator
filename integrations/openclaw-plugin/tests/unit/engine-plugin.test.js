import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { test } from "node:test";

import plugin, { DECIDE_COMMAND, DECIDE_METHOD, PANEL_PATH, READ_COMMAND, SNAPSHOT_METHOD } from "../../index.js";
import { parseConfig } from "../../src/config.js";
import { createEngine } from "../../src/engine.js";
import { PLAN, SESSION, appendLines, approvalRequested, ev, rmTemp, tempDir } from "./helpers.js";

async function setup() {
  const dir = await tempDir();
  const cfg = parseConfig({
    eventLog: path.join(dir, "state", "openclaw-events.jsonl"),
    inbox: path.join(dir, "state", "decisions"),
    stateDir: path.join(dir, "plugin"),
  });
  await fs.mkdir(path.join(dir, "state"), { recursive: true });
  return { dir, cfg };
}

test("engine persists position and state so a restart does not replay", async () => {
  const { dir, cfg } = await setup();
  try {
    await appendLines(cfg.eventLog, [ev("unit.progress", { seq: 1, data: { message: "a" } }), approvalRequested(2)]);
    const e1 = createEngine(cfg, { clock: () => 1_900_000_000 });
    assert.equal(await e1.pollOnce(), 2);
    assert.equal(e1.state.approvals["apr-1"].status, "pending");
    const e2 = createEngine(cfg, { clock: () => 1_900_000_000 });
    assert.equal(await e2.pollOnce(), 0, "no replay after restart");
    assert.equal(e2.state.approvals["apr-1"].plan_sha256, PLAN, "state restored");
    await appendLines(cfg.eventLog, [ev("approval.resolved", { seq: 3, data: { request_id: "apr-1", decision: "approved", decided_by: "owner-profile-1" } })]);
    assert.equal(await e2.pollOnce(), 1);
    assert.equal(e2.state.approvals["apr-1"].status, "approved");
  } finally {
    await rmTemp(dir);
  }
});

test("an unreadable state file is set aside and the view is rebuilt from the log", async () => {
  const { dir, cfg } = await setup();
  try {
    await appendLines(cfg.eventLog, [approvalRequested(1)]);
    await fs.mkdir(cfg.stateDir, { recursive: true });
    await fs.writeFile(path.join(cfg.stateDir, "plugin-state.json"), "{garbage");
    const e = createEngine(cfg, { clock: () => 1_900_000_000 });
    assert.equal(await e.pollOnce(), 1);
    const names = await fs.readdir(cfg.stateDir);
    assert.ok(names.some((n) => n.startsWith("plugin-state.json.unreadable-")));
  } finally {
    await rmTemp(dir);
  }
});

function fakeApi(pluginConfig) {
  const reg = { commands: {}, methods: {}, routes: {}, services: [], descriptors: [], warnings: [] };
  const api = {
    id: "ecc-orchestrator",
    pluginConfig,
    logger: { info() {}, warn: (m) => reg.warnings.push(m), error() {} },
    registerService: (s) => reg.services.push(s),
    registerCommand: (c) => (reg.commands[c.name] = c),
    registerGatewayMethod: (name, handler, opts) => (reg.methods[name] = { handler, opts }),
    registerHttpRoute: (r) => (reg.routes[r.path] = r),
    session: { controls: { registerControlUiDescriptor: (d) => reg.descriptors.push(d) } },
  };
  return { api, reg };
}

test("plugin registers commands, gateway method, panel route, tab and service; no auto approve", async () => {
  const { dir, cfg } = await setup();
  try {
    const { api, reg } = fakeApi({ eventLog: cfg.eventLog, inbox: cfg.inbox, stateDir: cfg.stateDir });
    plugin.register(api);
    assert.ok(reg.commands[READ_COMMAND]);
    assert.ok(reg.commands[DECIDE_COMMAND]);
    assert.deepEqual(reg.commands[DECIDE_COMMAND].requiredScopes, ["operator.approvals"]);
    assert.equal(reg.commands[DECIDE_COMMAND].requireAuth, true);
    assert.equal(reg.methods[SNAPSHOT_METHOD].opts.scope, "operator.read");
    assert.equal(reg.methods[DECIDE_METHOD].opts.scope, "operator.approvals");
    assert.equal(reg.routes[PANEL_PATH].auth, "gateway");
    assert.equal(reg.descriptors[0].surface, "tab");
    assert.equal(reg.descriptors[0].path, PANEL_PATH);
    assert.equal(reg.services.length, 1);

    await appendLines(cfg.eventLog, [approvalRequested(1)]);
    // Reading never decides.
    const pending = await reg.commands[READ_COMMAND].handler({ args: "pending", isAuthorizedSender: true, senderId: "u1", sessionKey: SESSION });
    assert.match(pending.text, /apr-1/);
    assert.deepEqual(await fs.readdir(cfg.inbox).catch(() => []), []);

    // Wrong session is refused and writes nothing.
    const wrong = await reg.commands[DECIDE_COMMAND].handler({
      args: `approve apr-1 ${PLAN.slice(0, 12)}`,
      isAuthorizedSender: true,
      senderId: "owner-profile-1",
      sessionKey: "agent:main:other",
    });
    assert.match(wrong.text, /session_mismatch/);
    assert.deepEqual(await fs.readdir(cfg.inbox).catch(() => []), []);

    // Missing identity is refused.
    const anon = await reg.commands[DECIDE_COMMAND].handler({ args: `approve apr-1 ${PLAN.slice(0, 12)}`, isAuthorizedSender: true, sessionKey: SESSION });
    assert.match(anon.text, /missing_identity/);

    const ok = await reg.commands[DECIDE_COMMAND].handler({
      args: `approve apr-1 ${PLAN.slice(0, 12)}`,
      isAuthorizedSender: true,
      senderId: "owner-profile-1",
      sessionKey: SESSION,
    });
    assert.match(ok.text, /written to the runtime inbox/);
    const files = (await fs.readdir(cfg.inbox)).filter((n) => n.endsWith(".json"));
    assert.equal(files.length, 1);
    const written = JSON.parse(await fs.readFile(path.join(cfg.inbox, files[0]), "utf8"));
    assert.equal(written.decided_by, "owner-profile-1");
    assert.equal(written.session_id, SESSION);

    const again = await reg.commands[DECIDE_COMMAND].handler({
      args: "reject apr-1",
      isAuthorizedSender: true,
      senderId: "owner-profile-1",
      sessionKey: SESSION,
    });
    assert.match(again.text, /already_submitted/);

    // Snapshot and panel.
    let snap;
    await reg.methods[SNAPSHOT_METHOD].handler({ respond: (ok2, payload) => (snap = { ok: ok2, payload }) });
    assert.equal(snap.ok, true);
    assert.equal(snap.payload.pending_approvals[0].submitted.decided_by, "owner-profile-1");
    const res = { headers: {}, setHeader(k, v) { this.headers[k] = v; }, end(b) { this.body = b; } };
    await reg.routes[PANEL_PATH].handler({ method: "GET" }, res);
    assert.equal(res.statusCode, 200);
    assert.match(res.body, /apr-1/);
    const post = { headers: {}, setHeader(k, v) { this.headers[k] = v; }, end() {} };
    await reg.routes[PANEL_PATH].handler({ method: "POST" }, post);
    assert.equal(post.statusCode, 405);
  } finally {
    await rmTemp(dir);
  }
});

test("invalid config registers only a read command that explains the problem", async () => {
  const { api, reg } = fakeApi({ eventLog: "relative.jsonl", inbox: "/x", stateDir: "/y" });
  plugin.register(api);
  assert.deepEqual(Object.keys(reg.commands), [READ_COMMAND]);
  assert.equal(reg.services.length, 0);
  const out = await reg.commands[READ_COMMAND].handler({});
  assert.match(out.text, /not configured/);
  assert.equal(reg.warnings.length, 1);
});

test("decide gateway method uses the authenticated profile and refuses without one", async () => {
  const { dir, cfg } = await setup();
  try {
    const { api, reg } = fakeApi({ eventLog: cfg.eventLog, inbox: cfg.inbox, stateDir: cfg.stateDir });
    plugin.register(api);
    await appendLines(cfg.eventLog, [approvalRequested(1)]);
    const call = async (params, client) => {
      let out;
      await reg.methods[DECIDE_METHOD].handler({ params, client, respond: (ok, payload, error) => (out = { ok, payload, error }) });
      return out;
    };
    const base = { requestId: "apr-1", decision: "approve", planHashPrefix: PLAN.slice(0, 10), sessionKey: SESSION };
    const anon = await call(base, { connect: {} });
    assert.equal(anon.ok, false);
    assert.equal(anon.error.details.reason, "missing_identity");
    const spoof = await call({ ...base, decided_by: "mallory" }, { authenticatedUserProfile: { profileId: "prof-1" } });
    assert.equal(spoof.ok, true);
    assert.equal(spoof.payload.decided_by, "prof-1", "request data never sets decided_by");
    const files = (await fs.readdir(cfg.inbox)).filter((n) => n.endsWith(".json"));
    const written = JSON.parse(await fs.readFile(path.join(cfg.inbox, files[0]), "utf8"));
    assert.equal(written.decided_by, "prof-1");
    assert.ok(!("decidedBy" in written));
    const again = await call(base, { authenticatedUserProfile: { profileId: "prof-1" } });
    assert.equal(again.error.details.reason, "already_submitted");
  } finally {
    await rmTemp(dir);
  }
});
