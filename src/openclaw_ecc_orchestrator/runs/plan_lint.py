"""Deterministic, read-only semantic linting for orchestration plans."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from functools import lru_cache

from ..gates.runner import INLINE_CODE_FLAGS
from .dag import analyze_plan, normalize_units
from ..tasks.scope import (
    glob_match,
    is_glob,
    normalize,
    scope_contains,
    unsafe_path_reason,
)


_PATH_SUFFIXES = (
    ".c",
    ".cc",
    ".cpp",
    ".go",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".py",
    ".rs",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
)

# These options name files created by common acceptance tools.  Keep this
# allowlist explicit: treating every option argument as an output would hide
# genuine input-path mistakes (for example, pytest's ``--rootdir``).
_OUTPUT_PATH_FLAGS = {
    "--html",
    "--json-report-file",
    "--junit-xml",
    "--junitxml",
    "--log-file",
    "--output",
    "--output-file",
    "-o",
}


def _finding(code, message, *, unit_ids=(), paths=()):
    return {"code": code, "message": message, "unit_ids": sorted(unit_ids), "paths": sorted(paths)}


def _digest(findings):
    raw = json.dumps(findings, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _ancestors(ids, deps):
    result = {}
    for uid in ids:
        found, pending = set(), list(deps.get(uid, ()))
        while pending:
            dep = pending.pop()
            if dep not in found:
                found.add(dep)
                pending.extend(deps.get(dep, ()))
        result[uid] = found
    return result


def _patterns_overlap(left, right, target_paths):
    # Existing paths give exact, slash-sensitive evidence. Literal directory
    # containment also detects planned paths which do not exist at the target.
    if any(scope_contains(p, left) and scope_contains(p, right) for p in target_paths):
        return True
    for a in left:
        for b in right:
            if not is_glob(a) and not is_glob(b):
                na, nb = normalize(a).rstrip("/"), normalize(b).rstrip("/")
                if na == nb or na.startswith(nb + "/") or nb.startswith(na + "/"):
                    return True
            elif not is_glob(a) and (glob_match(normalize(a), b) or glob_match(normalize(a) + "/x", b)):
                return True
            elif not is_glob(b) and (glob_match(normalize(b), a) or glob_match(normalize(b) + "/x", a)):
                return True
            elif is_glob(a) and is_glob(b):
                # A target snapshot cannot prove overlap for files that both
                # units intend to add. Generate concrete strings from each
                # pattern and accept only a real witness matched by both.
                if any(glob_match(witness, b) for witness in _glob_witnesses(a)):
                    return True
                if any(glob_match(witness, a) for witness in _glob_witnesses(b)):
                    return True
    return False


@lru_cache(maxsize=4096)
def _glob_witnesses(pattern):
    """Return bounded concrete paths that are guaranteed to match pattern."""
    pattern = normalize(pattern)
    witnesses = [""]
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            choices, index = ("", "x/"), index + 3
        elif pattern.startswith("**", index):
            choices, index = ("", "x"), index + 2
        elif pattern[index] == "*":
            choices, index = ("", "x"), index + 1
        elif pattern[index] == "?":
            choices, index = ("x",), index + 1
        else:
            choices, index = (pattern[index],), index + 1
        witnesses = [prefix + choice for prefix in witnesses for choice in choices][:64]
    return tuple(witness for witness in witnesses if glob_match(witness, pattern))


def _path_present(path, target_paths):
    """Return whether a literal directory or pattern exists in the target tree."""
    path = normalize(path).rstrip("/")
    if path in {"", "."}:
        return True
    if is_glob(path):
        return any(glob_match(target_path, path) for target_path in target_paths)
    return any(
        target_path == path or target_path.startswith(path + "/")
        for target_path in target_paths
    )


def _target_in_scope(path, scope):
    """Match a path obtained from Git without repeating user-input safety work."""
    if not scope:
        return True
    for entry in scope:
        prefix = _glob_literal_prefix(entry)
        if prefix and not path.startswith(prefix):
            continue
        if glob_match(path, entry):
            return True
        if not is_glob(entry):
            base = normalize(entry).rstrip("/")
            if base and (path == base or path.startswith(base + "/")):
                return True
    return False


@lru_cache(maxsize=4096)
def _glob_literal_prefix(pattern):
    pattern = normalize(pattern)
    wildcard = min((pattern.find(char) for char in "*?" if char in pattern), default=-1)
    return pattern[:wildcard] if wildcard >= 0 else ""


def _looks_like_repository_path(token):
    """Recognize path-shaped command arguments without running the command."""
    if "://" in token:
        return False
    normalized = token.replace("\\", "/")
    return (
        "/" in normalized
        or normalized.startswith((".", "~"))
        or normalized.lower().endswith(_PATH_SUFFIXES)
        or normalized in {"docs", "schemas", "scripts", "src", "tests"}
    )


def _argument_paths(tokens, program):
    """Extract path-shaped arguments, excluding an interpreter's inline code."""
    paths = []
    skip_next = False
    inline_flags = INLINE_CODE_FLAGS.get(program, set())
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token in inline_flags:
            skip_next = True
            continue
        if any(flag.startswith("--") and token.startswith(flag + "=") for flag in inline_flags):
            continue
        if token in _OUTPUT_PATH_FLAGS:
            skip_next = True
            continue
        if any(
            flag.startswith("--") and token.startswith(flag + "=")
            for flag in _OUTPUT_PATH_FLAGS
        ):
            continue
        if token in {"--command", "-m"}:
            # Module names and non-interpreter inline commands are not paths.
            skip_next = True
            continue
        if token.startswith("-"):
            if "=" not in token:
                continue
            token = token.split("=", 1)[1]
        token = token.split("::", 1)[0]
        if token.endswith("/..."):
            token = token[:-3] + "**"
        if token and _looks_like_repository_path(token):
            paths.append(token)
    return paths


