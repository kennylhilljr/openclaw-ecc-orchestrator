// Decision construction and the atomic inbox write.
//
// The decision copies the pending request's binding fields verbatim from the
// approval.requested event and adds `decision` and `decided_by`. Identity and
// session come only from the OpenClaw command context (authenticated sender
// and host-bound session key), never from OS users, environment variables or
// free text. The plugin never computes or alters plan hashes or head shas and
// never decides on its own.

import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";

import { containsSecret } from "./redact.js";
import { HANDLE_RE, effectiveStatus, validId } from "./state.js";

export const DECISIONS = Object.freeze(["approved", "rejected"]);
export const MAX_DECISION_BYTES = 64 * 1024;
export const MIN_PLAN_PREFIX = 8;

export class DecisionError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

function looksLikeEmail(v) {
  return /@/.test(v);
}

// Canonical OpenClaw gateway client product ids (client-info GATEWAY_CLIENT_IDS
// in OpenClaw 2026.9.6). For non UI gateway clients OpenClaw reports this id
// as the command sender; it names a program, not a person, so it is refused.
export const CLIENT_PRODUCT_IDS = new Set([
  "webchat-ui",
  "openclaw-control-ui",
  "openclaw-browser-copilot",
  "openclaw-tui",
  "webchat",
  "cli",
  "gateway-client",
  "openclaw-macos",
  "openclaw-linux",
  "openclaw-ios",
  "openclaw-watchos",
  "openclaw-android",
  "node-host",
  "openclaw-worker",
  "test",
  "fingerprint",
  "openclaw-probe",
]);

function checkHandles(decidedBy, sessionKey) {
  if (!HANDLE_RE.test(decidedBy) || looksLikeEmail(decidedBy) || containsSecret(decidedBy)) {
    throw new DecisionError("invalid_identity", "the OpenClaw identity is not a valid operator handle");
  }
  if (CLIENT_PRODUCT_IDS.has(decidedBy.toLowerCase())) {
    throw new DecisionError("invalid_identity", "the OpenClaw sender is a client program id, not a person");
  }
  if (!HANDLE_RE.test(sessionKey) || containsSecret(sessionKey)) {
    throw new DecisionError("invalid_session", "the session key is not a valid session handle");
  }
}

/**
 * Identity for the /ecc-decide command: the sender OpenClaw attributes to the
 * command (PluginCommandContext.senderId, a channel scoped sender id) and the
 * host bound session of the conversation (PluginCommandContext.sessionKey).
 */
export function identityFromCommandContext(ctx) {
  if (!ctx || typeof ctx !== "object") {
    throw new DecisionError("missing_identity", "no OpenClaw command context");
  }
  if (ctx.isAuthorizedSender !== true) {
    throw new DecisionError("unauthorized", "the sender is not authorized for this command");
  }
  const decidedBy = typeof ctx.senderId === "string" ? ctx.senderId.trim() : "";
  const sessionKey = typeof ctx.sessionKey === "string" ? ctx.sessionKey.trim() : "";
  if (!decidedBy) {
    throw new DecisionError(
      "missing_identity",
      "OpenClaw did not provide a sender identity for this command; decide with the ecc-orchestrator.decide gateway method instead",
    );
  }
  if (!sessionKey) {
    throw new DecisionError("missing_session", "OpenClaw did not provide a session for this command");
  }
  checkHandles(decidedBy, sessionKey);
  return { decidedBy, sessionKey };
}

/**
 * Identity for the ecc-orchestrator.decide gateway method: the connection's
 * authenticated OpenClaw profile (GatewayClient.authenticatedUserProfile,
 * attested by the Gateway, never by request data). Plugin RPC handlers get
 * no host bound session in OpenClaw 2026.9.6, so the caller names the
 * session and it must equal the request's requesting session.
 */
export function identityFromGatewayClient(client, params) {
  if (!client || typeof client !== "object" || client.invalidated) {
    throw new DecisionError("missing_identity", "no authenticated OpenClaw connection");
  }
  if (client.internal && (client.internal.syntheticClient || client.internal.agentToolCaller || client.internal.agentRuntimeIdentity)) {
    throw new DecisionError("missing_identity", "decisions must come from an operator connection, not an agent or synthetic caller");
  }
  const profile = client.authenticatedUserProfile;
  const decidedBy = profile && typeof profile.profileId === "string" ? profile.profileId.trim() : "";
  if (!decidedBy) {
    throw new DecisionError("missing_identity", "the OpenClaw connection has no authenticated profile");
  }
  const sessionKey = params && typeof params.sessionKey === "string" ? params.sessionKey.trim() : "";
  if (!sessionKey) throw new DecisionError("missing_session", "sessionKey is required");
  checkHandles(decidedBy, sessionKey);
  return { decidedBy, sessionKey };
}

