"""Minimal git subprocess wrapper (argument lists only, never a shell).

Every orchestrator git command runs with:

* a minimal environment: ``PATH``, ``HOME``, ``LANG``, ``TMPDIR``, ``USER``,
  ``LOGNAME`` from the parent (plus names explicitly allowlisted through
  ``env_allow`` or :func:`set_git_env_allow`), ``LC_ALL=C``,
  ``GIT_CONFIG_NOSYSTEM=1``, ``GIT_TERMINAL_PROMPT=0`` and
  ``GIT_OPTIONAL_LOCKS=0``. Orchestrator credentials in the parent
  environment never reach git or anything git spawns.
* command line safety overrides (``SAFETY_CONFIG``) that neutralise the
  repository config a worker could have written: no fsmonitor, no hooks, no
  ssh command, no credential helper, no ``ext::`` transport.
"""

import os
import subprocess

GIT_ENV_ALLOW = ("PATH", "HOME", "LANG", "TMPDIR", "USER", "LOGNAME")

SAFETY_CONFIG = (
    ("core.fsmonitor", "false"),
    ("core.hooksPath", os.devnull),
    ("core.sshCommand", ""),
    ("credential.helper", ""),
    ("core.askPass", ""),
    ("protocol.ext.allow", "never"),
)

_extra_allow = ()


def set_git_env_allow(names):
    """Process wide extra environment names passed to every orchestrator git command."""
    global _extra_allow
    _extra_allow = tuple(str(n) for n in (names or ()))


class GitError(RuntimeError):
    def __init__(self, argv, returncode, stdout, stderr):
        self.argv = list(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(f"{' '.join(argv)} failed ({returncode}): {stderr.strip()}")


def git_env(env_allow=()):
    env = {}
    for name in tuple(GIT_ENV_ALLOW) + _extra_allow + tuple(env_allow or ()):
        if name in os.environ and not name.startswith("GIT_"):
            env[name] = os.environ[name]
    env["LC_ALL"] = "C"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def safety_args():
    args = []
    for key, value in SAFETY_CONFIG:
        args += ["-c", f"{key}={value}"]
    return args


def run_git(args, cwd, check=True, timeout=120, config=None, env_allow=()):
    argv = ["git"] + safety_args()
    for key, value in (config or {}).items():
        argv += ["-c", f"{key}={value}"]
    argv += list(args)
    proc = subprocess.run(argv, cwd=cwd, env=git_env(env_allow), capture_output=True, text=True,
                          timeout=timeout, stdin=subprocess.DEVNULL)
    if check and proc.returncode != 0:
        raise GitError(argv, proc.returncode, proc.stdout, proc.stderr)
    return proc


def git_out(args, cwd, **kw):
    return run_git(args, cwd, **kw).stdout.strip()


def rev_exists(repo, ref):
    return run_git(["rev-parse", "--verify", "--quiet", ref + "^{commit}"], repo, check=False).returncode == 0


def toplevel(path):
    return os.path.realpath(git_out(["rev-parse", "--show-toplevel"], path))


def unique_commits(repo, tips, exclude_refs, include_remotes=True):
    """Commits reachable from `tips` but not from `exclude_refs` or any remote."""
    args = ["rev-list"] + list(tips) + ["--not"] + list(exclude_refs)
    if include_remotes:
        args.append("--remotes")
    out = git_out(args, repo)
    return [line for line in out.splitlines() if line]
