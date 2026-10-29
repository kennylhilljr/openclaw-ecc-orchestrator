import assert from "node:assert/strict";
import { test } from "node:test";

import { REDACTED, containsSecret, maskText, sanitize } from "../../src/redact.js";
import { renderPanelHtml, renderPendingText, renderSummaryText, snapshot } from "../../src/render.js";
import { applyEvent, emptyState } from "../../src/state.js";
import { HEAD, PLAN, SESSION, approvalRequested, ev } from "./helpers.js";

// Credential shaped test values are assembled at run time so this file does
// not itself look like it contains credentials.
const GH = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4";
const SK = "sk" + "-" + "proj-" + "Zz9Yy8Xx7Ww6Vv5Uu4";
const AWS = "AK" + "IA" + "ABCDEFGHIJKLMNOP";
const OPAQUE = "Qm9vYmFyQmF6UXV4MTIzNDU2Nzg5MGFiY2RlZmdoaWo";

test("maskText hides known token shapes, credentials in URLs and key=value pairs", () => {
  const input = `token ${GH} and ${SK} aws ${AWS} url https://user:hunter22@example.com/x password=hunter22 Bearer abcdefghijkl ${OPAQUE}`;
  const out = maskText(input, 2000);
  for (const secret of [GH, SK, AWS, "hunter22", "abcdefghijkl", OPAQUE]) assert.ok(!out.includes(secret), secret);
  assert.ok(out.includes(REDACTED));
  assert.ok(out.includes("https://user:[REDACTED]@example.com/x"));
});

test("maskText keeps plan hashes and commit shas and strips control characters", () => {
  const out = maskText(`plan ${PLAN} head ${HEAD}\u0007‮`);
  assert.ok(out.includes(PLAN));
  assert.ok(out.includes(HEAD));
  assert.ok(!out.includes("\u0007"));
  assert.ok(!out.includes("‮"));
});

test("sanitize refuses to render fields named like secrets", () => {
  const out = sanitize({ api_key: "short", nested: { session_id: "abc", Authorization: "x", note: "fine" }, list: [{ password: "p" }] });
  assert.equal(out.api_key, REDACTED);
  assert.equal(out.nested.session_id, REDACTED);
  assert.equal(out.nested.Authorization, REDACTED);
  assert.equal(out.nested.note, "fine");
  assert.equal(out.list[0].password, REDACTED);
});

test("containsSecret flags handles that carry credentials", () => {
  assert.equal(containsSecret("operator-1"), false);
  assert.equal(containsSecret(GH), true);
});

function populated() {
  const s = emptyState();
  applyEvent(s, ev("run.created", { seq: 1, unit: null, data: { plan_sha256: PLAN, unit_ids: ["alpha", "beta"] } }));
  applyEvent(s, ev("unit.state_changed", { seq: 2, data: { from: "ready", to: "running", owner: "codex" } }));
  applyEvent(s, ev("unit.progress", { seq: 3, data: { message: `using ${GH} now`, stream: "stdout" } }));
  applyEvent(s, ev("attention.required", { seq: 4, unit: "beta", data: { reason: "out_of_scope_changes", files: ["a.py"], detail: { token: "x" } } }));
  applyEvent(s, approvalRequested(5, { summary: `merge <b>alpha</b> ${SK}` }));
  applyEvent(s, ev("merge.completed", { seq: 6, data: { target_branch: "main", merged_commit: HEAD, pushed: false } }));
  return s;
}

test("text rendering surfaces progress, attention, approvals and merges without secrets", () => {
  const s = populated();
  const summary = renderSummaryText(s, 1_900_000_000);
  assert.match(summary, /Run run1/);
  assert.match(summary, /alpha: running/);
  assert.match(summary, /out_of_scope_changes/);
  assert.match(summary, /main @ c{40}/);
  assert.match(summary, /Pending approvals: 1/);
  const pending = renderPendingText(s, 1_900_000_000);
  assert.ok(pending.includes(PLAN));
  assert.ok(pending.includes(HEAD));
  assert.ok(pending.includes(SESSION));
  assert.ok(pending.includes("merge <b>alpha</b>"), "summary shown");
  assert.ok(pending.includes(`/ecc-decide approve apr-1 ${PLAN.slice(0, 12)}`));
  for (const text of [summary, pending]) {
    assert.ok(!text.includes(GH));
    assert.ok(!text.includes(SK));
  }
});

