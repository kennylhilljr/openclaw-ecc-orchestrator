"""Quality gates: acceptance commands plus repository required checks.

Commands arrive as strings from config. They are parsed with shlex.split into
argv and executed through the process supervisor, never through a shell.
A unit passes only when every required gate ran and exited 0; warnings in the
output never count as a pass or a fail, only exit codes do.

Command policy is allowlist first (:func:`check_command_policy`):

* shells and wrappers (sh, bash, zsh, dash, fish, env, xargs, eval, exec,
  sudo, nohup, time, nice, command, busybox, xcrun and similar, matched
  case insensitively on the basename) are refused unless
  the repository policy allowlists them (``repo_checks["allow_wrappers"]``);
  an allowlisted wrapper's inner command (``env ... CMD``, ``sh -c "..."``) is
  checked again;
* an argument containing shell metacharacter sequences (``;``, ``&&``, ``||``,
  ``|``, backticks, ``$(``, ``${``, redirections, newlines) is refused, except
  the inline program of an interpreter (``python -c CODE``), which is code;
* git runs only allowlisted read only subcommands; ``push``, ``remote``,
  ``config``, ``update-ref``, ``credential`` and unknown subcommands (which
  may be aliases) are refused, as is any ``-c`` override outside a small safe
  set (so ``git -c alias.p=push p`` is refused);
* ``rm`` with any recursive flag spelling and ``find`` with ``-delete`` or
  ``-exec`` are refused; the protected globs remain as a final guard rail.

Gates get ``HOME`` and a per unit ``TMPDIR`` (created fresh, removed after).
"""

import collections
import fnmatch
import os
import re
import shlex
import shutil
import tempfile
import time

from ..process.redact import Redactor, redact_argv
from ..process.supervisor import Supervisor
from ..runs.envelope import SCHEMA_VERSION, check, envelope
from ..runs.store import iso

SHELL_OPERATORS = {"&&", "||", "|", ";", ";;", "&", ">", ">>", "<", "<<", "2>", "2>&1", "&>", "|&"}
METACHAR_SEQUENCES = ("&&", "||", ";", "|", "`", "$(", "${", ">", "<", "\n", "\r")

SHELLS = frozenset({"sh", "bash", "zsh", "dash", "fish", "ksh", "mksh", "pdksh", "ash", "csh", "tcsh", "rbash",
                    "yash", "elvish", "nu", "xonsh", "pwsh", "powershell"})
WRAPPERS = SHELLS | frozenset({
    "env", "xargs", "eval", "exec", "sudo", "doas", "su", "runuser", "pkexec", "nohup", "time", "nice",
    "ionice", "command", "builtin", "busybox", "toybox", "timeout", "stdbuf", "setsid", "chroot", "script",
    "watch", "parallel", "unbuffer", "flock", "chrt", "taskset", "caffeinate", "arch", "strace", "ltrace",
    "xcrun", "sandbox-exec", "dtruss", "launchctl", "osascript",
})
# Interpreters whose inline program argument is code, not shell text.
INLINE_CODE_FLAGS = {"python": {"-c"}, "pypy": {"-c"}, "node": {"-e", "--eval", "-p", "--print"},
                     "ruby": {"-e"}, "perl": {"-e", "-E"}}

GIT_ALLOWED_SUBCOMMANDS = frozenset({
    "status", "diff", "log", "show", "rev-parse", "ls-files", "ls-tree", "grep", "diff-tree", "diff-files",
    "diff-index", "rev-list", "cat-file", "merge-base", "describe", "blame", "annotate", "check-ignore",
    "check-attr", "fsck", "version", "shortlog", "count-objects", "verify-commit", "verify-tag", "show-ref",
    "for-each-ref", "name-rev", "whatchanged", "help", "range-diff", "cherry", "show-branch",
})
GIT_REFUSED_SUBCOMMANDS = frozenset({"push", "remote", "config", "update-ref", "credential", "symbolic-ref",
                                     "replace", "filter-branch", "send-email", "fetch", "pull", "clone",
                                     "submodule", "worktree", "clean", "reset", "branch", "tag", "gc",
                                     "prune", "reflog", "daemon", "http-backend", "archive"})
GIT_SAFE_CONFIG_PREFIXES = ("color.", "advice.", "core.quotepath", "log.", "status.", "diff.renames",
                            "diff.algorithm", "user.", "init.defaultbranch", "commit.gpgsign")
