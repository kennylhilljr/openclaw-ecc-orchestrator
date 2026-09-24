"""Conflict prediction from declared scope (before) and changed files (after)."""

import fnmatch
import itertools

from ..tasks.scope import out_of_scope
from ..worktrees.git import git_out

_WILD = "*?["
WHOLE_REPOSITORY = "<whole repository>"


def _norm(pattern):
    p = str(pattern).strip()
    while p.startswith("./"):
        p = p[2:]
    return p.rstrip("/") if p not in ("", "/") else "*"


def _is_glob(p):
    return any(ch in p for ch in _WILD)


def _prefix(p):
    for i, ch in enumerate(p):
        if ch in _WILD:
            return p[:i]
    return p


def _under(path, directory):
    return path == directory or path.startswith(directory + "/")


def patterns_overlap(a, b):
    """Conservative: True whenever two scope entries could name the same file."""
    a, b = _norm(a), _norm(b)
    if a == b:
        return True
    ga, gb = _is_glob(a), _is_glob(b)
    if not ga and not gb:
        return _under(a, b) or _under(b, a)
    if ga and gb:
        pa, pb = _prefix(a), _prefix(b)
        return pa.startswith(pb) or pb.startswith(pa)
    literal, glob = (a, b) if gb else (b, a)
    if fnmatch.fnmatchcase(literal, glob):
        return True
    gp = _prefix(glob)
    # A literal directory containing the glob's fixed part, or a glob able
    # to match files beneath the literal directory.
    if _under(gp.rstrip("/"), literal) and gp.rstrip("/") != "":
        return True
    return fnmatch.fnmatchcase(literal + "/x", glob)


def scope_overlaps(files_a, files_b):
    """Overlapping entry pairs. An empty scope is the whole repository and
    overlaps everything, so a unit that declares no files always runs alone."""
    files_a, files_b = list(files_a or []), list(files_b or [])
    if not files_a or not files_b:
        return [[files_a[0] if files_a else WHOLE_REPOSITORY, files_b[0] if files_b else WHOLE_REPOSITORY]]
    return [[a, b] for a in files_a for b in files_b if patterns_overlap(a, b)]


def _scope(unit):
    return list((unit.get("scope") or {}).get("files") or [])


def predict_scope_conflicts(units):
    """Pairs of units whose declared scopes overlap."""
    pairs = []
    for ua, ub in itertools.combinations(units, 2):
        overlaps = scope_overlaps(_scope(ua), _scope(ub))
        if overlaps:
            pairs.append({"a": ua["id"], "b": ub["id"], "overlaps": overlaps})
    return pairs


def changed_files(repo, branch, target):
    """Files changed on `branch` since its merge base with `target`."""
    base = git_out(["merge-base", target, branch], repo)
    out = git_out(["diff", "--name-only", "--no-renames", base, branch], repo)
    return sorted(line for line in out.splitlines() if line)


def diff_stats(repo, branch, target):
    """`{"files", "files_changed", "lines_added", "lines_removed"}` for the
    unit's own changes (merge base of `target` and `branch` to `branch`).
    Binary files count as changed files with zero lines."""
    base = git_out(["merge-base", target, branch], repo)
    out = git_out(["diff", "--numstat", "--no-renames", base, branch], repo)
    files, added, removed = [], 0, 0
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        a, r, name = parts
        files.append(name)
        added += int(a) if a.isdigit() else 0
        removed += int(r) if r.isdigit() else 0
    files.sort()
    return {"files": files, "files_changed": len(files), "lines_added": added, "lines_removed": removed}


def predict_changed_conflicts(repo, target, branches):
    """`branches` maps unit id to branch; returns pairs touching the same files."""
    changed = {uid: set(changed_files(repo, br, target)) for uid, br in sorted(branches.items())}
    pairs = []
    for a, b in itertools.combinations(sorted(changed), 2):
        common = sorted(changed[a] & changed[b])
        if common:
            pairs.append({"a": a, "b": b, "files": common})
    return pairs


def out_of_scope_changes(changed, scope_files):
    """Changed files outside the declared scope. Exact glob semantics (a
    ``*`` never crosses ``/``), literal directories cover their subtree, and
    an empty scope is the whole repository. A non empty result blocks merge."""
    return out_of_scope(list(changed), list(scope_files or []))
