// Folds runtime events into a bounded view model: runs and unit states,
// recent progress, attention items, approval requests and merges.
//
// Binding fields of approval requests (request_id, run_id, unit_id, action,
// plan_sha256, requesting_session, head_sha, expires_at) are validated and
// stored verbatim; they are never recomputed, and a later event can never
// overwrite them. Display text is sanitized on ingest.

import { sanitize, maskText } from "./redact.js";

export const ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$/;
export const HANDLE_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
export const SHA256_RE = /^[0-9a-f]{64}$/;
export const HEAD_SHA_RE = /^[0-9a-f]{40,64}$/;

export const LIMITS = Object.freeze({
  progressPerUnit: 20,
  attention: 200,
  merges: 100,
  approvals: 500,
  runs: 200,
});

export const KNOWN_TYPES = new Set([
  "run.created",
  "unit.state_changed",
  "unit.progress",
  "attention.required",
  "approval.requested",
  "approval.resolved",
  "merge.completed",
]);

export function validId(v) {
  return typeof v === "string" && ID_RE.test(v) && !v.includes("..");
}

export function emptyState() {
  return { version: 1, runs: {}, approvals: {}, attention: [], merges: [], counters: { applied: 0, ignored: 0 } };
}

function ensureRun(state, runId, at) {
  let run = state.runs[runId];
  if (!run) {
    run = { run_id: runId, plan_sha256: null, unit_ids: [], created_at: null, updated_at: at, units: {} };
    state.runs[runId] = run;
  }
  run.updated_at = at;
  return run;
}

function ensureUnit(run, unitId, at) {
  let unit = run.units[unitId];
  if (!unit) {
    unit = { unit_id: unitId, state: null, from: null, reason: null, owner: null, updated_at: at, progress: [] };
    run.units[unitId] = unit;
  }
  unit.updated_at = at;
  return unit;
}

function text(v, max = 500) {
  return typeof v === "string" ? maskText(v, max) : v === undefined || v === null ? null : maskText(String(v), max);
}

function pruneApprovals(state) {
  const entries = Object.values(state.approvals);
  if (entries.length <= LIMITS.approvals) return;
  const removable = entries
    .filter((a) => a.status !== "pending")
    .sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
  let excess = entries.length - LIMITS.approvals;
  for (const a of removable) {
    if (excess <= 0) break;
    delete state.approvals[a.request_id];
    excess -= 1;
  }
}

function pruneRuns(state) {
  const runs = Object.values(state.runs);
  if (runs.length <= LIMITS.runs) return;
  runs.sort((a, b) => String(a.updated_at).localeCompare(String(b.updated_at)));
  for (const r of runs.slice(0, runs.length - LIMITS.runs)) delete state.runs[r.run_id];
}

/**
 * Validate the binding fields of an approval.requested event. Returns the
 * verbatim binding or a reason why it cannot be decided from OpenClaw.
 */
export function approvalBinding(ev) {
  const d = ev.data || {};
  const problems = [];
  if (!validId(d.request_id)) problems.push("request_id");
  if (!validId(ev.run_id)) problems.push("run_id");
  if (!validId(ev.unit_id)) problems.push("unit_id");
  if (typeof d.action !== "string" || d.action.length === 0 || d.action.length > 64) problems.push("action");
  if (typeof d.plan_sha256 !== "string" || !SHA256_RE.test(d.plan_sha256)) problems.push("plan_sha256");
  if (typeof d.expires_at !== "number" || !Number.isFinite(d.expires_at)) problems.push("expires_at");
  if (d.requesting_session !== undefined && d.requesting_session !== null) {
    if (typeof d.requesting_session !== "string" || !HANDLE_RE.test(d.requesting_session)) problems.push("requesting_session");
  }
  if (d.head_sha !== undefined && d.head_sha !== null) {
    if (typeof d.head_sha !== "string" || !HEAD_SHA_RE.test(d.head_sha)) problems.push("head_sha");
  }
  return {
    problems,
    binding: {
      request_id: d.request_id,
      run_id: ev.run_id,
      unit_id: ev.unit_id,
      action: d.action,
      plan_sha256: d.plan_sha256,
      requesting_session: typeof d.requesting_session === "string" ? d.requesting_session : null,
      head_sha: typeof d.head_sha === "string" ? d.head_sha : null,
      expires_at: d.expires_at,
    },
  };
}

