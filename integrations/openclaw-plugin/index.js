// OpenClaw plugin entry for the openclaw-ecc-orchestrator runtime.
//
// OpenClaw APIs used (OpenClaw 2026.9.6, see README "OpenClaw APIs"):
//   api.pluginConfig, api.logger, api.registerService, api.registerCommand,
//   api.registerGatewayMethod, api.registerHttpRoute,
//   api.session.controls.registerControlUiDescriptor.
//
// Decisions: the /ecc-decide command (sender and host bound session from the
// command context) or the ecc-orchestrator.decide gateway method (the
// connection's authenticated OpenClaw profile). Both require the
// operator.approvals scope. Nothing here approves on its own.
//
// The module has no runtime dependencies and does not import the OpenClaw SDK:
// the default export is the same plain object definePluginEntry() returns.

import { ConfigError, parseConfig } from "./src/config.js";
import { DecisionError, identityFromCommandContext, identityFromGatewayClient } from "./src/decision.js";
import { createEngine } from "./src/engine.js";
import {
  renderApprovalText,
  renderAttentionText,
  renderPanelHtml,
  renderPendingText,
  renderSummaryText,
  snapshot,
} from "./src/render.js";
import { maskText } from "./src/redact.js";
import { renderRuntimeStatus, runRuntimeStatus } from "./src/runtime-status.js";
import { validId } from "./src/state.js";

export const PLUGIN_ID = "ecc-orchestrator";
export const PANEL_PATH = "/plugins/ecc-orchestrator/panel";
export const SNAPSHOT_METHOD = "ecc-orchestrator.snapshot";
export const DECIDE_METHOD = "ecc-orchestrator.decide";
export const READ_COMMAND = "ecc";
export const DECIDE_COMMAND = "ecc-decide";

const READ_HELP = [
  "/ecc                     summary of runs, pending approvals, attention, merges",
  "/ecc pending             pending approval requests with plan hash and head sha",
  "/ecc show <request_id>   one approval request",
  "/ecc attention           recent attention items",
  "/ecc runtime [run_id]    read only runtime CLI status (when runtimeCli is configured)",
  "/ecc-decide approve <request_id> <plan_hash_prefix>",
  "/ecc-decide reject <request_id>",
  "Gateway method ecc-orchestrator.decide {requestId, decision: approve|reject, planHashPrefix, sessionKey}",
].join("\n");

function words(args) {
  return String(args ?? "")
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 8);
}

export function createCommandHandlers(engine, cfg, { runtimeStatus = runRuntimeStatus } = {}) {
  async function read(ctx) {
    const [sub = "status", arg] = words(ctx?.args);
    await engine.pollOnce();
    const now = engine.now();
    switch (sub) {
      case "status":
        return { text: renderSummaryText(engine.state, now) };
      case "pending":
        return { text: renderPendingText(engine.state, now) };
      case "attention":
        return { text: renderAttentionText(engine.state) };
      case "show": {
        if (!validId(arg)) return { text: "Usage: /ecc show <request_id>" };
        const a = engine.state.approvals[arg];
        return { text: a ? renderApprovalText(a, now) : "Unknown approval request id." };
      }
      case "runtime": {
        if (arg !== undefined && !validId(arg)) return { text: "Usage: /ecc runtime [run_id]" };
        const res = await runtimeStatus(cfg, { runId: arg ?? null });
        return { text: renderRuntimeStatus(res) };
      }
      case "help":
        return { text: READ_HELP };
      default:
        return { text: `Unknown subcommand.\n${READ_HELP}` };
    }
  }

  async function decide(ctx) {
    const [verb, requestId, planPrefix] = words(ctx?.args);
    const decision = verb === "approve" ? "approved" : verb === "reject" ? "rejected" : null;
    if (!decision || !requestId) {
      return { text: "Usage: /ecc-decide approve <request_id> <plan_hash_prefix> | /ecc-decide reject <request_id>" };
    }
    try {
      const identity = identityFromCommandContext(ctx);
      await engine.decide({ requestId, decision, identity, planPrefix });
      return {
        text:
          `Decision ${decision} for ${maskText(requestId, 100)} written to the runtime inbox by ${maskText(identity.decidedBy, 128)}. ` +
          "The runtime validates it and emits approval.resolved; check /ecc show " +
          `${maskText(requestId, 100)}.`,
      };
    } catch (err) {
      if (err instanceof DecisionError) return { text: `Refused (${err.code}): ${err.message}` };
      return { text: "Refused: the decision could not be written (see gateway logs)." };
    }
  }

  async function decideRpc({ params, client, respond }) {
    const p = params && typeof params === "object" ? params : {};
    const decision = p.decision === "approve" ? "approved" : p.decision === "reject" ? "rejected" : null;
    try {
      if (!decision) throw new DecisionError("invalid_decision", "decision must be approve or reject");
      if (typeof p.requestId !== "string") throw new DecisionError("unknown_request", "requestId is required");
      const identity = identityFromGatewayClient(client, p);
      await engine.decide({
        requestId: p.requestId,
        decision,
        identity,
        planPrefix: typeof p.planHashPrefix === "string" ? p.planHashPrefix : undefined,
      });
      respond(true, { ok: true, request_id: p.requestId, decision, decided_by: identity.decidedBy }, undefined);
    } catch (err) {
      if (err instanceof DecisionError) {
        respond(false, undefined, { code: "INVALID_REQUEST", message: `Refused (${err.code}): ${err.message}`, details: { reason: err.code } });
      } else {
        respond(false, undefined, { code: "UNAVAILABLE", message: "the decision could not be written" });
      }
    }
  }

  return { read, decide, decideRpc };
}

