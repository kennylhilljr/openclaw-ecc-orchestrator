// Plugin configuration validation. Configuration comes from
// plugins.entries.ecc-orchestrator.config in the OpenClaw config file
// (api.pluginConfig). Every path must be absolute, free of traversal, and
// outside ~/.openclaw and any .openclaw or .git directory.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export class ConfigError extends Error {}

const KNOWN_KEYS = new Set([
  "eventLog",
  "inbox",
  "stateDir",
  "runtimeCli",
  "runtimeCliArgs",
  "pollIntervalMs",
]);

const FORBIDDEN_COMPONENTS = new Set([".openclaw", ".git"]);

function nearestExistingRealpath(p) {
  // Resolve symlinks on the longest existing prefix so a symlink cannot
  // smuggle a path into ~/.openclaw.
  let current = p;
  const rest = [];
  for (;;) {
    try {
      const real = fs.realpathSync.native(current);
      return rest.length ? path.join(real, ...rest.reverse()) : real;
    } catch {
      const parent = path.dirname(current);
      if (parent === current) return p;
      rest.push(path.basename(current));
      current = parent;
    }
  }
}

function insideDir(child, dir) {
  const rel = path.relative(dir, child);
  return rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel));
}

/**
 * Validate one configured path. Returns the normalized absolute path or
 * throws ConfigError. `home` is injectable for tests.
 */
export function validatePath(value, name, { home = os.homedir() } = {}) {
  if (typeof value !== "string" || value.length === 0) {
    throw new ConfigError(`${name} must be a non-empty string`);
  }
  if (value.length > 4096) throw new ConfigError(`${name} is too long`);
  if (value.includes("\0")) throw new ConfigError(`${name} contains a NUL byte`);
  if (!path.isAbsolute(value)) throw new ConfigError(`${name} must be an absolute path`);
  const components = value.split(/[\\/]+/);
  if (components.includes("..") || components.includes(".")) {
    throw new ConfigError(`${name} must not contain '.' or '..' components`);
  }
  const normalized = path.resolve(value);
  const real = nearestExistingRealpath(normalized);
  for (const candidate of [normalized, real]) {
    const parts = candidate.split(path.sep);
    if (parts.some((part) => FORBIDDEN_COMPONENTS.has(part))) {
      throw new ConfigError(`${name} must not be inside a .openclaw or .git directory`);
    }
    for (const base of [path.join(home, ".openclaw"), nearestExistingRealpath(path.join(home, ".openclaw"))]) {
      if (insideDir(candidate, base)) {
        throw new ConfigError(`${name} must not be inside ~/.openclaw`);
      }
    }
  }
  return normalized;
}

/** Validate and normalize the whole plugin config object. */
export function parseConfig(raw, { home = os.homedir() } = {}) {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    throw new ConfigError("plugin config must be an object with eventLog, inbox and stateDir");
  }
  for (const key of Object.keys(raw)) {
    if (!KNOWN_KEYS.has(key)) throw new ConfigError(`unknown config key ${JSON.stringify(key)}`);
  }
  const cfg = {
    eventLog: validatePath(raw.eventLog, "eventLog", { home }),
    inbox: validatePath(raw.inbox, "inbox", { home }),
    stateDir: validatePath(raw.stateDir, "stateDir", { home }),
    runtimeCli: null,
    runtimeCliArgs: [],
    pollIntervalMs: 1000,
  };
  if (cfg.inbox === cfg.stateDir || insideDir(cfg.stateDir, cfg.inbox) || insideDir(cfg.inbox, cfg.stateDir)) {
    throw new ConfigError("inbox and stateDir must be separate directories");
  }
  if (insideDir(cfg.eventLog, cfg.inbox)) throw new ConfigError("eventLog must not be inside the inbox");
  if (raw.runtimeCli !== undefined && raw.runtimeCli !== null) {
    cfg.runtimeCli = validatePath(raw.runtimeCli, "runtimeCli", { home });
  }
  if (raw.runtimeCliArgs !== undefined) {
    if (!Array.isArray(raw.runtimeCliArgs) || raw.runtimeCliArgs.length > 16) {
      throw new ConfigError("runtimeCliArgs must be an array of at most 16 strings");
    }
    for (const arg of raw.runtimeCliArgs) {
      if (typeof arg !== "string" || arg.length === 0 || arg.length > 1024 || arg.includes("\0")) {
        throw new ConfigError("runtimeCliArgs entries must be non-empty strings without NUL bytes");
      }
      if (arg.split(/[\\/]+/).includes("..")) throw new ConfigError("runtimeCliArgs must not contain '..'");
    }
    cfg.runtimeCliArgs = [...raw.runtimeCliArgs];
  }
  if (raw.pollIntervalMs !== undefined) {
    const n = raw.pollIntervalMs;
    if (!Number.isInteger(n) || n < 250 || n > 60000) {
      throw new ConfigError("pollIntervalMs must be an integer between 250 and 60000");
    }
    cfg.pollIntervalMs = n;
  }
  return Object.freeze(cfg);
}
