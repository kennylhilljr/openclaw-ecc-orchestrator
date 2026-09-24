// Optional read only `status` call to the runtime CLI. Only the `status`
// subcommand is ever run, without a shell, with a minimal environment, a
// timeout and an output cap. Output is sanitized before display.

import { execFile } from "node:child_process";
import fs from "node:fs/promises";

import { maskText, sanitize } from "./redact.js";
import { validId } from "./state.js";

const ENV_ALLOWLIST = ["PATH", "HOME", "LANG", "LC_ALL", "XDG_STATE_HOME", "XDG_DATA_HOME", "TMPDIR"];

export async function runRuntimeStatus(cfg, { runId = null, timeoutMs = 20000, env = process.env, exec = execFile } = {}) {
  if (!cfg.runtimeCli) return { ok: false, error: "runtimeCli is not configured" };
  if (runId !== null && !validId(runId)) return { ok: false, error: "invalid run id" };
  const st = await fs.stat(cfg.runtimeCli).catch(() => null);
  if (!st || !st.isFile()) return { ok: false, error: "runtimeCli does not exist or is not a file" };
  const args = [...cfg.runtimeCliArgs, "status", "--json"];
  if (runId) args.push("--run-id", runId);
  const childEnv = {};
  for (const k of ENV_ALLOWLIST) if (typeof env[k] === "string") childEnv[k] = env[k];
  return await new Promise((resolve) => {
    exec(
      cfg.runtimeCli,
      args,
      { timeout: timeoutMs, maxBuffer: 1024 * 1024, env: childEnv, shell: false, windowsHide: true },
      (err, stdout) => {
        let parsed = null;
        try {
          parsed = JSON.parse(String(stdout || ""));
        } catch {
          parsed = null;
        }
        if (!parsed || typeof parsed !== "object") {
          resolve({ ok: false, error: err ? `runtime status failed (${maskText(err.code ?? err.message, 80)})` : "runtime status returned no JSON" });
          return;
        }
        resolve({ ok: parsed.ok === true, envelope: sanitize(parsed, { maxDepth: 8, maxItems: 100, maxLength: 500 }) });
      },
    );
  });
}

export function renderRuntimeStatus(result) {
  if (!result.envelope) return `Runtime status unavailable: ${maskText(result.error, 200)}`;
  const env = result.envelope;
  const lines = [`Runtime status: ${env.ok ? "ok" : "not ok"}`];
  const data = env.data || {};
  if (Array.isArray(data.runs)) {
    for (const r of data.runs.slice(0, 20)) {
      const counts = r.counts || r.unit_states || {};
      lines.push(`  ${maskText(r.run_id ?? "?", 100)}: ${maskText(JSON.stringify(counts), 200)}`);
    }
  }
  if (data.units && typeof data.units === "object") {
    for (const [uid, u] of Object.entries(data.units).slice(0, 50)) {
      lines.push(`  ${maskText(uid, 100)}: ${maskText(u && u.state, 40)}`);
    }
  }
  const actions = Array.isArray(env.required_user_actions) ? env.required_user_actions : [];
  if (actions.length) {
    lines.push("Required user actions:");
    for (const a of actions.slice(0, 20)) {
      lines.push(`  ${maskText(a.kind, 40)} ${maskText(a.run_id ?? "", 100)}/${maskText(a.unit_id ?? "-", 100)} ${maskText(a.detail ?? "", 200)}`);
    }
  }
  return lines.join("\n");
}