/** Apply one event in place. Unknown types and malformed payloads are ignored. */
export function applyEvent(state, ev) {
  if (!KNOWN_TYPES.has(ev.type)) {
    state.counters.ignored += 1;
    return false;
  }
  const d = ev.data || {};
  const at = typeof ev.emitted_at === "string" ? ev.emitted_at.slice(0, 40) : null;
  if (!validId(ev.run_id)) {
    state.counters.ignored += 1;
    return false;
  }
  const unitOk = validId(ev.unit_id);
  switch (ev.type) {
    case "run.created": {
      const run = ensureRun(state, ev.run_id, at);
      if (typeof d.plan_sha256 === "string" && SHA256_RE.test(d.plan_sha256)) run.plan_sha256 = d.plan_sha256;
      if (Array.isArray(d.unit_ids)) run.unit_ids = d.unit_ids.filter(validId).slice(0, 200);
      run.created_at = at;
      for (const uid of run.unit_ids) ensureUnit(run, uid, at);
      break;
    }
    case "unit.state_changed": {
      if (!unitOk) return ignore(state);
      const unit = ensureUnit(ensureRun(state, ev.run_id, at), ev.unit_id, at);
      unit.from = text(d.from, 40);
      unit.state = text(d.to, 40);
      unit.reason = text(d.reason, 200);
      unit.owner = text(d.owner, 128);
      unit.last_seq = ev.seq;
      break;
    }
    case "unit.progress": {
      if (!unitOk) return ignore(state);
      const unit = ensureUnit(ensureRun(state, ev.run_id, at), ev.unit_id, at);
      unit.progress.push({ seq: ev.seq, at, stream: text(d.stream, 10), message: text(d.message, 300) });
      if (unit.progress.length > LIMITS.progressPerUnit) unit.progress.splice(0, unit.progress.length - LIMITS.progressPerUnit);
      break;
    }
    case "attention.required": {
      ensureRun(state, ev.run_id, at);
      state.attention.push({
        seq: ev.seq,
        at,
        run_id: ev.run_id,
        unit_id: unitOk ? ev.unit_id : null,
        reason: text(d.reason, 100),
        state: text(d.state, 40),
        files: Array.isArray(d.files) ? d.files.slice(0, 20).map((f) => text(f, 200)) : [],
        detail: sanitize(d.detail ?? null, { maxDepth: 3, maxItems: 20, maxLength: 300 }),
      });
      if (state.attention.length > LIMITS.attention) state.attention.splice(0, state.attention.length - LIMITS.attention);
      break;
    }
    case "approval.requested": {
      const { problems, binding } = approvalBinding(ev);
      if (!validId(binding.request_id)) return ignore(state);
      if (state.approvals[binding.request_id]) {
        // Never overwrite binding fields of a known request.
        state.counters.ignored += 1;
        return false;
      }
      ensureRun(state, ev.run_id, at);
      state.approvals[binding.request_id] = {
        ...binding,
        seq: ev.seq,
        requested_at: at,
        summary: text(d.summary, 1000),
        status: problems.length ? "invalid" : "pending",
        invalid_fields: problems,
        decided_by: null,
        submitted: null,
        runtime_result: null,
      };
      pruneApprovals(state);
      break;
    }
    case "approval.resolved": {
      if (!validId(d.request_id)) return ignore(state);
      const a = state.approvals[d.request_id];
      const decision = d.decision === "approved" || d.decision === "rejected" ? d.decision : null;
      if (!a) {
        state.approvals[d.request_id] = {
          request_id: d.request_id,
          run_id: ev.run_id,
          unit_id: unitOk ? ev.unit_id : null,
          action: text(d.action, 64),
          plan_sha256: null,
          requesting_session: null,
          head_sha: null,
          expires_at: null,
          seq: ev.seq,
          requested_at: null,
          summary: null,
          status: decision ?? "resolved",
          invalid_fields: ["unknown_request"],
          decided_by: text(d.decided_by, 128),
          submitted: null,
          runtime_result: null,
        };
        pruneApprovals(state);
      } else {
        a.status = decision ?? "resolved";
        a.decided_by = text(d.decided_by, 128);
        a.resolved_seq = ev.seq;
      }
      break;
    }
    case "merge.completed": {
      if (!unitOk) return ignore(state);
      ensureRun(state, ev.run_id, at);
      state.merges.push({
        seq: ev.seq,
        at,
        run_id: ev.run_id,
        unit_id: ev.unit_id,
        target_branch: text(d.target_branch, 200),
        merged_commit: text(d.merged_commit, 64),
        previous_commit: text(d.previous_commit, 64),
        branch: text(d.branch, 200),
        pushed: d.pushed === true,
      });
      if (state.merges.length > LIMITS.merges) state.merges.splice(0, state.merges.length - LIMITS.merges);
      break;
    }
    default:
      return ignore(state);
  }
  pruneRuns(state);
  state.counters.applied += 1;
  return true;
}

function ignore(state) {
  state.counters.ignored += 1;
  return false;
}

/** Effective approval status on the plugin clock (pending requests can expire). */
export function effectiveStatus(approval, nowSeconds) {
  if (approval.status === "pending" && typeof approval.expires_at === "number" && nowSeconds >= approval.expires_at) {
    return "expired";
  }
  return approval.status;
}

export function pendingApprovals(state, nowSeconds) {
  return Object.values(state.approvals)
    .filter((a) => effectiveStatus(a, nowSeconds) === "pending")
    .sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
}
