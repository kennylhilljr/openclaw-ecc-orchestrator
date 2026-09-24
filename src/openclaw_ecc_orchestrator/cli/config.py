"""CLI configuration: flags over an optional JSON config file over defaults.

Defaults are derived at runtime from XDG style directories under the
invoking user's home; nothing personal is baked in. Every path is checked
before use: no `..` components, no NUL bytes, and state locations never
inside a repository, a `.git` directory or any `.openclaw` directory.
"""

import os
import re

from ..handoffs.redaction import contains_secret
from ..runs.store import valid_identifier

APP = "openclaw-ecc-orchestrator"
MAX_CONFIG_BYTES = 256 * 1024

# key -> kind. "path" keys are resolved relative to the config file.
CONFIG_KEYS = {
    "state_dir": "path",
    "worktree_root": "path",
    "repo": "path",
    "target_branch": "str",
    "policy_file": "path",
    "event_log": "path",
    "decision_inbox": "path",
    "certifications": "path",
    "catalog": "path",
    "repo_checks": "object",
    "conductor_id": "id",
    "lease_ttl": "number",
    "runner_timeout": "number",
    "gate_timeout": "number",
    "approval_ttl": "number",
    "review_ttl": "number",
    "tier_costs": "object",
}

FLAG_KEYS = ("state_dir", "worktree_root", "repo", "target_branch", "policy_file", "event_log", "decision_inbox",
             "certifications")

_HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


class UsageError(ValueError):
    """Bad invocation or configuration (exit code 2)."""

    def __init__(self, message, check_name="usage"):
        super().__init__(message)
        self.check_name = check_name


def valid_id(value):
    return valid_identifier(value) and not value.startswith(".")


def valid_handle(value):
    """Operator and session handles: short display ids, never emails or secrets."""
    return isinstance(value, str) and bool(_HANDLE_RE.match(value)) and not contains_secret(value)


def _components(path):
    return [p for p in re.split(r"[\\/]+", path) if p]


def checked_path(value, label, base=None):
    """Absolute path for `value`; refuses traversal before any resolution."""
    if not isinstance(value, str) or not value.strip():
        raise UsageError(f"{label} must be a non-empty path", "config_path_valid")
    if "\0" in value:
        raise UsageError(f"{label} contains a NUL byte", "config_path_valid")
    if ".." in _components(value):
        raise UsageError(f"{label} must not contain '..' components", "config_path_valid")
    path = os.path.expanduser(value) if value.startswith("~") else value
    if not os.path.isabs(path):
        path = os.path.join(base or os.getcwd(), path)
    return os.path.normpath(path)


def inside(child, parent):
    child, parent = os.path.realpath(child), os.path.realpath(parent)
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def forbidden_location(path, home, repo=None):
    """Reason a state location is unacceptable, or None."""
    real = os.path.realpath(path)
    parts = set(_components(path)) | set(_components(real))
    if ".openclaw" in parts:
        return "inside an .openclaw directory"
    if ".git" in parts:
        return "inside a .git directory"
    if home and inside(real, os.path.join(home, ".openclaw")):
        return "inside ~/.openclaw"
    if repo and inside(real, repo):
        return "inside the repository"
    return None


def xdg_dir(env, name, fallback):
    value = env.get(name)
    if value and os.path.isabs(value):
        return value
    home = env.get("HOME")
    if not home or not os.path.isabs(home):
        raise UsageError(f"cannot derive a default: HOME is not an absolute path; set {name} or pass the path",
                         "config_defaults")
    return os.path.join(home, *fallback)


class Config:
    """Resolved configuration. Attribute access only; `public()` for output."""

    def __init__(self, values, home):
        self.__dict__.update(values)
        self.home = home

    @property
    def runs_dir(self):
        return os.path.join(self.state_dir, "runs")

    @property
    def approvals_path(self):
        return os.path.join(self.state_dir, "approvals.json")

    @property
    def queue_path(self):
        return os.path.join(self.state_dir, "merge-queue.json")

    @property
    def reviews_dir(self):
        return os.path.join(self.state_dir, "reviews")

    @property
    def logs_dir(self):
        return os.path.join(self.state_dir, "logs")

    @property
    def scratch_root(self):
        return os.path.join(self.worktree_root, "_merge-scratch")

    def public(self):
        keys = ("state_dir", "worktree_root", "repo", "target_branch", "policy_file", "event_log",
                "decision_inbox", "certifications", "catalog", "conductor_id")
        return {k: getattr(self, k) for k in keys}