GIT_OPTS_WITH_VALUE = {"-C", "--git-dir", "--work-tree", "--namespace"}
GIT_FLAG_OPTS = {"--no-pager", "-P", "--paginate", "-p", "--bare", "--no-replace-objects", "--literal-pathspecs",
                 "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs", "--no-optional-locks",
                 "--version", "--help", "-h", "-v", "--no-advice"}
GIT_REFUSED_ARGS = ("--output", "-O", "--open-files-in-pager", "--ext-diff", "--exec", "--upload-pack",
                    "--receive-pack")

FIND_REFUSED = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}

DEFAULT_PROTECTED_GLOBS = (
    "git push*",
    "git * push*",
    "git reset --hard*",
    "git clean *",
    "git branch -D*",
    "git worktree remove*",
    "git update-ref*",
    "* --force*",
    "* -f push*",
    "rm -rf*",
    "rm -fr*",
    "rm -r *",
    "sudo *",
    "su *",
    "gh *",
    "npm publish*",
    "yarn publish*",
    "pnpm publish*",
    "twine upload*",
    "docker push*",
    "kubectl *",
    "terraform apply*",
    "terraform destroy*",
    "curl *",
    "wget *",
    "ssh *",
    "scp *",
    "rsync *",
    "nc *",
)


class GateCommandError(ValueError):
    pass


def parse_command(command):
    """Parse a config command string into argv; reject shell operator tokens."""
    if isinstance(command, (list, tuple)):
        argv = [str(a) for a in command]
    else:
        try:
            argv = shlex.split(str(command), posix=True)
        except ValueError as exc:
            raise GateCommandError(f"cannot parse command: {exc}")
    if not argv:
        raise GateCommandError("empty command")
    ops = [a for a in argv if a in SHELL_OPERATORS]
    if ops:
        raise GateCommandError(f"shell operators are not supported (no shell is used): {' '.join(ops)}")
    return argv


def _program(arg):
    """Lower case basename without a trailing version (``bash5.2`` -> ``bash``,
    ``python3.11`` -> ``python``). Lower case because the default macOS
    filesystem resolves ``BASH`` or ``RM`` to the real program."""
    base = os.path.basename(arg).lower()
    return re.sub(r"[-_]?\d[\d.]*$", "", base) or base


def _inline_code_indexes(argv):
    flags = INLINE_CODE_FLAGS.get(_program(argv[0]), set())
    return {i + 1 for i, a in enumerate(argv[:-1]) if i > 0 and a in flags}


def _metachar(argv):
    skip = _inline_code_indexes(argv)
    for i, arg in enumerate(argv):
        if i in skip:
            continue
        for seq in METACHAR_SEQUENCES:
            if seq in arg:
                return f"argument {i} contains shell metacharacters {seq!r}"
    return None


def _unwrap(program, argv):
    """Inner command of a wrapper, or None when there is none to check."""
    rest = argv[1:]
    if program in SHELLS:
        for i, arg in enumerate(rest):
            if arg == "-c" or (arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]):
                if i + 1 >= len(rest):
                    return []
                try:
                    return shlex.split(rest[i + 1])
                except ValueError:
                    return ["<unparsable>"]
        return None  # running a script file; its content is not inspected
    value_opts = {"env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
                  "nice": {"-n", "--adjustment"}, "timeout": {"-s", "--signal", "-k", "--kill-after"},
                  "xargs": {"-n", "-I", "-P", "-L", "-s", "-d", "-E", "-a", "--max-args", "--max-procs"},
                  "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U"},
                  "ionice": {"-c", "-n", "-p"}, "flock": {"-w", "-E", "-c"}, "stdbuf": set(),
                  "chrt": {"-p"}, "taskset": {"-p"},
                  "xcrun": {"--sdk", "--toolchain", "-sdk", "-toolchain"},
                  "sandbox-exec": {"-f", "-n", "-p", "-D"}}.get(program, set())
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--":
            i += 1
            break
        if arg in value_opts:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        if program == "env" and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", arg):
            i += 1
            continue
        if program in ("timeout", "flock", "chrt", "taskset", "chroot") and i == _first_positional(rest, value_opts):
            i += 1  # duration, lock file, priority, cpu mask or new root
            continue
        break
    return rest[i:]


def _first_positional(rest, value_opts):
    i = 0
    while i < len(rest):
        if rest[i] in value_opts:
            i += 2
        elif rest[i].startswith("-"):
            i += 1
        else:
            return i
    return -1


