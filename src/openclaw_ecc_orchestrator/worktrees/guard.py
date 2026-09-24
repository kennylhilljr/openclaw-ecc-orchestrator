"""Detect worker mutations of git state shared by every worktree of a repository.

A unit's worktree shares refs, config, hooks and object storage with the main
repository, so a worker can move the target branch, add a hook, or set
``core.fsmonitor`` from inside its own checkout. Take a snapshot before the
runner starts and compare after it exits::

    before = snapshot_shared_git_state(repo)
    ...run the unit...
    changes = diff_shared_git_state(before, snapshot_shared_git_state(repo),
                                    allow_refs=["refs/heads/ecc/<run>/<unit>"])

Every string in ``changes`` is a human readable violation. Config values are
never echoed (they may hold credentials or payloads); only key names are.
Snapshots are plain JSON serialisable dicts. All git calls go through the
hardened wrapper (minimal env, no hooks, no fsmonitor).
"""

import fnmatch
import hashlib
import os

from .git import git_out, run_git

REF_NAMESPACES = ("refs/heads", "refs/tags", "refs/remotes", "refs/replace")

# Config keys (lower case, fnmatch patterns) whose change can execute code,
# redirect data, or hide work.
RISKY_CONFIG_PATTERNS = (
    "core.fsmonitor", "core.hookspath", "core.sshcommand", "core.editor", "core.pager", "core.askpass",
    "core.gitproxy", "core.worktree", "core.bare", "core.attributesfile", "core.excludesfile",
    "core.repositoryformatversion", "extensions.*",
    "alias.*", "include.*", "includeif.*", "credential.*", "remote.*.url", "remote.*.pushurl",
    "remote.*.fetch", "remote.*.uploadpack", "remote.*.receivepack", "remote.*.proxy",
    "url.*.insteadof", "url.*.pushinsteadof", "filter.*", "diff.external", "diff.*.textconv",
    "diff.*.command", "merge.*.driver", "sequence.editor", "gpg.program", "gpg.*.program",
    "protocol.*", "uploadpack.*", "receive.*", "http.*", "safe.directory", "submodule.*",
    "branch.*.remote", "branch.*.merge", "push.*", "transfer.*", "fetch.*",
)

_SHARED_FILES = (
    ("packed_refs_sha256", "packed-refs"),
    ("config_sha256", "config"),
    ("config_worktree_sha256", "config.worktree"),
    ("info_attributes_sha256", os.path.join("info", "attributes")),
    ("info_exclude_sha256", os.path.join("info", "exclude")),
    ("alternates_sha256", os.path.join("objects", "info", "alternates")),
)