def _segment_paths(tokens):
    """Extract paths from one simple command, including an ``env`` wrapper."""
    paths = []
    index = 0
    while index < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[index]):
        index += 1
    if index >= len(tokens):
        return paths

    program = re.sub(r"[-_]?\d[\d.]*$", "", tokens[index].rsplit("/", 1)[-1].lower())
    index += 1
    if program == "env":
        while index < len(tokens):
            token = tokens[index]
            if token == "--":
                index += 1
                break
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
                index += 1
                continue
            if token in {"-u", "--unset"}:
                index += 2
                continue
            if token.startswith("--unset="):
                index += 1
                continue
            if token in {"-C", "--chdir"}:
                if index + 1 < len(tokens):
                    paths.extend(_argument_paths([tokens[index + 1]], ""))
                index += 2
                continue
            if token.startswith("--chdir="):
                paths.extend(_argument_paths([token.split("=", 1)[1]], ""))
                index += 1
                continue
            if token.startswith("-C") and token != "-C":
                paths.extend(_argument_paths([token[2:]], ""))
                index += 1
                continue
            if token in {"-S", "--split-string"}:
                if index + 1 < len(tokens):
                    paths.extend(_command_paths(tokens[index + 1]))
                return paths
            if token.startswith("--split-string="):
                paths.extend(_command_paths(token.split("=", 1)[1]))
                return paths
            if token.startswith("-"):
                index += 1
                continue
            break
        if index >= len(tokens):
            return paths
        program = re.sub(r"[-_]?\d[\d.]*$", "", tokens[index].rsplit("/", 1)[-1].lower())
        index += 1

    paths.extend(_argument_paths(tokens[index:], program))
    return paths


