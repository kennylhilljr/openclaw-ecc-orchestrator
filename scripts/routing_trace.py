#!/usr/bin/env python3
"""Show how the router classifies each work unit, signal by signal.

    python3 scripts/routing_trace.py UNITS.json [--repo PATH] [--policy FILE]

UNITS.json holds one work unit, a list of units, or an object with a
``units`` list. Every unit goes through routing.classify.classify with the
repository policy (``--policy`` or, when absent,
``<repo root>/.orchestration/config.yaml`` if it exists). Classification uses
the declared scope only; no diff statistics are available before a unit runs.

Prints JSON: ``ok`` (every classification valid) and ``classifications``,
one complete Classification.to_dict() per unit plus its declared
``initial_tier``. Scoring stays in the classifier; this script only reports.

Exit codes: 0 all valid, 1 any invalid classification, 2 input, policy or
Git error.
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
from openclaw_ecc_orchestrator.routing.classify import classify  # noqa: E402
from openclaw_ecc_orchestrator.runs.dag import PlanError, normalize_units  # noqa: E402


class InputError(Exception):
    """An input, policy or Git problem; reported with exit code 2."""


def _strict_pairs(items):
    out = {}
    for key, value in items:
        if key in out:
            raise ValueError("duplicate key %r" % key)
        out[key] = value
    return out


def _reject_constant(name):
    raise ValueError("non-finite number %s" % name)


def strict_json(text: str):
    """Parse JSON like the CLI does: duplicate keys and NaN/Infinity are refused."""
    return json.loads(text, object_pairs_hook=_strict_pairs, parse_constant=_reject_constant)


def repository_root(repo: str) -> pathlib.Path:
    path = pathlib.Path(repo)
    if not path.is_dir():
        raise InputError("repository is not a directory: %s" % repo)
    try:
        result = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                                capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise InputError("git failed: %s" % exc) from None
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise InputError("not a Git repository: %s (%s)"
                         % (repo, detail[-1] if detail else "exit %d" % result.returncode))
    return pathlib.Path(result.stdout.decode("utf-8").strip())


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


def load_units(path: str) -> list:
    """Return the units in ``path``: one unit, a units list or a plan object."""
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError("cannot read %s: %s" % (path, exc)) from None
    try:
        data = strict_json(text)
    except (ValueError, RecursionError) as exc:
        raise InputError("%s is not valid JSON: %s" % (path, exc)) from None
    if isinstance(data, dict) and "units" not in data:
        # A work unit always has an id; anything else is an unsupported container.
        if "id" in data:
            return [data]
        raise InputError("expected a work unit, a list of units or an object with a units list")
    if isinstance(data, dict) and not isinstance(data.get("units"), list):
        raise InputError("units must be a list")
    if not isinstance(data, (list, dict)):
        raise InputError("expected a work unit, a list of units or an object with a units list")
    try:
        units = normalize_units(data)
    except PlanError as exc:
        raise InputError("invalid plan: %s" % exc) from None
    for index, unit in enumerate(units):
        if not isinstance(unit, dict):
            raise InputError("units[%d] must be a JSON object" % index)
    return units


def trace(units: list, policy: dict | None) -> dict:
    classifications = []
    for unit in units:
        entry = classify(unit, policy).to_dict()
        routing = unit.get("routing")
        entry["initial_tier"] = routing.get("initial_tier") if isinstance(routing, dict) else None
        classifications.append(entry)
    return {"ok": all(entry["ok"] for entry in classifications),
            "classifications": classifications}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("units", help="JSON work unit, units list or object with a units list")
    parser.add_argument("--repo", default=".",
                        help="repository whose policy applies (default: .)")
    parser.add_argument("--policy", default=None,
                        help="repository policy file (default: <repo>/.orchestration/config.yaml)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    try:
        units = load_units(args.units)
        root = repository_root(args.repo)
        policy = load_policy(root, args.policy)
    except InputError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    try:
        result = trace(units, policy)
    except Exception as exc:  # malformed fields the classifier cannot read
        print("error: units could not be classified (%s: %s)" % (type(exc).__name__, exc),
              file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