test("panel HTML is escaped, script free and secret free", () => {
  const html = renderPanelHtml(populated(), 1_900_000_000);
  assert.ok(html.includes("&lt;b&gt;alpha&lt;/b&gt;"));
  assert.ok(!html.includes("<b>alpha</b>"));
  assert.ok(!/<script/i.test(html));
  assert.ok(!html.includes(GH));
  assert.ok(!html.includes(SK));
  assert.ok(html.includes(PLAN));
});

test("snapshot is sanitized and lists pending approvals", () => {
  const snap = snapshot(populated(), 1_900_000_000);
  assert.equal(snap.pending_approvals.length, 1);
  assert.equal(snap.pending_approvals[0].plan_sha256, PLAN);
  assert.equal(snap.attention[0].detail.token, REDACTED);
  assert.ok(!JSON.stringify(snap).includes(GH));
});

test("expired approvals are not pending", () => {
  const s = populated();
  assert.equal(snapshot(s, 2_100_000_000).pending_approvals.length, 0);
  assert.match(renderPendingText(s, 2_100_000_000), /No pending/);
});

test("health, completion and long running events render; unknown types stay ignored", () => {
  const s = emptyState();
  applyEvent(s, ev("run.created", { seq: 1, unit: null, data: { plan_sha256: PLAN, unit_ids: ["alpha"] } }));
  applyEvent(s, ev("unit.state_changed", { seq: 2, data: { from: "assigned", to: "running", owner: "codex" } }));
  applyEvent(s, ev("unit.health", { seq: 3, data: { state: "suspected_hang", previous: "healthy", process: "runner", flat_checks: 1, flat_checks_for_hang: 5, interval_seconds: 120 } }));
  applyEvent(s, ev("attention.required", { seq: 4, data: { reason: "suspected_hang", detail: { process: "runner", evidence: { flat_checks: 1, flat_checks_for_hang: 5 } } } }));
  applyEvent(s, ev("attention.required", { seq: 5, data: { reason: "long_running", detail: { elapsed_minutes: 95.5, budget_minutes: 45, multiple: 2 } } }));
  let summary = renderSummaryText(s, 1_900_000_000);
  assert.match(summary, /alpha: running \[health suspected_hang\]/);
  assert.match(summary, /long_running \(running 95.5 min, budget 45 min; not stopped\)/);
  assert.match(summary, /suspected_hang \(no progress for 1 of 5 checks\)/);
  applyEvent(s, ev("unit.completed", { seq: 6, data: { exit_code: 0, duration_seconds: 5400, attempts: 1, tier: 1, runner: "codex" } }));
  applyEvent(s, ev("unit.state_changed", { seq: 7, data: { from: "running", to: "verifying" } }));
  applyEvent(s, ev("attention.required", { seq: 8, unit: "alpha", data: { reason: "hung_cancelled", detail: { process: "gate", gate: "unit-tests", evidence: { flat_checks: 5 } } } }));
  summary = renderSummaryText(s, 1_900_000_000);
  assert.match(summary, /Recent completions:\n  run1\/alpha: exited 0 after 1h 30m, attempt 1, tier 1, runner codex/);
  assert.match(summary, /hung_cancelled \(cancelled after 5 checks without progress, gate unit-tests\)/);
  assert.equal(s.runs.run1.units.alpha.completed.duration_seconds, 5400);
  const snap = snapshot(s, 1_900_000_000);
  assert.equal(snap.completions.length, 1);
  assert.equal(snap.runs[0].units[0].health.state, "suspected_hang");
  const html = renderPanelHtml(s, 1_900_000_000);
  assert.ok(html.includes("Completions (1)"));
  assert.ok(html.includes("exited 0 after 1h 30m"));
  assert.ok(!html.includes("<script"));
  const before = s.counters.ignored;
  applyEvent(s, ev("unit.something_new", { seq: 9, data: {} }));
  assert.equal(s.counters.ignored, before + 1);
  applyEvent(s, ev("unit.completed", { seq: 10, unit: "../bad", data: { exit_code: 1 } }));
  assert.equal(s.counters.ignored, before + 2);
});