function notConfigured(message) {
  return async () => ({
    text: `ecc-orchestrator is not configured: ${maskText(message, 300)}. Set plugins.entries.${PLUGIN_ID}.config (eventLog, inbox, stateDir).`,
  });
}

export function register(api) {
  const logger = api.logger;
  let cfg;
  try {
    cfg = parseConfig(api.pluginConfig ?? {});
  } catch (err) {
    const message = err instanceof ConfigError ? err.message : "invalid configuration";
    logger?.warn?.(`ecc-orchestrator: ${message}`);
    api.registerCommand({
      name: READ_COMMAND,
      description: "ECC orchestrator status (not configured)",
      acceptsArgs: true,
      requireAuth: true,
      handler: notConfigured(message),
    });
    return;
  }

  const engine = createEngine(cfg, { logger });
  const handlers = createCommandHandlers(engine, cfg);
  let timer = null;

  api.registerService({
    id: "ecc-orchestrator-tail",
    async start() {
      const tick = async () => {
        try {
          await engine.pollOnce();
        } catch (err) {
          logger?.warn?.(`ecc-orchestrator: event log poll failed (${maskText(err?.code ?? "error", 40)})`);
        }
      };
      await tick();
      timer = setInterval(tick, cfg.pollIntervalMs);
      if (typeof timer.unref === "function") timer.unref();
    },
    async stop() {
      if (timer) clearInterval(timer);
      timer = null;
    },
  });

  api.registerCommand({
    name: READ_COMMAND,
    description: "ECC orchestrator runs, approvals, attention and merges (read only)",
    acceptsArgs: true,
    requireAuth: true,
    requiredScopes: ["operator.read"],
    handler: handlers.read,
  });

  api.registerCommand({
    name: DECIDE_COMMAND,
    description: "Approve or reject an ECC orchestrator approval request from its requesting session",
    acceptsArgs: true,
    requireAuth: true,
    requiredScopes: ["operator.approvals"],
    handler: handlers.decide,
  });

  api.registerGatewayMethod(
    SNAPSHOT_METHOD,
    async ({ respond }) => {
      try {
        await engine.pollOnce();
        respond(true, snapshot(engine.state, engine.now()), undefined);
      } catch {
        respond(false, undefined, { code: "UNAVAILABLE", message: "ecc-orchestrator snapshot unavailable" });
      }
    },
    { scope: "operator.read" },
  );

  api.registerGatewayMethod(DECIDE_METHOD, handlers.decideRpc, { scope: "operator.approvals" });

  api.registerHttpRoute({
    path: PANEL_PATH,
    auth: "gateway",
    match: "exact",
    handler: async (req, res) => {
      if (req.method !== "GET" && req.method !== "HEAD") {
        res.statusCode = 405;
        res.setHeader("Allow", "GET, HEAD");
        res.end();
        return true;
      }
      let html;
      try {
        await engine.pollOnce();
        html = renderPanelHtml(engine.state, engine.now());
      } catch {
        html = "<!doctype html><title>ECC Orchestrator</title><p>Snapshot unavailable.</p>";
      }
      res.statusCode = 200;
      res.setHeader("Content-Type", "text/html; charset=utf-8");
      res.setHeader("Cache-Control", "no-store");
      res.setHeader("X-Content-Type-Options", "nosniff");
      res.setHeader("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'self'");
      res.end(req.method === "HEAD" ? undefined : html);
      return true;
    },
  });

  const controls = api.session && api.session.controls;
  const registerDescriptor =
    controls && typeof controls.registerControlUiDescriptor === "function"
      ? controls.registerControlUiDescriptor.bind(controls)
      : typeof api.registerControlUiDescriptor === "function"
        ? api.registerControlUiDescriptor.bind(api)
        : null;
  if (registerDescriptor) {
    registerDescriptor({
      surface: "tab",
      id: "ecc-orchestrator",
      label: "ECC Orchestrator",
      description: "Runs, approvals, attention items and merges from the ECC orchestrator runtime.",
      icon: "git-merge",
      group: "control",
      path: PANEL_PATH,
      requiredScopes: ["operator.read"],
    });
  }
}

export default {
  id: PLUGIN_ID,
  name: "ECC Orchestrator",
  description: "Surfaces openclaw-ecc-orchestrator runtime events in OpenClaw and records approval decisions.",
  register,
};