def _check_rm(argv):
    for arg in argv[1:]:
        if arg == "--":
            break
        if arg.startswith("--"):
            name = arg[2:].split("=", 1)[0]
            if name and "recursive".startswith(name):
                return f"rm with recursive flag {arg!r} is refused"
        elif arg.startswith("-") and len(arg) > 1 and ("r" in arg[1:] or "R" in arg[1:]):
            return f"rm with recursive flag {arg!r} is refused"
    return None


def _check_find(argv):
    for arg in argv[1:]:
        if arg in FIND_REFUSED:
            return f"find {arg} is refused"
    return None


def _check_git(argv):
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        arg = argv[i]
        if arg in ("-c", "--config-env") or arg.startswith("--config-env="):
            value = arg.split("=", 1)[1] if arg.startswith("--config-env=") else (argv[i + 1] if i + 1 < len(argv) else "")
            key = value.split("=", 1)[0].lower()
            if not any(key == p.rstrip(".") or key.startswith(p) for p in GIT_SAFE_CONFIG_PREFIXES):
                return f"git config override {key!r} is refused"
            i += 1 if arg.startswith("--config-env=") else 2
            continue
        if arg.startswith("-c") and len(arg) > 2:
            return "git config override is refused"
        if arg in GIT_OPTS_WITH_VALUE:
            i += 2
            continue
        if any(arg.startswith(o + "=") for o in GIT_OPTS_WITH_VALUE):
            i += 1
            continue
        if arg in GIT_FLAG_OPTS:
            i += 1
            continue
        return f"git option {arg!r} is not allowlisted"
    if i >= len(argv):
        return None
    sub = argv[i]
    if sub in GIT_REFUSED_SUBCOMMANDS or sub.startswith("credential"):
        return f"git {sub} is refused"
    if sub not in GIT_ALLOWED_SUBCOMMANDS:
        return f"git subcommand {sub!r} is not allowlisted (it may be an alias)"
    for arg in argv[i + 1:]:
        if arg == "--":
            break
        for bad in GIT_REFUSED_ARGS:
            if arg == bad or arg.startswith(bad + "=") or (bad == "-O" and arg.startswith("-O")):
                return f"git {sub} {bad} is refused"
    return None


def _forms(argv):
    joined = " ".join(argv)
    base = " ".join([os.path.basename(argv[0])] + argv[1:])
    lowered = " ".join([os.path.basename(argv[0]).lower()] + argv[1:])
    return {joined, base, lowered, shlex.join(argv)}


def _glob_match(argv, extra_globs):
    patterns = tuple(DEFAULT_PROTECTED_GLOBS) + tuple(extra_globs or ())
    for form in _forms(argv):
        for pattern in patterns:
            if fnmatch.fnmatchcase(form, pattern):
                return f"matches protected glob {pattern!r}"
    return None


def check_command_policy(argv, extra_globs=(), allow_wrappers=(), _depth=0):
    """Return a refusal reason for ``argv``, or None when it may run."""
    if not argv:
        return "empty command"
    if _depth > 4:
        return "wrapper nesting too deep"
    ops = [a for a in argv if a in SHELL_OPERATORS]
    if ops:
        return f"shell operator {ops[0]!r}"
    reason = _metachar(argv)
    if reason:
        return reason
    program = _program(argv[0])
    allowed = {_program(w) for w in (allow_wrappers or ())}
    if program in WRAPPERS:
        if program not in allowed:
            return f"{program} is a shell or command wrapper; not allowlisted by repository policy"
        inner = _unwrap(program, argv)
        if inner:
            reason = check_command_policy(inner, extra_globs, allow_wrappers, _depth + 1)
            if reason:
                return f"{program} wraps a refused command: {reason}"
        return _glob_match(argv, extra_globs)
    if program == "rm":
        reason = _check_rm(argv)
    elif program == "find":
        reason = _check_find(argv)
    elif program == "git":
        reason = _check_git(argv)
    if reason:
        return reason
    return _glob_match(argv, extra_globs)


def is_protected(argv, extra_globs=(), allow_wrappers=()):
    """Compatibility name for :func:`check_command_policy` (truthy reason when refused)."""
    return check_command_policy(argv, extra_globs, allow_wrappers)


def _gate_specs(unit, repo_checks):
    specs = []
    for index, command in enumerate(((unit.get("acceptance") or {}).get("commands") or [])):
        specs.append({"name": f"acceptance[{index}]", "source": "acceptance", "command": command})
    repo_checks = repo_checks or {}
    commands = repo_checks.get("commands") or {}
    for name in repo_checks.get("required") or []:
        specs.append({"name": name, "source": "required_check", "command": commands.get(name)})
    return specs


def _safe_name(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80]


