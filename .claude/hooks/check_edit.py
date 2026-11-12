#!/usr/bin/env python3
"""Claude Code edit hook for this repository.

Claude Code passes the tool call as JSON on stdin. This one script handles
both events registered in ``.claude/settings.json``:

PreToolUse (Write, Edit, MultiEdit)
  * exit 2 for any target whose file name starts with ``.env``
  * warn (exit 0) on direct edits to ``schemas/*.json``

PostToolUse (Write, Edit, MultiEdit)
  * ``src/openclaw_ecc_orchestrator/<path>.py`` runs only the matching
    ``tests/test_<module>.py`` and ``tests/test_<module>_*.py`` files
  * ``docs/cli.md`` runs ``tests.test_cli.DocsTest``
  * files under ``integrations/openclaw-plugin/`` run ``npm test`` there

PostToolUse checks report back through ``additionalContext`` and never
block. Standard library only.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

EDIT_TOOLS = frozenset({"Write", "Edit", "MultiEdit"})
PRE = "PreToolUse"
POST = "PostToolUse"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PACKAGE_PARTS = ("src", "openclaw_ecc_orchestrator")
PLUGIN_PARTS = ("integrations", "openclaw-plugin")
DOCS_CLI = ("docs", "cli.md")
TIMEOUT_SECONDS = 120
MAX_OUTPUT_CHARS = 4000
TEST_FILE = re.compile(r"^test_[A-Za-z0-9_]+\.py$")
ENV_MESSAGE = (
    "Blocked: {name} is a .env file. Do not write .env files; secrets go in "
    "environment variables (see AGENTS.md, Safety)."
)
SCHEMA_MESSAGE = (
    "Warning: {rel} is a generated copy. src/openclaw_ecc_orchestrator/schemas.py "
    "is the source of truth; change it there too, and tests/test_schemas_json.py "
    "must still pass."
)


def repo_root():
    return REPO_ROOT


def _emit_context(stdout, event, text):
    doc = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    stdout.write(json.dumps(doc) + "\n")


def _parse(raw, stderr):
    """Return (event, tool, file_path, cwd) or None for anything to ignore."""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        stderr.write("check_edit: stdin is not valid JSON; skipping checks\n")
        return None
    if not isinstance(payload, dict):
        stderr.write("check_edit: stdin JSON is not an object; skipping checks\n")
        return None
    event = payload.get("hook_event_name")
    tool = payload.get("tool_name")
    if event not in (PRE, POST) or tool not in EDIT_TOOLS:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        stderr.write("check_edit: tool_input is missing or not an object; skipping checks\n")
        return None
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path.strip() or "\x00" in file_path:
        stderr.write("check_edit: tool_input.file_path is missing or invalid; skipping checks\n")
        return None
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip() or "\x00" in cwd:
        cwd = None
    return event, tool, file_path, cwd


def _absolute(file_path, cwd):
    path = Path(os.path.expanduser(file_path))
    if not path.is_absolute():
        base = Path(cwd) if cwd and Path(cwd).is_absolute() else repo_root()
        path = base / path
    return path


def _resolve(path):
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return Path(os.path.abspath(path))


def _relative_to_repo(resolved):
    root = _resolve(repo_root())
    try:
        return resolved.relative_to(root).parts
    except ValueError:
        return None


def _is_env_name(name):
    return name.startswith(".env")


def pre_tool_use(file_path, cwd, stdout, stderr):
    absolute = _absolute(file_path, cwd)
    resolved = _resolve(absolute)
    names = [Path(file_path).name, absolute.name, Path(os.path.normpath(absolute)).name, resolved.name]
    for name in names:
        if _is_env_name(name):
            stderr.write(ENV_MESSAGE.format(name=name) + "\n")
            return 2
    parts = _relative_to_repo(resolved)
    if parts and len(parts) == 2 and parts[0] == "schemas" and parts[1].endswith(".json"):
        _emit_context(stdout, PRE, SCHEMA_MESSAGE.format(rel="/".join(parts)))
    return 0


def python_test_modules(parts):
    """Explicit unittest module names for a package source file, sorted."""
    if len(parts) < 3 or tuple(parts[:2]) != PACKAGE_PARTS or not parts[-1].endswith(".py"):
        return []
    rest = list(parts[2:])
    rest[-1] = rest[-1][: -len(".py")]
    if not all(rest):
        return []
    module = "_".join(rest)
    tests_dir = _resolve(repo_root()) / "tests"
    selected = set()
    try:
        entries = os.listdir(tests_dir)
    except OSError:
        return []
    for entry in entries:
        if not TEST_FILE.match(entry) or not (tests_dir / entry).is_file():
            continue
        stem = entry[: -len(".py")]
        if stem == "test_" + module or stem.startswith("test_" + module + "_"):
            selected.add("tests." + stem)
    return sorted(selected)


def _plan(parts):
    """Return (label, argv, cwd, python) or a str explaining why nothing runs, or None."""
    root = _resolve(repo_root())
    if tuple(parts) == DOCS_CLI:
        return ("docs", [sys.executable, "-m", "unittest", "tests.test_cli.DocsTest"], root, True)
    if len(parts) > 2 and tuple(parts[:2]) == PLUGIN_PARTS:
        return ("plugin", ["npm", "test"], root.joinpath(*PLUGIN_PARTS), False)
    if parts and parts[0] == "src":
        modules = python_test_modules(parts)
        if not modules:
            return "No test files match {}; ran nothing.".format("/".join(parts))
        return ("python", [sys.executable, "-m", "unittest"] + modules, root, True)
    return None


def _unsafe_tmp_reason(candidate):
    if not candidate:
        return "empty"
    path = Path(candidate)
    if not path.is_absolute():
        return "not absolute"
    resolved = _resolve(path)
    if ".openclaw" in path.parts or ".openclaw" in resolved.parts:
        return "inside an .openclaw directory"
    if ".git" in resolved.parts:
        return "inside a .git directory"
    root = _resolve(repo_root())
    if resolved == root or root in resolved.parents:
        return "inside the repository"
    if not resolved.is_dir():
        return "not a directory"
    if not os.access(resolved, os.W_OK | os.X_OK):
        return "not writable"
    return None


def safe_temp_base():
    """A validated system temporary directory outside the repo and .openclaw."""
    candidates = [os.environ.get("TMPDIR"), tempfile.gettempdir(), "/tmp", "/var/tmp"]
    for candidate in candidates:
        if candidate and _unsafe_tmp_reason(candidate) is None:
            return str(_resolve(Path(candidate)))
    return None


def _tail(text):
    text = text or ""
    if len(text) > MAX_OUTPUT_CHARS:
        return "...\n" + text[-MAX_OUTPUT_CHARS:]
    return text


def run_check(argv, cwd, python):
    """Run one check in a fresh TMPDIR. Returns a report string; never raises."""
    command = " ".join(argv)
    base = safe_temp_base()
    if base is None:
        return "Skipped `{}`: no safe temporary directory outside the repository and .openclaw.".format(command)
    try:
        tmp = tempfile.TemporaryDirectory(prefix="ecc-hook-", dir=base)
    except OSError as exc:
        return "Skipped `{}`: could not create a temporary directory ({}).".format(command, exc.__class__.__name__)
    try:
        env = dict(os.environ)
        env["TMPDIR"] = tmp.name
        if python:
            src = str(_resolve(repo_root()) / "src")
            existing = env.get("PYTHONPATH")
            env["PYTHONPATH"] = src + (os.pathsep + existing if existing else "")
        try:
            proc = subprocess.run(
                argv,
                cwd=str(cwd),
                env=env,
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return "Check `{}` timed out after {} seconds.".format(command, TIMEOUT_SECONDS)
        except OSError as exc:
            return "Could not launch `{}`: {}.".format(command, exc.strerror or exc.__class__.__name__)
        if proc.returncode == 0:
            return "Check `{}` passed.".format(command)
        output = _tail((proc.stdout or "") + (proc.stderr or ""))
        return "Check `{}` FAILED (exit {}):\n{}".format(command, proc.returncode, output)
    finally:
        try:
            tmp.cleanup()
        except OSError:
            pass


def post_tool_use(file_path, cwd, stdout, stderr):
    resolved = _resolve(_absolute(file_path, cwd))
    parts = _relative_to_repo(resolved)
    if not parts:
        return 0
    plan = _plan(parts)
    if plan is None:
        return 0
    if isinstance(plan, str):
        _emit_context(stdout, POST, plan)
        return 0
    _label, argv, run_cwd, python = plan
    _emit_context(stdout, POST, run_check(argv, run_cwd, python))
    return 0


def main(stdin=None, stdout=None, stderr=None):
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        raw = stdin.read()
    except (OSError, ValueError, UnicodeDecodeError):
        stderr.write("check_edit: could not read stdin; skipping checks\n")
        return 0
    parsed = _parse(raw, stderr)
    if parsed is None:
        return 0
    event, _tool, file_path, cwd = parsed
    try:
        if event == PRE:
            return pre_tool_use(file_path, cwd, stdout, stderr)
        return post_tool_use(file_path, cwd, stdout, stderr)
    except Exception as exc:  # a hook bug must never crash the agent's edit
        stderr.write("check_edit: internal error ({}); skipping checks\n".format(exc.__class__.__name__))
        return 0


if __name__ == "__main__":
    sys.exit(main())
