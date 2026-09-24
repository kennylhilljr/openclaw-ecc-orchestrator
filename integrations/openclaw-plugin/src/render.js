// Rendering of the view model for OpenClaw command replies (plain text) and
// the read only Control UI tab (static HTML, no scripts). Every string passes
// through maskText; HTML output is escaped.

import { maskText, sanitize } from "./redact.js";
import { effectiveStatus, pendingApprovals } from "./state.js";

const ACTIVE_STATES = new Set(["assigned", "running", "verifying", "reviewing", "queued_for_merge"]);
const PROBLEM_STATES = new Set(["failed", "blocked", "needs_user", "interrupted"]);

function t(v, max = 200) {
  return maskText(v ?? "", max);
}

function remaining(expiresAt, nowSeconds) {
  if (typeof expiresAt !== "number") return "unknown";
  const s = Math.floor(expiresAt - nowSeconds);
  if (s <= 0) return "expired";
  if (s < 120) return `${s}s left`;
  if (s < 7200) return `${Math.floor(s / 60)}m left`;
  return `${Math.floor(s / 3600)}h left`;
}

export function unitCounts(run) {
  const counts = {};
  for (const u of Object.values(run.units)) {
    const k = u.state || "unknown";
    counts[k] = (counts[k] || 0) + 1;
  }
  return counts;
}

export function snapshot(state, nowSeconds, { attentionLimit = 20, mergeLimit = 20 } = {}) {
  const runs = Object.values(state.runs)
    .sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)))
    .map((r) => ({
      run_id: r.run_id,
      plan_sha256: r.plan_sha256,
      updated_at: r.updated_at,
      counts: unitCounts(r),
      units: Object.values(r.units)
        .sort((a, b) => a.unit_id.localeCompare(b.unit_id))
        .map((u) => ({
          unit_id: u.unit_id,
          state: u.state,
          reason: u.reason,
          owner: u.owner,
          updated_at: u.updated_at,
          last_progress: u.progress.length ? u.progress[u.progress.length - 1] : null,
        })),
    }));
  const approvals = Object.values(state.approvals)
    .sort((a, b) => (b.seq ?? 0) - (a.seq ?? 0))
    .map((a) => ({
      request_id: a.request_id,
      run_id: a.run_id,
      unit_id: a.unit_id,
      action: a.action,
      status: effectiveStatus(a, nowSeconds),
      summary: a.summary,
      plan_sha256: a.plan_sha256,
      head_sha: a.head_sha,
      requesting_session: a.requesting_session,
      expires_at: a.expires_at,
      decided_by: a.decided_by,
      submitted: a.submitted ? { decision: a.submitted.decision, decided_by: a.submitted.decided_by, at: a.submitted.at } : null,
      runtime_result: a.runtime_result,
    }));
  return sanitize(
    {
      runs,
      pending_approvals: approvals.filter((a) => a.status === "pending"),
      approvals: approvals.slice(0, 50),
      attention: state.attention.slice(-attentionLimit).reverse(),
      merges: state.merges.slice(-mergeLimit).reverse(),
      counters: state.counters,
    },
    { maxDepth: 8, maxItems: 200, maxLength: 1000 },
  );
}

export function renderApprovalText(a, nowSeconds) {
  const status = effectiveStatus(a, nowSeconds);
  const lines = [
    `Approval ${t(a.request_id, 100)} [${status}]`,
    `  run ${t(a.run_id)} unit ${t(a.unit_id)} action ${t(a.action, 64)}`,
    `  plan sha256 ${t(a.plan_sha256, 64)}`,
    `  head sha    ${a.head_sha ? t(a.head_sha, 64) : "(not bound)"}`,
    `  session     ${t(a.requesting_session ?? "(none)", 128)}`,
    `  expires     ${remaining(a.expires_at, nowSeconds)}`,
  ];
  if (a.summary) lines.push(`  summary     ${t(a.summary, 500)}`);
  if (a.decided_by) lines.push(`  decided by  ${t(a.decided_by, 128)}`);
  if (a.submitted) lines.push(`  submitted   ${a.submitted.decision} by ${t(a.submitted.decided_by, 128)}`);
  if (a.runtime_result) {
    lines.push(`  runtime     ${a.runtime_result.ok ? "accepted" : `refused (${t((a.runtime_result.failed || []).join(","), 200)})`}`);
  }
  if (status === "pending") {
    const prefix = typeof a.plan_sha256 === "string" ? a.plan_sha256.slice(0, 12) : "<hash prefix>";
    lines.push(`  approve:    /ecc-decide approve ${t(a.request_id, 100)} ${prefix}`);
    lines.push(`  reject:     /ecc-decide reject ${t(a.request_id, 100)}`);
    lines.push(
      `  or method:  ecc-orchestrator.decide {"requestId":"${t(a.request_id, 100)}","decision":"approve","planHashPrefix":"${prefix}","sessionKey":"${t(a.requesting_session ?? "", 128)}"}`,
    );
  }
  return lines.join("\n");
}