def _sha256_file(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except FileNotFoundError:
        return None
    except IsADirectoryError:
        return "directory"


def _common_dir(repo_path):
    raw = git_out(["rev-parse", "--git-common-dir"], repo_path)
    if not os.path.isabs(raw):
        raw = os.path.join(repo_path, raw)
    return os.path.realpath(raw)


def _head(common_dir):
    # The main worktree's HEAD file lives in the common dir itself.
    try:
        with open(os.path.join(common_dir, "HEAD")) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _refs(repo_path):
    out = git_out(["for-each-ref", "--format=%(refname) %(objectname)", *REF_NAMESPACES], repo_path)
    refs = {}
    for line in out.splitlines():
        name, _, oid = line.rpartition(" ")
        if name:
            refs[name] = oid
    return refs


def _risky(key):
    key = key.lower()
    return any(fnmatch.fnmatchcase(key, pattern) for pattern in RISKY_CONFIG_PATTERNS)


def _config_digests(path):
    """Map every risky key in ``path`` to a digest of its values (never the values)."""
    if not os.path.isfile(path):
        return {}
    proc = run_git(["config", "--file", path, "--no-includes", "--null", "--list"], os.path.dirname(path),
                   check=False)
    if proc.returncode != 0:
        return {"<unparsable>": hashlib.sha256(proc.stderr.encode()).hexdigest()}
    values = {}
    for entry in proc.stdout.split("\0"):
        if not entry:
            continue
        key, _, value = entry.partition("\n")
        if _risky(key):
            values.setdefault(key.lower(), []).append(value)
    return {k: hashlib.sha256("\0".join(v).encode()).hexdigest() for k, v in sorted(values.items())}


def _hooks(common_dir):
    hooks_dir = os.path.join(common_dir, "hooks")
    listing = {}
    if os.path.isdir(hooks_dir):
        for name in sorted(os.listdir(hooks_dir)):
            path = os.path.join(hooks_dir, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if os.path.islink(path):
                digest = "symlink:" + os.readlink(path)
            elif os.path.isfile(path):
                digest = _sha256_file(path)
            else:
                digest = "dir"
            listing[name] = f"{oct(st.st_mode & 0o7777)}:{digest}"
    blob = "\n".join(f"{k}={v}" for k, v in sorted(listing.items()))
    return listing, hashlib.sha256(blob.encode()).hexdigest()


def snapshot_shared_git_state(repo_path) -> dict:
    """Snapshot the git state shared by all worktrees of ``repo_path``."""
    common_dir = _common_dir(repo_path)
    snap = {
        "common_dir": common_dir,
        "head": _head(common_dir),
        "refs": _refs(repo_path),
    }
    for key, rel in _SHARED_FILES:
        snap[key] = _sha256_file(os.path.join(common_dir, rel))
    risky = {}
    for label, rel in (("config", "config"), ("config.worktree", "config.worktree")):
        for key, digest in _config_digests(os.path.join(common_dir, rel)).items():
            risky[f"{label}:{key}"] = digest
    snap["risky_config"] = risky
    snap["hooks"], snap["hooks_sha256"] = _hooks(common_dir)
    return snap


def _allowed(ref, allow_refs):
    for pattern in allow_refs or ():
        pattern = str(pattern)
        candidates = {pattern} if pattern.startswith("refs/") else {pattern, "refs/heads/" + pattern}
        if any(ref == c or fnmatch.fnmatchcase(ref, c) for c in candidates):
            return True
    return False


_LABELS = {
    "packed_refs_sha256": "packed-refs",
    "config_sha256": "shared config file",
    "config_worktree_sha256": "shared config.worktree file",
    "info_attributes_sha256": "info/attributes (applies to every worktree)",
    "info_exclude_sha256": "info/exclude",
    "alternates_sha256": "objects/info/alternates",
}


def _short(oid):
    return (oid or "")[:12] or "none"


def diff_shared_git_state(before, after, allow_refs=()) -> list:
    """Human readable changes between two snapshots, ignoring ``allow_refs``.

    ``allow_refs`` entries are full ref names, glob patterns, or branch names
    (``ecc/r1/u1`` means ``refs/heads/ecc/r1/u1``).
    """
    changes = []
    if before.get("common_dir") != after.get("common_dir"):
        changes.append("snapshots are from different repositories")
    if before.get("head") != after.get("head"):
        changes.append(f"main worktree HEAD changed: {before.get('head')!r} -> {after.get('head')!r}")
    b_refs, a_refs = before.get("refs") or {}, after.get("refs") or {}
    for ref in sorted(set(b_refs) | set(a_refs)):
        if _allowed(ref, allow_refs):
            continue
        old, new = b_refs.get(ref), a_refs.get(ref)
        if old == new:
            continue
        if old is None:
            changes.append(f"ref created: {ref} at {_short(new)}")
        elif new is None:
            changes.append(f"ref deleted: {ref} (was {_short(old)})")
        else:
            changes.append(f"ref moved: {ref} {_short(old)} -> {_short(new)}")
    for key, label in _LABELS.items():
        if key == "packed_refs_sha256" and before.get(key) != after.get(key):
            # Only a violation when the packed content changed beyond allowed refs.
            if not _only_allowed_ref_changes(b_refs, a_refs, allow_refs):
                changes.append("packed-refs changed")
            continue
        if before.get(key) != after.get(key):
            changes.append(f"{label} changed")
    b_cfg, a_cfg = before.get("risky_config") or {}, after.get("risky_config") or {}
    for key in sorted(set(b_cfg) | set(a_cfg)):
        if b_cfg.get(key) == a_cfg.get(key):
            continue
        state = "added" if key not in b_cfg else "removed" if key not in a_cfg else "changed"
        changes.append(f"risky config {state}: {key}")
    b_hooks, a_hooks = before.get("hooks") or {}, after.get("hooks") or {}
    for name in sorted(set(b_hooks) | set(a_hooks)):
        if b_hooks.get(name) == a_hooks.get(name):
            continue
        state = "added" if name not in b_hooks else "removed" if name not in a_hooks else "modified"
        changes.append(f"hook {state}: hooks/{name}")
    if before.get("hooks_sha256") != after.get("hooks_sha256") and b_hooks == a_hooks:
        changes.append("hooks directory changed")
    return changes


def _only_allowed_ref_changes(b_refs, a_refs, allow_refs):
    changed = [r for r in set(b_refs) | set(a_refs) if b_refs.get(r) != a_refs.get(r)]
    return bool(changed) and all(_allowed(r, allow_refs) for r in changed)


__all__ = ["snapshot_shared_git_state", "diff_shared_git_state", "RISKY_CONFIG_PATTERNS", "REF_NAMESPACES"]
