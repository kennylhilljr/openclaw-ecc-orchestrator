import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

export const PLAN = "3b7e".padEnd(64, "a");
export const HEAD = "c".repeat(40);
export const SESSION = "agent:main:main";

export async function tempDir(prefix = "ecc-plugin-test-") {
  return await fs.mkdtemp(path.join(os.tmpdir(), prefix));
}

export async function rmTemp(dir) {
  await fs.rm(dir, { recursive: true, force: true });
}

let counter = 0;
export function ev(type, { seq, run = "run1", unit = "alpha", data = {}, id } = {}) {
  counter += 1;
  return {
    schema_version: "1.0",
    event_version: 1,
    type,
    id: id ?? `id${String(seq ?? counter).padStart(6, "0")}x${counter}`,
    seq,
    emitted_at: "2026-09-24T12:00:00+00:00",
    run_id: run,
    unit_id: unit,
    data,
  };
}

export function approvalRequested(seq, overrides = {}) {
  return ev("approval.requested", {
    seq,
    data: {
      request_id: "apr-1",
      action: "merge",
      plan_sha256: PLAN,
      requesting_session: SESSION,
      expires_at: 2_000_000_000,
      summary: "merge alpha into main",
      head_sha: HEAD,
      ...overrides,
    },
  });
}

export async function appendLines(file, lines) {
  await fs.appendFile(file, lines.map((l) => (typeof l === "string" ? l : JSON.stringify(l))).join("\n") + "\n");
}