def run_gates(unit, worktree, *, repo_checks=None, supervisor=None, protected_globs=(), timeout=600.0,
              tail_chars=4000, log_dir=None, env_allow=(), clock=time.time, redactor=None, allow_wrappers=(),
              tmp_root=None):
    """Run every gate for `unit` in `worktree` and return a verification record.

    Gates inherit ``HOME`` (plus ``env_allow``) and get ``TMPDIR`` set to a
    fresh per unit directory under ``tmp_root`` (default: the system temp
    dir) that is removed afterwards. ``allow_wrappers`` and
    ``repo_checks["allow_wrappers"]`` allowlist shells or wrappers.
    """
    supervisor = supervisor or Supervisor()
    redactor = redactor or supervisor.redactor or Redactor()
    wrappers = tuple(allow_wrappers or ()) + tuple((repo_checks or {}).get("allow_wrappers") or ())
    gate_env_allow = tuple(dict.fromkeys(("HOME",) + tuple(env_allow or ())))
    started = clock()
    gates = []
    missing, refused = [], []
    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp(prefix=f"gate-{_safe_name(str(unit.get('id')))}-", dir=tmp_root)
        _run_specs(unit, worktree, repo_checks, supervisor, redactor, protected_globs, wrappers, timeout,
                   tail_chars, log_dir, gate_env_allow, tmpdir, gates, missing, refused)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    passed = bool(gates) and all(g["status"] == "passed" for g in gates)
    warnings = [] if gates else ["no gates configured; a unit without gates cannot pass"]
    return {
        "schema_version": SCHEMA_VERSION,
        "unit_id": unit.get("id"),
        "worktree": worktree,
        "passed": passed,
        "gates": gates,
        "missing_required": missing,
        "refused": refused,
        "warnings": warnings,
        "started_at": iso(started),
        "ended_at": iso(clock()),
    }


def _run_specs(unit, worktree, repo_checks, supervisor, redactor, protected_globs, wrappers, timeout, tail_chars,
               log_dir, env_allow, tmpdir, gates, missing, refused):
    for spec in _gate_specs(unit, repo_checks):
        gate = {"name": spec["name"], "source": spec["source"], "required": True, "argv": None,
                "command": None, "status": None, "exit_code": None, "duration_s": 0.0, "timed_out": False,
                "output_tail": "", "log_path": None, "detail": ""}
        gates.append(gate)
        if spec["command"] is None:
            gate["status"] = "missing"
            gate["detail"] = "required check has no configured command"
            missing.append(spec["name"])
            continue
        gate["command"] = redactor.redact(str(spec["command"]))
        try:
            argv = parse_command(spec["command"])
        except GateCommandError as exc:
            gate["status"] = "invalid"
            gate["detail"] = str(exc)
            continue
        gate["argv"] = redact_argv(argv, redactor)
        reason = check_command_policy(argv, protected_globs, wrappers)
        if reason:
            gate["status"] = "refused"
            gate["detail"] = "refused by command policy: " + redactor.redact(reason)
            refused.append(spec["name"])
            continue
        tail = collections.deque()
        size = [0]

        def on_line(stream, line, tail=tail, size=size):
            tail.append(line)
            size[0] += len(line) + 1
            while size[0] > tail_chars * 2 and len(tail) > 1:
                size[0] -= len(tail.popleft()) + 1

        log_path = None
        if log_dir:
            log_path = os.path.join(log_dir, f"{_safe_name(str(unit.get('id')))}-{len(gates):02d}-{_safe_name(spec['name'])}.log")
        result = supervisor.run(argv, cwd=worktree, env_allow=env_allow, public_env={"TMPDIR": tmpdir},
                                on_line=on_line, log_path=log_path, timeout=timeout)
        text = "\n".join(tail)
        gate["output_tail"] = text[-tail_chars:] if tail_chars > 0 else ""
        gate["exit_code"] = result["exit_code"]
        gate["duration_s"] = result["duration_s"]
        gate["timed_out"] = result["timed_out"]
        gate["log_path"] = log_path
        if result["error"]:
            gate["status"] = "error"
            gate["detail"] = result["error"]
        elif result["timed_out"]:
            gate["status"] = "timeout"
        elif result["exit_code"] == 0:
            gate["status"] = "passed"
        else:
            gate["status"] = "failed"


def verification_envelope(verification):
    checks = [check(g["name"], g["status"] == "passed", g["status"]) for g in verification["gates"]]
    return envelope("gates.verify", ok=verification["passed"], changed=False, checks=checks,
                    warnings=verification["warnings"], data=verification)