def _command_paths(command):
    """Extract repository-shaped path arguments from a shell command string."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens = list(lexer)
    paths = []
    segment = []
    for token in tokens + [";"]:
        if token in {"&&", "||", "|", ";", "(", ")"}:
            paths.extend(_segment_paths(segment))
            segment = []
        else:
            segment.append(token)
    return tuple(dict.fromkeys(paths))


def lint_plan(plan, *, target_paths=(), policy=None):
    """Return stable sorted findings and their canonical SHA-256 digest.

    ``target_paths`` must be the repository-relative paths from the pinned
    target commit, not the caller's working tree.
    """
    target_paths = tuple(sorted({normalize(path) for path in target_paths if path}))
    policy = policy or {}
    analysis = analyze_plan(plan)
    findings = [
        _finding(
            "dag." + error["code"],
            error["message"],
            unit_ids=[error["unit_id"]] if error.get("unit_id") else (),
        )
        for error in analysis["errors"]
    ]
    try:
        units = [
            unit
            for unit in normalize_units(plan)
            if isinstance(unit, dict) and isinstance(unit.get("id"), str)
        ]
    except Exception:
        units = []
    by_id = {unit["id"]: unit for unit in units}
    ids = sorted(by_id)
    deps = {}
    for uid in ids:
        raw_deps = by_id[uid].get("depends_on", [])
        # ``analyze_plan`` already emits dag.invalid_depends_on.  Traverse a
        # sanitized view so malformed values cannot replace that useful,
        # deterministic finding with an exception from this secondary pass.
        if not isinstance(raw_deps, list):
            raw_deps = []
        deps[uid] = [
            dep for dep in raw_deps if isinstance(dep, str) and dep in by_id
        ]
    ancestors = _ancestors(ids, deps)
    scopes = {
        uid: (by_id[uid].get("scope") or {}).get("files") or []
        for uid in ids
    }
    # Match each target-tree path to each unit once. Pairwise overlap checks
    # can then use set intersection instead of rescanning a large repository
    # for every unit pair.
    scope_targets = {
        uid: frozenset(path for path in target_paths if _target_in_scope(path, scopes[uid]))
        for uid in ids
    }

    for position, left_id in enumerate(ids):
        for right_id in ids[position + 1 :]:
            if left_id in ancestors[right_id] or right_id in ancestors[left_id]:
                continue
            left_scope = scopes[left_id]
            right_scope = scopes[right_id]
            if (
                not scope_targets[left_id].isdisjoint(scope_targets[right_id])
                or _patterns_overlap(left_scope, right_scope, ())
            ):
                findings.append(
                    _finding(
                        "scope.unordered_overlap",
                        f"unordered units {left_id!r} and {right_id!r} have overlapping file scopes",
                        unit_ids=[left_id, right_id],
                    )
                )

    for uid in ids:
        unit = by_id[uid]
        scope = scopes[uid]
        for path in (unit.get("acceptance") or {}).get("paths", []):
            if not any(glob_match(target_path, path) for target_path in target_paths):
                findings.append(
                    _finding(
                        "acceptance.path_missing",
                        f"acceptance path {path!r} does not match the target-tree snapshot",
                        unit_ids=[uid],
                        paths=[path],
                    )
                )

        for command in (unit.get("acceptance") or {}).get("commands", []):
            try:
                command_paths = _command_paths(command)
            except ValueError as exc:
                findings.append(
                    _finding(
                        "acceptance.command_unparseable",
                        f"acceptance command cannot be parsed: {exc}",
                        unit_ids=[uid],
                    )
                )
                continue
            for path in command_paths:
                reason = unsafe_path_reason(path)
                if reason is not None:
                    findings.append(
                        _finding(
                            "acceptance.command_path_unsafe",
                            f"acceptance command path {path!r} is unsafe: {reason}",
                            unit_ids=[uid],
                            paths=[path],
                        )
                    )
                elif not scope_contains(path, scope) and not _path_present(path, target_paths):
                    findings.append(
                        _finding(
                            "acceptance.command_path_missing",
                            f"acceptance command path {path!r} is neither in scope nor in the target tree",
                            unit_ids=[uid],
                            paths=[path],
                        )
                    )

        available = set()
        for ancestor in ancestors[uid]:
            available.update(by_id[ancestor].get("provides") or [])
        for requirement in unit.get("requires") or []:
            target_path_satisfied = (
                isinstance(requirement, str)
                and unsafe_path_reason(requirement) is None
                and _path_present(requirement, target_paths)
            )
            if requirement not in available and not target_path_satisfied:
                findings.append(
                    _finding(
                        "requirements.unsatisfied",
                        f"requirement {requirement!r} is not provided by a transitive ancestor or present in the target tree",
                        unit_ids=[uid],
                    )
                )

        for coupling in policy.get("generated_file_couplings") or []:
            source, generated = coupling.get("source"), coupling.get("generated")
            if not isinstance(source, str) or not isinstance(generated, str):
                continue
            source_hit = _patterns_overlap(scope, [source], target_paths)
            generated_hit = _patterns_overlap(scope, [generated], target_paths)
            if source_hit != generated_hit:
                missing = generated if source_hit else source
                findings.append(
                    _finding(
                        "generated.coupling",
                        f"scope must include both coupled patterns {source!r} and {generated!r}",
                        unit_ids=[uid],
                        paths=[missing],
                    )
                )

    # Exemptions suppress only the named unit finding and are never implicit.
    exemptions = {}
    for uid in ids:
        for exemption in by_id[uid].get("lint_exemptions") or []:
            if not isinstance(exemption, dict) or not isinstance(exemption.get("code"), str):
                findings.append(
                    _finding(
                        "exemption.invalid",
                        "lint exemption must name a code and reason",
                        unit_ids=[uid],
                    )
                )
                continue
            reason = exemption.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                findings.append(
                    _finding(
                        "exemption.reason_required",
                        f"exemption for {exemption['code']!r} requires a non-empty reason",
                        unit_ids=[uid],
                    )
                )
                continue
            exemptions[(uid, exemption["code"])] = reason.strip()

    active, applied = [], []
    for finding in findings:
        keys = [(uid, finding["code"]) for uid in finding["unit_ids"]]
        if keys and all(key in exemptions for key in keys):
            applied.append(
                {
                    "code": finding["code"],
                    "unit_ids": finding["unit_ids"],
                    "reasons": [exemptions[key] for key in keys],
                }
            )
        else:
            active.append(finding)

    active.sort(key=lambda finding: (finding["code"], finding["unit_ids"], finding["paths"], finding["message"]))
    applied.sort(key=lambda exemption: (exemption["code"], exemption["unit_ids"], exemption["reasons"]))
    return {
        "ok": not active,
        "findings": active,
        "finding_digest": _digest(active),
        "exemptions": applied,
    }