export function renderSummaryText(state, nowSeconds) {
  const lines = [];
  const runs = Object.values(state.runs).sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)));
  if (!runs.length) lines.push("No orchestrator runs seen yet.");
  for (const run of runs.slice(0, 10)) {
    const counts = unitCounts(run);
    const summary = Object.entries(counts)
      .sort()
      .map(([k, v]) => `${k} ${v}`)
      .join(", ");
    lines.push(`Run ${t(run.run_id)}: ${summary || "no units"}`);
    for (const u of Object.values(run.units).sort((a, b) => a.unit_id.localeCompare(b.unit_id))) {
      if (!ACTIVE_STATES.has(u.state) && !PROBLEM_STATES.has(u.state)) continue;
      const last = u.progress.length ? ` | ${t(u.progress[u.progress.length - 1].message, 120)}` : "";
      const reason = u.reason ? ` (${t(u.reason, 80)})` : "";
      lines.push(`  ${t(u.unit_id)}: ${t(u.state, 40)}${reason}${last}`);
    }
  }
  const pending = pendingApprovals(state, nowSeconds);
  lines.push("");
  lines.push(`Pending approvals: ${pending.length}${pending.length ? " (see /ecc pending)" : ""}`);
  const attention = state.attention.slice(-5).reverse();
  if (attention.length) {
    lines.push("Recent attention:");
    for (const a of attention) {
      lines.push(`  ${t(a.run_id)}/${t(a.unit_id ?? "-")}: ${t(a.reason, 80)}${a.state ? ` [${t(a.state, 40)}]` : ""}`);
    }
  }
  const merges = state.merges.slice(-5).reverse();
  if (merges.length) {
    lines.push("Recent merges:");
    for (const m of merges) {
      lines.push(`  ${t(m.run_id)}/${t(m.unit_id)} -> ${t(m.target_branch, 100)} @ ${t(m.merged_commit, 64)}`);
    }
  }
  return lines.join("\n");
}

export function renderPendingText(state, nowSeconds) {
  const pending = pendingApprovals(state, nowSeconds);
  if (!pending.length) return "No pending approval requests.";
  return pending.map((a) => renderApprovalText(a, nowSeconds)).join("\n\n");
}

export function renderAttentionText(state, limit = 20) {
  const items = state.attention.slice(-limit).reverse();
  if (!items.length) return "No attention items.";
  return items
    .map((a) => {
      const files = a.files && a.files.length ? ` files: ${a.files.slice(0, 5).map((f) => t(f, 100)).join(", ")}` : "";
      return `${t(a.at ?? "", 40)} ${t(a.run_id)}/${t(a.unit_id ?? "-")}: ${t(a.reason, 80)}${a.state ? ` [${t(a.state, 40)}]` : ""}${files}`;
    })
    .join("\n");
}

export function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function h(v, max) {
  return escapeHtml(t(v, max));
}

