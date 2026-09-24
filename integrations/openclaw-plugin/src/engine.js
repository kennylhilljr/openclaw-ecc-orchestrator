// Host independent core: tails the event log into the view model, persists
// the read position and view atomically, and submits operator decisions.

import fs from "node:fs/promises";
import path from "node:path";

import { DecisionError, buildDecision, writeDecisionFile } from "./decision.js";
import { applyEvent, emptyState, validId } from "./state.js";
import { emptyCursor, normalizeCursor, readNewEvents } from "./tailer.js";

export const STATE_FILE = "plugin-state.json";
const STATE_VERSION = 1;

async function atomicWriteJson(file, obj) {
  const dir = path.dirname(file);
  const tmp = path.join(dir, `.${path.basename(file)}.${process.pid}.${Date.now()}.tmp`);
  const handle = await fs.open(tmp, "w", 0o600);
  try {
    await handle.writeFile(`${JSON.stringify(obj)}\n`, "utf8");
    await handle.sync();
  } finally {
    await handle.close();
  }
  await fs.rename(tmp, file);
  let dh;
  try {
    dh = await fs.open(dir, "r");
    await dh.sync();
  } catch {
    // best effort
  } finally {
    if (dh) await dh.close();
  }
}

export function createEngine(cfg, { clock = () => Date.now() / 1000, logger = null } = {}) {
  const stateFile = path.join(cfg.stateDir, STATE_FILE);
  let cursor = emptyCursor();
  let state = emptyState();
  let loaded = false;
  let chain = Promise.resolve();

  // Serialize every state mutation (poll, decide) on one promise chain.
  function serial(fn) {
    const next = chain.then(fn, fn);
    chain = next.catch(() => {});
    return next;
  }

  async function load() {
    if (loaded) return;
    await fs.mkdir(cfg.stateDir, { recursive: true, mode: 0o700 });
    try {
      const raw = JSON.parse(await fs.readFile(stateFile, "utf8"));
      if (!raw || raw.version !== STATE_VERSION || raw.eventLog !== cfg.eventLog) throw new Error("state does not match");
      cursor = normalizeCursor(raw.cursor);
      state = raw.state && typeof raw.state === "object" ? { ...emptyState(), ...raw.state } : emptyState();
    } catch (err) {
      if (err && err.code !== "ENOENT") {
        // Keep the unreadable file aside and rebuild from the event log.
        await fs.rename(stateFile, `${stateFile}.unreadable-${Date.now()}`).catch(() => {});
        logger?.warn?.("ecc-orchestrator: plugin state unreadable, rebuilding from the event log");
      }
      cursor = emptyCursor();
      state = emptyState();
    }
    loaded = true;
  }

  async function persist() {
    await atomicWriteJson(stateFile, { version: STATE_VERSION, eventLog: cfg.eventLog, cursor, state });
  }

  async function refreshRuntimeResults() {
    // Report what the runtime did with decisions this plugin submitted.
    const processed = path.join(cfg.inbox, "processed");
    let changed = false;
    for (const a of Object.values(state.approvals)) {
      if (!a.submitted || a.runtime_result || !a.submitted.file) continue;
      const resultFile = path.join(processed, `${a.submitted.file}.result.json`);
      let raw;
      try {
        const st = await fs.lstat(resultFile);
        if (!st.isFile() || st.size > 256 * 1024) continue;
        raw = JSON.parse(await fs.readFile(resultFile, "utf8"));
      } catch {
        continue;
      }
      const failed = Array.isArray(raw.checks) ? raw.checks.filter((c) => c && c.ok === false).map((c) => String(c.name)).slice(0, 10) : [];
      a.runtime_result = { ok: raw.ok === true, failed };
      changed = true;
    }
    return changed;
  }

  async function pollOnceInner() {
    await load();
    let total = 0;
    let changed = false;
    // Drain in bounded chunks.
    for (let i = 0; i < 50; i += 1) {
      const res = await readNewEvents(cfg.eventLog, cursor);
      const moved = res.cursor.offset !== cursor.offset || res.cursor.fileId !== cursor.fileId;
      for (const ev of res.events) applyEvent(state, ev);
      total += res.events.length;
      if (moved || res.events.length) {
        cursor = res.cursor;
        changed = true;
      }
      if (!res.events.length && !moved) break;
      if (!moved) break;
    }
    if (await refreshRuntimeResults()) changed = true;
    if (changed) await persist();
    return total;
  }

  return {
    get state() {
      return state;
    },
    get cursor() {
      return cursor;
    },
    now: clock,
    pollOnce: () => serial(pollOnceInner),
    /**
     * Submit a decision. identity comes from identityFromCommandContext.
     * Returns { file, decision } or throws DecisionError.
     */
    decide: ({ requestId, decision, identity, planPrefix }) =>
      serial(async () => {
        await pollOnceInner();
        if (!validId(requestId)) throw new DecisionError("unknown_request", "invalid approval request id");
        const approval = state.approvals[requestId];
        const obj = buildDecision({ approval, decision, identity, planPrefix, nowSeconds: clock() });
        const file = await writeDecisionFile(cfg.inbox, obj);
        approval.submitted = {
          decision,
          decided_by: identity.decidedBy,
          at: new Date(clock() * 1000).toISOString(),
          file: path.basename(file),
        };
        await persist();
        return { file, decision: obj };
      }),
  };
}
