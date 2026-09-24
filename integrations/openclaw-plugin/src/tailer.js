// Incremental reader for the runtime's append-only JSONL event log.
//
// * Reads from a persisted byte offset; only complete, newline terminated
//   lines are consumed, so a torn last line is retried on the next poll.
// * Lines that fail to parse or do not have the event envelope are skipped.
// * Events are deduplicated by `id` and must have `seq` above the last
//   processed seq, then returned in `seq` order.
// * A rotated or truncated file (different inode, or smaller than the offset)
//   is re-read from the start; dedupe and lastSeq prevent replays.

import fs from "node:fs/promises";

export const MAX_LINE_BYTES = 1024 * 1024;
export const MAX_READ_BYTES = 4 * 1024 * 1024;
export const MAX_SEEN_IDS = 5000;

const ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

export function emptyCursor() {
  return { offset: 0, lastSeq: 0, fileId: null, seenIds: [] };
}

export function normalizeCursor(raw) {
  const c = emptyCursor();
  if (!raw || typeof raw !== "object") return c;
  if (Number.isSafeInteger(raw.offset) && raw.offset >= 0) c.offset = raw.offset;
  if (Number.isSafeInteger(raw.lastSeq) && raw.lastSeq >= 0) c.lastSeq = raw.lastSeq;
  if (typeof raw.fileId === "string") c.fileId = raw.fileId;
  if (Array.isArray(raw.seenIds)) c.seenIds = raw.seenIds.filter((x) => typeof x === "string").slice(-MAX_SEEN_IDS);
  return c;
}

/** Minimal envelope check; payload validation happens in the reducer. */
export function isEnvelope(obj) {
  return (
    obj !== null &&
    typeof obj === "object" &&
    !Array.isArray(obj) &&
    typeof obj.type === "string" &&
    obj.type.length > 0 &&
    obj.type.length <= 128 &&
    typeof obj.id === "string" &&
    ID_RE.test(obj.id) &&
    Number.isSafeInteger(obj.seq) &&
    obj.seq > 0 &&
    typeof obj.run_id === "string" &&
    obj.data !== null &&
    typeof obj.data === "object" &&
    !Array.isArray(obj.data)
  );
}

/**
 * Read new events. Returns { events, cursor, stats } without mutating the
 * input cursor; the caller persists the returned cursor together with the
 * state derived from the events.
 */
export async function readNewEvents(filePath, cursorIn, { maxReadBytes = MAX_READ_BYTES } = {}) {
  const cursor = normalizeCursor(cursorIn);
  const stats = { linesRead: 0, parsed: 0, malformed: 0, duplicates: 0, stale: 0, oversized: 0, reset: false };
  let handle;
  try {
    handle = await fs.open(filePath, "r");
  } catch (err) {
    if (err && err.code === "ENOENT") return { events: [], cursor, stats: { ...stats, missing: true } };
    throw err;
  }
  try {
    const st = await handle.stat();
    if (!st.isFile()) throw new Error("event log is not a regular file");
    const fileId = `${st.dev}:${st.ino}`;
    let offset = cursor.offset;
    if ((cursor.fileId && cursor.fileId !== fileId) || st.size < offset) {
      offset = 0;
      stats.reset = true;
    }
    const available = st.size - offset;
    if (available <= 0) {
      return { events: [], cursor: { ...cursor, offset, fileId }, stats };
    }
    const toRead = Math.min(available, maxReadBytes);
    const buf = Buffer.alloc(toRead);
    const { bytesRead } = await handle.read(buf, 0, toRead, offset);
    const chunk = buf.subarray(0, bytesRead);
    let lastNewline = chunk.lastIndexOf(0x0a);
    let consumeUpTo;
    if (lastNewline === -1) {
      if (bytesRead >= MAX_LINE_BYTES) {
        // A single line larger than the limit: skip past it rather than stall.
        stats.oversized += 1;
        return { events: [], cursor: { ...cursor, offset: offset + bytesRead, fileId }, stats };
      }
      // Only a partial line so far (torn tail): wait for more data.
      return { events: [], cursor: { ...cursor, offset, fileId }, stats };
    }
    consumeUpTo = lastNewline + 1;
    const text = chunk.subarray(0, consumeUpTo).toString("utf8");
    const seen = new Set(cursor.seenIds);
    const accepted = [];
    for (const line of text.split("\n")) {
      if (line.trim() === "") continue;
      stats.linesRead += 1;
      if (Buffer.byteLength(line, "utf8") > MAX_LINE_BYTES) {
        stats.oversized += 1;
        continue;
      }
      let obj;
      try {
        obj = JSON.parse(line);
      } catch {
        stats.malformed += 1;
        continue;
      }
      if (!isEnvelope(obj)) {
        stats.malformed += 1;
        continue;
      }
      stats.parsed += 1;
      if (seen.has(obj.id)) {
        stats.duplicates += 1;
        continue;
      }
      if (obj.seq <= cursor.lastSeq) {
        stats.stale += 1;
        continue;
      }
      seen.add(obj.id);
      accepted.push(obj);
    }
    accepted.sort((a, b) => a.seq - b.seq);
    // Drop duplicate seq values inside one batch (keep the first by file order).
    const events = [];
    let prevSeq = cursor.lastSeq;
    for (const ev of accepted) {
      if (ev.seq <= prevSeq) {
        stats.stale += 1;
        continue;
      }
      events.push(ev);
      prevSeq = ev.seq;
    }
    const seenIds = [...cursor.seenIds, ...events.map((e) => e.id)].slice(-MAX_SEEN_IDS);
    return {
      events,
      cursor: { offset: offset + consumeUpTo, lastSeq: prevSeq, fileId, seenIds },
      stats,
    };
  } finally {
    await handle.close();
  }
}
