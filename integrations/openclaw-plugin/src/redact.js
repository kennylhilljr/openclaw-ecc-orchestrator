// Display side secret protection. The runtime already redacts every event
// payload before it is written; this module is a second, conservative layer so
// the plugin never renders or logs a credential even if an upstream redaction
// misses one. It never changes binding fields (ids, plan hashes, head shas):
// those are validated separately and rendered as exact values.

export const REDACTED = "[REDACTED]";
export const MAX_DISPLAY_STRING = 500;

const NAME_WORDS =
  "token|secret|passw(?:or)?d|passphrase|api[_-]?key|access[_-]?key|private[_-]?key" +
  "|credential|cookie|session[_-]?id|auth|bearer|signature";

// Same shape as the runtime's looks_secret_name, plus a few extra words.
const SECRET_NAME_RE = new RegExp(`(?:${NAME_WORDS})|(?:^|[_.-])(?:key|pass|pwd)(?:$|[_.-])`, "i");

const PREFIX_PATTERNS = [
  "-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\\s\\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)",
  "sk-ant-[A-Za-z0-9_\\-]{8,}",
  "sk-proj-[A-Za-z0-9_\\-]{8,}",
  "sk-or-[A-Za-z0-9_\\-]{8,}",
  "sk-[A-Za-z0-9_\\-]{16,}",
  "gsk_[A-Za-z0-9]{16,}",
  "AIza[A-Za-z0-9_\\-]{20,}",
  "github_pat_[A-Za-z0-9_]{16,}",
  "gh[pousr]_[A-Za-z0-9]{20,}",
  "glpat-[A-Za-z0-9_\\-]{16,}",
  "xox[a-z]-[A-Za-z0-9\\-]{10,}",
  "(?:AKIA|ASIA)[A-Z0-9]{16}",
  "hf_[A-Za-z0-9]{20,}",
  "eyJ[A-Za-z0-9_\\-]{8,}\\.eyJ[A-Za-z0-9_\\-]{8,}\\.[A-Za-z0-9_\\-]{8,}",
];
const PREFIX_RE = new RegExp(PREFIX_PATTERNS.map((p) => `(?:${p})`).join("|"), "g");

// Rules that keep context and mask only the value.
const VALUE_RULES = [
  // name=value, name: value, "name": "value" with a secret looking name.
  new RegExp(
    `(\\b[A-Za-z0-9_.-]*(?:${NAME_WORDS}|_key\\b|_pass\\b|_pwd\\b)[A-Za-z0-9_.-]*["']?\\s*[:=]\\s*)("[^"]*"|'[^']*'|[^\\s,;"']+)`,
    "gi",
  ),
  // Authorization headers.
  /(\b(?:proxy-)?authorization\s*[:=]\s*(?:[A-Za-z]+\s+)?)([^\s,;"']{4,})/gi,
  // Bearer / Basic credentials.
  /(\b(?:bearer|basic)\s+)([A-Za-z0-9._~+/=-]{8,})/gi,
  // scheme://user:password@host
  /(\b[a-z][a-z0-9+.-]*:\/\/[^/\s:@]+:)([^/\s@]+)(@)/gi,
];

// Long opaque tokens: at least 32 characters mixing upper case, lower case and
// digits. Pure hex (plan hashes, commit shas) is deliberately left alone.
const OPAQUE_RE = /[A-Za-z0-9+_=-]{32,}/g;
const HEX_RE = /^[0-9a-fA-F]+$/;

// C0 controls except tab and newline, DEL, C1 controls, bidi overrides.
// eslint-disable-next-line no-control-regex
const CONTROL_RE = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f‪-‮⁦-⁩]/g;

export function looksSecretName(name) {
  return SECRET_NAME_RE.test(String(name));
}

function maskOpaque(match) {
  if (HEX_RE.test(match)) return match;
  const hasUpper = /[A-Z]/.test(match);
  const hasLower = /[a-z]/.test(match);
  const hasDigit = /[0-9]/.test(match);
  return hasUpper && hasLower && hasDigit ? REDACTED : match;
}

/** Mask secret looking substrings and strip control characters. */
export function maskText(value, maxLength = MAX_DISPLAY_STRING) {
  if (value === null || value === undefined) return "";
  let text = String(value);
  if (text.length > maxLength * 4) text = text.slice(0, maxLength * 4);
  text = text.replace(CONTROL_RE, "");
  text = text.replace(PREFIX_RE, REDACTED);
  for (const rule of VALUE_RULES) {
    text = text.replace(rule, (...m) => {
      // Groups: 1 prefix, 2 value, optional 3 suffix.
      const suffix = typeof m[3] === "string" ? m[3] : "";
      return `${m[1]}${REDACTED}${suffix}`;
    });
  }
  text = text.replace(OPAQUE_RE, maskOpaque);
  if (text.length > maxLength) text = `${text.slice(0, maxLength)}...[truncated]`;
  return text;
}

/**
 * Recursively sanitize a JSON value for display: values under secret looking
 * keys are replaced entirely, strings are masked and bounded, depth and
 * collection sizes are capped.
 */
export function sanitize(value, { depth = 0, maxDepth = 6, maxItems = 50, maxLength = MAX_DISPLAY_STRING } = {}) {
  if (value === null || value === undefined) return value ?? null;
  if (typeof value === "string") return maskText(value, maxLength);
  if (typeof value === "number" || typeof value === "boolean") return value;
  if (depth >= maxDepth) return "[depth limit]";
  const opts = { depth: depth + 1, maxDepth, maxItems, maxLength };
  if (Array.isArray(value)) {
    const out = value.slice(0, maxItems).map((v) => sanitize(v, opts));
    if (value.length > maxItems) out.push(`[${value.length - maxItems} more]`);
    return out;
  }
  if (typeof value === "object") {
    const out = {};
    let count = 0;
    for (const [key, v] of Object.entries(value)) {
      if (count >= maxItems) break;
      count += 1;
      const safeKey = maskText(key, 100);
      out[safeKey] = looksSecretName(key) ? REDACTED : sanitize(v, opts);
    }
    return out;
  }
  return null;
}

/** True when a string contains something the mask would hide. */
export function containsSecret(value) {
  const text = String(value ?? "");
  return maskText(text, Number.MAX_SAFE_INTEGER) !== text.replace(CONTROL_RE, "");
}