def _load_file(path):
    import json

    try:
        st = os.stat(path)
    except OSError:
        raise UsageError(f"config file not found: {path}", "config_readable") from None
    if not os.path.isfile(path) or st.st_size > MAX_CONFIG_BYTES:
        raise UsageError(f"config file must be a regular file under {MAX_CONFIG_BYTES} bytes", "config_readable")
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError, UnicodeDecodeError):
        raise UsageError(f"config file is not valid JSON: {path}", "config_readable") from None
    if not isinstance(doc, dict):
        raise UsageError("config file must hold a JSON object", "config_readable")
    unknown = sorted(set(doc) - set(CONFIG_KEYS))
    if unknown:
        raise UsageError(f"unknown config keys: {', '.join(unknown)}", "config_keys_known")
    return doc


def _typed(key, value, base):
    kind = CONFIG_KEYS[key]
    if value is None:
        return None
    if kind == "path":
        return checked_path(value, f"config {key}", base)
    if kind == "str":
        if not isinstance(value, str) or not _BRANCH_RE.match(value) or ".." in value:
            raise UsageError(f"config {key} must be a branch name", "config_value_valid")
        return value
    if kind == "id":
        if not valid_id(value):
            raise UsageError(f"config {key} must be an identifier", "config_value_valid")
        return value
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise UsageError(f"config {key} must be a positive number", "config_value_valid")
        return float(value)
    if kind == "object":
        if not isinstance(value, dict):
            raise UsageError(f"config {key} must be an object", "config_value_valid")
        return value
    raise AssertionError(kind)


def load_config(args, env, cwd, validate_locations=True):
    """Resolve the configuration for one invocation. Raises UsageError.
    `validate_locations=False` (doctor) reports instead of refusing."""
    values = {}
    config_path = getattr(args, "config", None)
    if config_path:
        path = checked_path(config_path, "--config", cwd)
        base = os.path.dirname(path)
        for key, value in _load_file(path).items():
            values[key] = _typed(key, value, base)
    for key in FLAG_KEYS:
        flag = getattr(args, key, None)
        if flag is not None:
            values[key] = _typed(key, flag, cwd) if CONFIG_KEYS[key] != "path" else checked_path(
                flag, "--" + key.replace("_", "-"), cwd)
    home = env.get("HOME") if env.get("HOME") and os.path.isabs(env.get("HOME")) else None
    if values.get("state_dir") is None:
        values["state_dir"] = os.path.join(xdg_dir(env, "XDG_STATE_HOME", (".local", "state")), APP)
    if values.get("worktree_root") is None:
        values["worktree_root"] = os.path.join(xdg_dir(env, "XDG_DATA_HOME", (".local", "share")), APP, "worktrees")
    if values.get("repo") is None:
        values["repo"] = os.path.normpath(cwd)
    values.setdefault("target_branch", "main")
    if values["target_branch"] is None:
        values["target_branch"] = "main"
    state = values["state_dir"]
    if values.get("event_log") is None:
        values["event_log"] = os.path.join(state, "openclaw-events.jsonl")
    if values.get("decision_inbox") is None:
        values["decision_inbox"] = os.path.join(state, "decisions")
    if values.get("policy_file") is None:
        values["policy_file"] = None
        values["policy_default"] = os.path.join(values["repo"], ".orchestration", "config.yaml")
    values.setdefault("certifications", None)
    values.setdefault("catalog", None)
    values.setdefault("repo_checks", None)
    values.setdefault("tier_costs", None)
    values["conductor_id"] = values.get("conductor_id") or "ecc-cli"
    defaults = {"lease_ttl": 300.0, "runner_timeout": 3600.0, "gate_timeout": 600.0, "approval_ttl": 3600.0,
                "review_ttl": 3600.0}
    for key, value in defaults.items():
        if values.get(key) is None:
            values[key] = value
    cfg = Config(values, home)
    if not validate_locations:
        return cfg
    for key in ("state_dir", "event_log", "decision_inbox"):
        reason = forbidden_location(getattr(cfg, key), home)
        if reason:
            raise UsageError(f"{key} is {reason}", "state_location_allowed")
    return cfg


def check_state_outside_repo(cfg, repo_top):
    for key in ("state_dir", "event_log", "decision_inbox"):
        reason = forbidden_location(getattr(cfg, key), cfg.home, repo_top)
        if reason:
            raise UsageError(f"{key} is {reason}", "state_location_allowed")
