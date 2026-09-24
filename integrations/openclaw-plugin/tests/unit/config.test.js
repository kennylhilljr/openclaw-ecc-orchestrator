import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { test } from "node:test";

import { ConfigError, parseConfig, validatePath } from "../../src/config.js";
import { rmTemp, tempDir } from "./helpers.js";

test("paths must be absolute, traversal free and outside ~/.openclaw, .openclaw and .git", async () => {
  const home = await tempDir("ecc-home-");
  try {
    await fs.mkdir(path.join(home, ".openclaw"));
    const ok = path.join(home, "state", "events.jsonl");
    assert.equal(validatePath(ok, "x", { home }), ok);
    for (const bad of [
      "relative/path",
      "",
      `${home}/a/../b`,
      `${home}/./b`,
      `${home}/.openclaw/plugins/x`,
      `${home}/.openclaw`,
      "/srv/other/.openclaw/x",
      "/srv/repo/.git/x",
      `${home}/a\0b`,
    ]) {
      assert.throws(() => validatePath(bad, "x", { home }), ConfigError, bad);
    }
    // A symlink that leads into ~/.openclaw is refused too.
    await fs.symlink(path.join(home, ".openclaw"), path.join(home, "sneaky"));
    assert.throws(() => validatePath(path.join(home, "sneaky", "inbox"), "x", { home }), ConfigError);
  } finally {
    await rmTemp(home);
  }
});

test("parseConfig requires eventLog, inbox and stateDir and rejects unknown keys", async () => {
  const home = await tempDir("ecc-home-");
  try {
    const base = { eventLog: `${home}/s/events.jsonl`, inbox: `${home}/s/decisions`, stateDir: `${home}/plugin` };
    const cfg = parseConfig(base, { home });
    assert.equal(cfg.pollIntervalMs, 1000);
    assert.equal(cfg.runtimeCli, null);
    assert.throws(() => parseConfig({ ...base, autoApprove: true }, { home }), /unknown config key/);
    assert.throws(() => parseConfig({ eventLog: base.eventLog, inbox: base.inbox }, { home }), ConfigError);
    assert.throws(() => parseConfig({ ...base, stateDir: `${home}/s/decisions/x` }, { home }), /separate/);
    assert.throws(() => parseConfig({ ...base, pollIntervalMs: 5 }, { home }), /pollIntervalMs/);
    assert.throws(() => parseConfig({ ...base, runtimeCli: "ecc-orchestrator" }, { home }), /absolute/);
    assert.throws(() => parseConfig({ ...base, runtimeCliArgs: ["../x"] }, { home }), /\.\./);
    const withCli = parseConfig({ ...base, runtimeCli: "/usr/bin/python3", runtimeCliArgs: ["-m", "openclaw_ecc_orchestrator"] }, { home });
    assert.deepEqual(withCli.runtimeCliArgs, ["-m", "openclaw_ecc_orchestrator"]);
    assert.throws(() => parseConfig(null, { home }), ConfigError);
  } finally {
    await rmTemp(home);
  }
});
