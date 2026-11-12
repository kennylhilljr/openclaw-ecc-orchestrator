#!/usr/bin/env python3
"""Write the JSON Schema files in schemas/ from schemas.json_schemas().

    python3 scripts/export_schemas.py            # regenerate schemas/*.json
    python3 scripts/export_schemas.py --check    # exit 1 if any file drifted

The Python definitions in src/openclaw_ecc_orchestrator/schemas.py are the
source; the committed JSON files are generated output. Files are written as
UTF-8 JSON with sorted keys, two space indentation and one trailing newline.

Exit codes: 0 success (or no drift), 1 drift found by --check, 2 usage or
I/O error. --check never writes.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from openclaw_ecc_orchestrator import schemas  # noqa: E402

DEFAULT_OUTPUT_DIR = REPO_ROOT / "schemas"


def render(schema: dict) -> bytes:
    return (json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def expected_files() -> dict[str, bytes]:
    return {name: render(schema) for name, schema in sorted(schemas.json_schemas().items())}


def check(output_dir: pathlib.Path) -> tuple[list[str], list[str]]:
    """Return (missing, differing) filenames under ``output_dir``."""
    missing: list[str] = []
    differing: list[str] = []
    for name, data in expected_files().items():
        path = output_dir / name
        if not path.is_file():
            missing.append(name)
        elif path.read_bytes() != data:
            differing.append(name)
    return missing, differing


def write(output_dir: pathlib.Path) -> list[str]:
    """Write every schema file; return the names whose bytes changed."""
    output_dir.mkdir(parents=True, exist_ok=True)
    changed = []
    for name, data in expected_files().items():
        path = output_dir / name
        if path.is_file() and path.read_bytes() == data:
            continue
        path.write_bytes(data)
        changed.append(name)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-dir", type=pathlib.Path, default=DEFAULT_OUTPUT_DIR,
                        help="directory for the schema files (default: the repository schemas/)")
    parser.add_argument("--check", action="store_true",
                        help="compare instead of writing; exit 1 if a file is missing or differs")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    output_dir = args.output_dir
    try:
        if args.check:
            if output_dir.exists() and not output_dir.is_dir():
                print("error: %s is not a directory" % output_dir, file=sys.stderr)
                return 2
            missing, differing = check(output_dir)
            for name in missing:
                print("missing: %s" % (output_dir / name))
            for name in differing:
                print("differs: %s" % (output_dir / name))
            if missing or differing:
                print("schemas are out of date; run python3 scripts/export_schemas.py",
                      file=sys.stderr)
                return 1
            print("schemas are up to date (%d files)" % len(expected_files()))
            return 0
        changed = write(output_dir)
    except OSError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    for name in changed:
        print("wrote: %s" % (output_dir / name))
    print("%d of %d schema files changed" % (len(changed), len(expected_files())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