export function renderPanelHtml(state, nowSeconds, { refreshSeconds = 10 } = {}) {
  const pending = pendingApprovals(state, nowSeconds);
  const runs = Object.values(state.runs).sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)));
  const parts = [];
  parts.push(`<!doctype html><html lang="en"><head><meta charset="utf-8">`);
  parts.push(`<meta http-equiv="refresh" content="${Number(refreshSeconds) | 0}">`);
  parts.push(`<meta name="viewport" content="width=device-width, initial-scale=1">`);
  parts.push(`<title>ECC Orchestrator</title><style>
:root{color-scheme:light dark;--fg:#1d1d1f;--bg:#fff;--muted:#6b6b70;--line:#d9d9de;--warn:#9a5b00;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--fg:#ececf0;--bg:#16161a;--muted:#a0a0a8;--line:#34343a;--warn:#f0b35a;--bad:#ff8a80}}
body{font:14px/1.45 system-ui,sans-serif;color:var(--fg);background:var(--bg);margin:0;padding:16px}
h1{font-size:18px;margin:0 0 12px}h2{font-size:15px;margin:20px 0 8px}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left;vertical-align:top}
code{font:12px ui-monospace,monospace;word-break:break-all}.muted{color:var(--muted)}.warn{color:var(--warn)}.bad{color:var(--bad)}
</style></head><body>`);
  parts.push(`<h1>ECC Orchestrator</h1><p class="muted">Read only view of the runtime event log. Decide with <code>/ecc-decide</code> in the requesting session or with the <code>ecc-orchestrator.decide</code> gateway method; both need <code>operator.approvals</code>.</p>`);
  parts.push(`<h2>Pending approvals (${pending.length})</h2>`);
  if (!pending.length) parts.push(`<p class="muted">None.</p>`);
  else {
    parts.push(`<table><tr><th>Request</th><th>Run / unit</th><th>Action</th><th>Plan sha256</th><th>Head sha</th><th>Session</th><th>Expires</th><th>Summary</th></tr>`);
    for (const a of pending) {
      parts.push(
        `<tr><td><code>${h(a.request_id, 100)}</code></td><td>${h(a.run_id)} / ${h(a.unit_id)}</td><td>${h(a.action, 64)}</td>` +
          `<td><code>${h(a.plan_sha256, 64)}</code></td><td><code>${h(a.head_sha ?? "-", 64)}</code></td>` +
          `<td><code>${h(a.requesting_session ?? "-", 128)}</code></td><td>${escapeHtml(remaining(a.expires_at, nowSeconds))}</td>` +
          `<td>${h(a.summary ?? "", 500)}</td></tr>`,
      );
    }
    parts.push(`</table>`);
  }
  parts.push(`<h2>Runs</h2>`);
  if (!runs.length) parts.push(`<p class="muted">No runs seen yet.</p>`);
  for (const run of runs.slice(0, 20)) {
    parts.push(`<h3>${h(run.run_id)}</h3><table><tr><th>Unit</th><th>State</th><th>Reason</th><th>Owner</th><th>Last progress</th></tr>`);
    for (const u of Object.values(run.units).sort((a, b) => a.unit_id.localeCompare(b.unit_id))) {
      const cls = PROBLEM_STATES.has(u.state) ? "bad" : ACTIVE_STATES.has(u.state) ? "warn" : "";
      const last = u.progress.length ? u.progress[u.progress.length - 1].message : "";
      parts.push(
        `<tr><td>${h(u.unit_id)}</td><td class="${cls}">${h(u.state ?? "-", 40)}</td><td>${h(u.reason ?? "", 120)}</td>` +
          `<td>${h(u.owner ?? "", 128)}</td><td>${h(last, 200)}</td></tr>`,
      );
    }
    parts.push(`</table>`);
  }
  const attention = state.attention.slice(-20).reverse();
  parts.push(`<h2>Attention (${attention.length})</h2>`);
  if (!attention.length) parts.push(`<p class="muted">None.</p>`);
  else {
    parts.push(`<table><tr><th>When</th><th>Run / unit</th><th>Reason</th><th>State</th><th>Files</th></tr>`);
    for (const a of attention) {
      parts.push(
        `<tr><td>${h(a.at ?? "", 40)}</td><td>${h(a.run_id)} / ${h(a.unit_id ?? "-")}</td><td class="warn">${h(a.reason, 100)}</td>` +
          `<td>${h(a.state ?? "", 40)}</td><td>${h((a.files || []).slice(0, 5).join(", "), 300)}</td></tr>`,
      );
    }
    parts.push(`</table>`);
  }
  const merges = state.merges.slice(-20).reverse();
  parts.push(`<h2>Merges (${merges.length})</h2>`);
  if (!merges.length) parts.push(`<p class="muted">None.</p>`);
  else {
    parts.push(`<table><tr><th>When</th><th>Run / unit</th><th>Target</th><th>Merged commit</th><th>Pushed</th></tr>`);
    for (const m of merges) {
      parts.push(
        `<tr><td>${h(m.at ?? "", 40)}</td><td>${h(m.run_id)} / ${h(m.unit_id)}</td><td>${h(m.target_branch, 100)}</td>` +
          `<td><code>${h(m.merged_commit, 64)}</code></td><td>${m.pushed ? "yes" : "no"}</td></tr>`,
      );
    }
    parts.push(`</table>`);
  }
  parts.push(`</body></html>`);
  return parts.join("");
}
