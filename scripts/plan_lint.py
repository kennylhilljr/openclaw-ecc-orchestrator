#!/usr/bin/env python3
"""Lint a work-unit plan against a repository's committed HEAD.

    python3 scripts/plan_lint.py PLAN.json [--repo PATH] [--policy FILE]

PLAN.json holds a list of work units or an object with a ``units`` list.
Target paths come from the committed tree at the repository's HEAD
(``git ls-tree``), never from the working tree, so untracked and
staged-only files do not count. The policy is ``--policy`` or, when that is
absent, ``<repo root>/.orchestration/config.yaml`` if it exists.

Prints the complete runs.plan_lint.lint_plan result as JSON (ok, findings,
finding_digest, exemptions). Read-only: never runs plan commands and never
writes runtime state.

Exit codes: 0 no findings, 1 findings, 2 input, policy or Git error.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from openclaw_ecc_orchestrator import schemas  # noqa: E402
from openclaw_ecc_orchestrator.runs.plan_lint import lint_plan  # noqa: E402


class InputError(Exception):
    """A plan, policy or Git problem; reported with exit code 2."""


def _git(repo: pathlib.Path, *args: str) -> bytes:
    try:
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                                timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise InputError("git failed: %s" % exc) from None
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise InputError("git %s failed: %s" % (args[0], detail[-1] if detail else
                                                "exit %d" % result.returncode))
    return result.stdout


def repository_root(repo: str) -> pathlib.Path:
    path = pathlib.Path(repo)
    if not path.is_dir():
        raise InputError("repository is not a directory: %s" % repo)
    root = _git(path, "rev-parse", "--show-toplevel").decode("utf-8").strip()
    return pathlib.Path(root)


def committed_paths(root: pathlib.Path) -> tuple[str, list[str]]:
    """Pin HEAD and return (commit, repository-relative committed paths)."""
    commit = _git(root, "rev-parse", "--verify", "HEAD^{commit}").decode("utf-8").strip()
    listing = _git(root, "ls-tree", "-r", "-z", "--name-only", commit)
    paths = [entry.decode("utf-8", "surrogateescape") for entry in listing.split(b"\0") if entry]
    return commit, paths


def load_policy(root: pathlib.Path, policy: str | None) -> dict | None:
    if policy is None:
        default = root / ".orchestration" / "config.yaml"
        if not default.is_file():
            return None
        path = default
    else:
        path = pathlib.Path(policy)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError("cannot read policy %s: %s" % (path, exc)) from None
    try:
        return schemas.load_repository_policy(text)
    except schemas.DocumentError as exc:
        detail = "; ".join([str(exc)] + list(exc.errors)[:5])
        raise InputError("invalid policy %s: %s" % (path, detail)) from None


def load_plan(path: str):
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError("cannot read plan %s: %s" % (path, exc)) from None
    try:
        plan = json.loads(text)
    except ValueError as exc:
        raise InputError("plan %s is not valid JSON: %s" % (path, exc)) from None
    if isinstance(plan, list):
        return plan
    if isinstance(plan, dict) and isinstance(plan.get("units"), list):
        return plan
    raise InputError("plan must be a list of units or an object with a units list")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("plan", help="JSON plan: a units list or an object with a units list")
    parser.add_argument("--repo", default=".",
                        help="repository whose committed HEAD supplies target paths (default: .)")
    parser.add_argument("--policy", default=None,
                        help="repository policy file (default: <repo>/.orchestration/config.yaml)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    try:
        plan = load_plan(args.plan)
        root = repository_root(args.repo)
        policy = load_policy(root, args.policy)
        _, paths = committed_paths(root)
    except InputError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    try:
        result = lint_plan(plan, target_paths=paths, policy=policy)
    except Exception as exc:  # malformed nested fields the linter cannot read
        print("error: plan could not be linted, check that every unit is a valid work unit "
              "(%s: %s)" % (type(exc).__name__, exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