/**
 * Build a decision object for a known approval. `nowSeconds` is the plugin
 * clock; the runtime re-checks expiry on its own clock.
 */
export function buildDecision({ approval, decision, identity, planPrefix, nowSeconds }) {
  if (!approval) throw new DecisionError("unknown_request", "unknown approval request id");
  if (!DECISIONS.includes(decision)) throw new DecisionError("invalid_decision", "decision must be approved or rejected");
  if (!identity || !identity.decidedBy) throw new DecisionError("missing_identity", "missing decider identity");
  if (!identity.sessionKey) throw new DecisionError("missing_session", "missing session");
  if (approval.status === "invalid" || (approval.invalid_fields && approval.invalid_fields.length)) {
    throw new DecisionError("invalid_request", `request has invalid fields: ${approval.invalid_fields.join(",")}`);
  }
  const status = effectiveStatus(approval, nowSeconds);
  if (status === "expired") throw new DecisionError("expired", "the approval request has expired");
  if (status !== "pending") throw new DecisionError("not_pending", `the approval request is ${status}`);
  if (approval.submitted) {
    throw new DecisionError("already_submitted", "a decision for this request was already submitted from OpenClaw");
  }
  if (!approval.requesting_session) {
    throw new DecisionError("no_requesting_session", "the request names no requesting session");
  }
  if (identity.sessionKey !== approval.requesting_session) {
    throw new DecisionError(
      "session_mismatch",
      "decide from the requesting OpenClaw session; this session is not bound to the request",
    );
  }
  if (decision === "approved") {
    const prefix = typeof planPrefix === "string" ? planPrefix.trim().toLowerCase() : "";
    if (prefix.length < MIN_PLAN_PREFIX || !/^[0-9a-f]+$/.test(prefix)) {
      throw new DecisionError(
        "plan_confirmation_required",
        `approving requires the first ${MIN_PLAN_PREFIX} or more hex characters of the plan hash`,
      );
    }
    if (!approval.plan_sha256.startsWith(prefix)) {
      throw new DecisionError("plan_confirmation_mismatch", "the confirmed plan hash prefix does not match the request");
    }
  }
  for (const f of ["request_id", "run_id", "unit_id"]) {
    if (!validId(approval[f])) throw new DecisionError("invalid_request", `request field ${f} is invalid`);
  }
  // Binding fields copied verbatim; session_id is the request's requesting
  // session, which the check above proved equal to the caller's session.
  return {
    request_id: approval.request_id,
    run_id: approval.run_id,
    unit_id: approval.unit_id,
    action: approval.action,
    plan_sha256: approval.plan_sha256,
    session_id: approval.requesting_session,
    decision,
    decided_by: identity.decidedBy,
  };
}

async function fsyncDir(dir) {
  let h;
  try {
    h = await fs.open(dir, "r");
    await h.sync();
  } catch {
    // Directory fsync is not supported everywhere; the rename is still atomic.
  } finally {
    if (h) await h.close();
  }
}

/**
 * Write a decision into the inbox atomically: create a hidden temporary file
 * (the runtime ignores hidden files), fsync, then rename to a *.json name.
 */
export async function writeDecisionFile(inboxDir, decision, { now = Date.now(), random = crypto.randomBytes } = {}) {
  const body = `${JSON.stringify(decision)}\n`;
  if (Buffer.byteLength(body) > MAX_DECISION_BYTES) throw new DecisionError("too_large", "decision too large");
  if (!validId(decision.request_id)) throw new DecisionError("invalid_request", "invalid request id");
  await fs.mkdir(inboxDir, { recursive: true, mode: 0o700 });
  const st = await fs.lstat(inboxDir);
  if (st.isSymbolicLink() || !st.isDirectory()) {
    throw new DecisionError("bad_inbox", "the inbox is not a real directory");
  }
  const suffix = random(6).toString("hex");
  const name = `${now}-${decision.request_id}-${suffix}.json`;
  const tmp = path.join(inboxDir, `.${name}.tmp`);
  const final = path.join(inboxDir, name);
  const handle = await fs.open(tmp, "wx", 0o600);
  try {
    await handle.writeFile(body, "utf8");
    await handle.sync();
  } catch (err) {
    await handle.close();
    await fs.rm(tmp, { force: true });
    throw err;
  }
  await handle.close();
  try {
    await fs.rename(tmp, final);
  } catch (err) {
    await fs.rm(tmp, { force: true });
    throw err;
  }
  await fsyncDir(inboxDir);
  return final;
}
