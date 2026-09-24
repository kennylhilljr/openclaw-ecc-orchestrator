import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { test } from "node:test";

import { emptyCursor, readNewEvents } from "../../src/tailer.js";
import { appendLines, ev, rmTemp, tempDir } from "./helpers.js";

test("reads complete lines in seq order and advances the offset", async () => {
  const dir = await tempDir();
  try {
    const file = path.join(dir, "events.jsonl");
    await appendLines(file, [ev("unit.progress", { seq: 2, data: { message: "b" } }), ev("run.created", { seq: 1, data: { plan_sha256: "a".repeat(64), unit_ids: [] } })]);
    const res = await readNewEvents(file, emptyCursor());
    assert.deepEqual(res.events.map((e) => e.seq), [1, 2]);
    assert.equal(res.cursor.lastSeq, 2);
    assert.equal(res.cursor.offset, (await fs.stat(file)).size);
    const again = await readNewEvents(file, res.cursor);
    assert.equal(again.events.length, 0);
  } finally {
    await rmTemp(dir);
  }
});

test("tolerates a partial last line and picks it up once complete", async () => {
  const dir = await tempDir();
  try {
    const file = path.join(dir, "events.jsonl");
    const full = ev("unit.progress", { seq: 1, data: { message: "one" } });
    const torn = JSON.stringify(ev("unit.progress", { seq: 2, data: { message: "two" } }));
    await fs.writeFile(file, `${JSON.stringify(full)}\n${torn.slice(0, 20)}`);
    const first = await readNewEvents(file, emptyCursor());
    assert.deepEqual(first.events.map((e) => e.seq), [1]);
    assert.equal(first.cursor.offset, Buffer.byteLength(JSON.stringify(full)) + 1);
    // Nothing new while the tail is still torn.
    const mid = await readNewEvents(file, first.cursor);
    assert.equal(mid.events.length, 0);
    assert.equal(mid.cursor.offset, first.cursor.offset);
    await fs.appendFile(file, `${torn.slice(20)}\n`);
    const second = await readNewEvents(file, first.cursor);
    assert.deepEqual(second.events.map((e) => e.seq), [2]);
  } finally {
    await rmTemp(dir);
  }
});

test("skips malformed lines, non envelopes and keeps unknown types for the reducer", async () => {
  const dir = await tempDir();
  try {
    const file = path.join(dir, "events.jsonl");
    await appendLines(file, [
      "{not json",
      JSON.stringify({ hello: "world" }),
      JSON.stringify([1, 2, 3]),
      ev("future.type", { seq: 1, data: {} }),
      ev("unit.progress", { seq: 2, data: { message: "ok" } }),
    ]);
    const res = await readNewEvents(file, emptyCursor());
    assert.deepEqual(res.events.map((e) => e.type), ["future.type", "unit.progress"]);
    assert.equal(res.stats.malformed, 3);
  } finally {
    await rmTemp(dir);
  }
});

test("dedupes by id and ignores seq at or below the last processed seq", async () => {
  const dir = await tempDir();
  try {
    const file = path.join(dir, "events.jsonl");
    const a = ev("unit.progress", { seq: 1, id: "dup1", data: { message: "a" } });
    await appendLines(file, [a, a, ev("unit.progress", { seq: 3, id: "e3", data: { message: "c" } })]);
    const res = await readNewEvents(file, emptyCursor());
    assert.deepEqual(res.events.map((e) => e.id), ["dup1", "e3"]);
    assert.equal(res.stats.duplicates, 1);
    // A late event with a lower seq than already processed is stale.
    await appendLines(file, [ev("unit.progress", { seq: 2, id: "late", data: { message: "b" } }), a]);
    const res2 = await readNewEvents(file, res.cursor);
    assert.equal(res2.events.length, 0);
    assert.equal(res2.stats.stale, 1);
    assert.equal(res2.stats.duplicates, 1);
  } finally {
    await rmTemp(dir);
  }
});

test("restart from a persisted cursor does not replay; rotation is re-read without replay", async () => {
  const dir = await tempDir();
  try {
    const file = path.join(dir, "events.jsonl");
    const e1 = ev("unit.progress", { seq: 1, id: "r1", data: { message: "a" } });
    const e2 = ev("unit.progress", { seq: 2, id: "r2", data: { message: "b" } });
    await appendLines(file, [e1, e2]);
    const first = await readNewEvents(file, emptyCursor());
    const persisted = JSON.parse(JSON.stringify(first.cursor));
    const restarted = await readNewEvents(file, persisted);
    assert.equal(restarted.events.length, 0);
    // Rotate: a new file with the old content plus one new event.
    const rotated = path.join(dir, "events.new");
    await appendLines(rotated, [e1, e2, ev("unit.progress", { seq: 3, id: "r3", data: { message: "c" } })]);
    await fs.rename(rotated, file);
    const after = await readNewEvents(file, persisted);
    assert.equal(after.stats.reset, true);
    assert.deepEqual(after.events.map((e) => e.id), ["r3"]);
  } finally {
    await rmTemp(dir);
  }
});

test("a missing event log is not an error", async () => {
  const dir = await tempDir();
  try {
    const res = await readNewEvents(path.join(dir, "absent.jsonl"), emptyCursor());
    assert.equal(res.events.length, 0);
    assert.equal(res.stats.missing, true);
  } finally {
    await rmTemp(dir);
  }
});
