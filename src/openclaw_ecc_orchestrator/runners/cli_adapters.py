"""Coding CLI adapters (Claude Code, Codex).

Adapters only build argument lists and interpret output; every process is
started through the injected ``ctx.run_command``. Prompts are always passed
after ``--`` so a prompt beginning with ``-`` is never parsed as an option.
Verify the version and authentication argv against the installed CLI during
certification; they are constructor overridable.
"""

from __future__ import annotations

import json
import re
import os
from typing import Any

from ..handoffs.redaction import looks_secret_name
from .base import CancelResult, CommandResult, ProbeContext, StepOutcome
from .registry import get_profile

PROBE_PROMPT = "Reply with the single word OK and nothing else."
EXERCISE_PROMPT = (
    "This git repository is a tiny Python project with one failing unit test. "
    "Run: python3 -m unittest -q   Read calc.py and fix the bug in calc.py only; do not "
    "modify test_calc.py. Run the tests again to confirm they pass, then commit your fix "
    "with: git commit -am \"probe: fix add\"   Do not touch anything else.")
# The cancellation check starts the ordinary probe prompt in streaming output
# mode and cancels on the first stream event: it never asks the model to run
# anything. An exit before any event is inconclusive and retried; after
# CANCEL_ATTEMPTS inconclusive runs the check fails with ``no_stream_event``.
CANCEL_ATTEMPTS = 3

# The disposable project for ``repo_exercise``: one failing unittest.
EXERCISE_FILES = {
    "calc.py": "def add(a, b):\n    return a - b\n",
    "test_calc.py": ("import unittest\n\nfrom calc import add\n\n\n"
                     "class AddTest(unittest.TestCase):\n"
                     "    def test_add(self):\n"
                     "        self.assertEqual(add(2, 3), 5)\n\n\n"
                     "if __name__ == \"__main__\":\n    unittest.main()\n"),
}
EXERCISE_TEST_ARGV = ["python3", "-B", "-m", "unittest", "-q"]  # -B: no stale bytecode

_GIT_IDENTITY = ["-c", "user.name=runner-probe", "-c", "user.email=runner-probe@example.invalid"]
_ERROR_LIMIT = 160
_DETAIL_LIMIT = 480


def _parse_version(text: str | None) -> tuple[int, ...] | None:
    """The first dotted version number in ``text`` (e.g. "2.1.281 (Claude Code)")."""
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not match:
        return None
    return tuple(int(part) for part in match.groups() if part is not None)


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _json_lines(stdout: str):
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


class CliAdapter:
    name = ""
    version_argv: list[str] = []
    auth_argv: list[str] = []
    # Environment names this CLI needs beyond the base probe allowlist
    # (PATH, HOME, USER, LANG, TERM, TMPDIR). HOME is listed explicitly: on
    # macOS both CLIs find their login (keychain or config dir) through it.
    credential_env: tuple[str, ...] = ("HOME",)

    def __init__(self, version_argv: list[str] | None = None,
                 auth_argv: list[str] | None = None,
                 credential_env: tuple[str, ...] | list[str] | None = None,
                 effort: str | None = None):
        self.profile = get_profile(self.name)
        if version_argv is not None:
            self.version_argv = list(version_argv)
        if auth_argv is not None:
            self.auth_argv = list(auth_argv)
        if credential_env is not None:
            self.credential_env = tuple(dict.fromkeys(("HOME",) + tuple(credential_env)))
        # Reasoning effort for the CLI (e.g. "low"); None keeps the CLI default.
        self.effort = effort

    # Not secrets, but needed at run time: Claude Code on macOS looks up its
    # keychain login by account name and reports "Not logged in" without USER
    # (seen 2026-10-19; certification passed because probes keep USER).
    # uv's cache and offline switches travel with the runner so a sandboxed agent
    # can reuse the owner's warm cache (live finding, rmcp-m0f).
    runtime_env: tuple[str, ...] = ("USER", "LOGNAME", "UV_CACHE_DIR", "UV_OFFLINE", "UV_PYTHON_INSTALL_DIR")

    def env_allow(self) -> tuple[str, ...]:
        """Names to allowlist when this CLI runs as a unit runner under the supervisor."""
        return tuple(dict.fromkeys(tuple(self.runtime_env) + tuple(self.credential_env)))

    # interface
    def build_invocation(self, prompt: str, model: str | None = None, cwd: str | None = None,
                         writable: bool = False) -> list[str]:
        raise NotImplementedError

    def build_cancel_invocation(self, model: str | None = None,
                                cwd: str | None = None) -> list[str]:
        """The short probe prompt in the CLI's streaming (JSON lines) output mode."""
        return self.build_invocation(PROBE_PROMPT, model, cwd=cwd)

    def parse_output(self, stdout: str) -> dict[str, Any]:
        """``{"text", "model", "cost_usd", "input_tokens", "output_tokens", "error"}``;
        ``error`` is set when the CLI reported a failed run, even with exit 0."""
        raise NotImplementedError

    def parse_auth(self, result: CommandResult) -> tuple[bool, str]:
        """(logged_in, safe_detail) from the authentication command output."""
        return True, _first_line(result.stdout)

    def configured(self, ctx: ProbeContext) -> tuple[bool, str]:
        return True, ""

    def secret_values(self, ctx: ProbeContext) -> list[str]:
        return [str(ctx.env.get(name)) for name in self.credential_env
                if looks_secret_name(name) and ctx.env.get(name)]

    # checks
    @staticmethod
    def _failed(result: CommandResult, what: str) -> StepOutcome | None:
        if result.timed_out:
            return StepOutcome("fail", "timeout", "%s did not finish in time" % what)
        if result.returncode != 0:
            return StepOutcome("fail", "%s failed (exit %d)" % (what, result.returncode),
                               _first_line(result.stderr) or _first_line(result.stdout))
        return None

    def _run_failed(self, result: CommandResult, what: str) -> StepOutcome | None:
        """Like ``_failed`` but prefers the CLI's own reported error."""
        parsed = self.parse_output(result.stdout) if not result.timed_out else {}
        if parsed.get("error"):
            return StepOutcome("fail", "%s error: %s" % (what, parsed["error"][:_ERROR_LIMIT]),
                               "exit %d" % result.returncode)
        return self._failed(result, what)

    # Oldest CLI version whose flags this adapter uses; None means no floor.
    min_version: tuple[int, ...] | None = None

    def installed(self, ctx: ProbeContext) -> StepOutcome:
        result = ctx.run_command(list(self.version_argv), ctx.step_timeout)
        failed = self._failed(result, "version check")
        if failed:
            return failed
        version = _first_line(result.stdout)
        found = _parse_version(version)
        if self.min_version and found and found < self.min_version:
            want = ".".join(str(n) for n in self.min_version)
            have = ".".join(str(n) for n in found)
            return StepOutcome(
                "fail", "%s %s is older than the minimum %s; check which %s is first on PATH"
                % (self.name, have, want, self.version_argv[0]), version,
                {"version": version or None})
        return StepOutcome("pass", "installed", version, {"version": version or None})

    def authentication(self, ctx: ProbeContext) -> StepOutcome:
        result = ctx.run_command(list(self.auth_argv), ctx.step_timeout)
        failed = self._failed(result, "authentication check")
        if failed:
            return failed
        logged_in, detail = self.parse_auth(result)
        if not logged_in:
            return StepOutcome("fail", "not logged in", detail)
        return StepOutcome("pass", "authenticated", detail)

    def live_inference(self, ctx: ProbeContext) -> StepOutcome:
        workdir = ctx.make_workdir()
        try:
            result = ctx.run_command(self.build_invocation(PROBE_PROMPT, ctx.model, cwd=workdir),
                                     ctx.step_timeout, workdir)
        finally:
            ctx.cleanup_workdir(workdir)
        failed = self._run_failed(result, "inference")
        if failed:
            return failed
        parsed = self.parse_output(result.stdout)
        if not parsed.get("text"):
            return StepOutcome("fail", "inference returned no text")
        metadata = {"model": parsed.get("model") or ctx.model,
                    "cost_usd": parsed.get("cost_usd"),
                    "input_tokens": parsed.get("input_tokens"),
                    "output_tokens": parsed.get("output_tokens")}
        return StepOutcome("pass", "inference ok", parsed["text"][:200], metadata)

    def repo_exercise(self, ctx: ProbeContext) -> StepOutcome:
        """Disposable repo with one failing unittest: the agent must read, fix,
        test and commit. The probe verifies the result itself: the tests pass
        on the working tree, the working tree matches HEAD, a new commit
        exists, and the test file is unchanged."""
        workdir = ctx.make_workdir()
        try:
            for relpath, content in EXERCISE_FILES.items():
                ctx.write_file(os.path.join(workdir, relpath), content)
            steps = [
                (["git", "init", "-q", workdir], "git init"),
                (["git", "-C", workdir, "config", "user.name", "runner-probe"], "git config"),
                (["git", "-C", workdir, "config", "user.email", "runner-probe@example.invalid"],
                 "git config"),
                (["git", "-C", workdir, "add", "-A"], "git add"),
                (["git", "-C", workdir, *_GIT_IDENTITY, "commit", "-q", "-m", "probe: base"],
                 "base commit"),
            ]
            for argv, what in steps:
                failed = self._failed(ctx.run_command(argv, ctx.step_timeout, workdir), what)
                if failed:
                    return failed
            before = ctx.run_command(list(EXERCISE_TEST_ARGV), ctx.step_timeout, workdir)
            if before.returncode == 0 and not before.timed_out:
                return StepOutcome("fail", "exercise setup error: the test already passes")
            agent = ctx.run_command(self.build_invocation(EXERCISE_PROMPT, ctx.model, cwd=workdir,
                                                          writable=True),
                                    ctx.step_timeout, workdir)
            failed = self._run_failed(agent, "agent edit")
            if failed:
                return failed
            after = ctx.run_command(list(EXERCISE_TEST_ARGV), ctx.step_timeout, workdir)
            if after.timed_out or after.returncode != 0:
                return StepOutcome("fail", "tests still fail after the agent run",
                                   _first_line(after.stderr))
            count = ctx.run_command(["git", "-C", workdir, "rev-list", "--count", "HEAD"],
                                    ctx.step_timeout, workdir)
            failed = self._failed(count, "verify commit")
            if failed:
                return failed
            try:
                commits = int(count.stdout.strip())
            except ValueError:
                commits = 0
            if commits < 2:
                return StepOutcome("fail", "agent did not commit its fix")
            dirty = ctx.run_command(["git", "-C", workdir, "status", "--porcelain",
                                     "--untracked-files=no"], ctx.step_timeout, workdir)
            failed = self._failed(dirty, "verify clean tree")
            if failed:
                return failed
            if dirty.stdout.strip():
                return StepOutcome("fail", "fix is not fully committed")
            tests_same = ctx.run_command(["git", "-C", workdir, "diff", "--quiet", "HEAD~%d"
                                          % (commits - 1), "HEAD", "--", "test_calc.py"],
                                         ctx.step_timeout, workdir)
            if tests_same.timed_out or tests_same.returncode != 0:
                return StepOutcome("fail", "agent modified the test file")
            return StepOutcome("pass", "read/edit/test/commit exercise passed",
                               "%d new commit(s); tests pass on the committed tree"
                               % (commits - 1))
        finally:
            ctx.cleanup_workdir(workdir)

    def cancellation(self, ctx: ProbeContext) -> StepOutcome:
        """Event triggered cancellation: start the probe prompt in streaming
        mode, cancel the process group as soon as the first stream event
        arrives on stdout, and prove the whole group is gone. Independent of
        the model agreeing to do anything: the event precedes the answer."""
        if ctx.cancel_command is None:
            return StepOutcome("fail", "no event aware cancel runner",
                               "a finished command cannot be cancelled; inject ctx.cancel_command")
        inconclusive: list[str] = []
        for attempt in range(1, CANCEL_ATTEMPTS + 1):
            workdir = ctx.make_workdir()
            try:
                argv = self.build_cancel_invocation(ctx.model, cwd=workdir)
                res: CancelResult = ctx.cancel_command(argv, ctx.step_timeout, workdir)
            finally:
                ctx.cleanup_workdir(workdir)
            meta = {"attempts": attempt, "members_before": res.members_before,
                    "elapsed_seconds": res.elapsed_seconds, "first_event": res.first_event or None,
                    "event_seconds": res.event_seconds}
            if not res.started:
                return StepOutcome("fail", "could not start the cancellation task", "", meta)
            if res.event_seen and not res.exited_early:
                if not res.group_gone:
                    return StepOutcome("fail", "process group survived cancellation",
                                       "%d process(es) in the group at cancel" % res.members_before, meta)
                return StepOutcome("pass", "cancelled on the first stream event; whole process group gone",
                                   "first event %s after %ss; %d process(es) in the group at cancel, "
                                   "0 after; attempt %d" % (res.first_event, res.event_seconds,
                                                            res.members_before, attempt), meta)
            if not res.exited_early:
                return StepOutcome("fail", "no_stream_event",
                                   "no stream event within %ss; group killed (%s)"
                                   % (int(ctx.step_timeout), "gone" if res.group_gone else "survivors"),
                                   meta)
            parsed = self.parse_output(res.stdout)
            why = parsed.get("error") or _first_line(res.stderr) or ("exit %s" % res.returncode)
            inconclusive.append("attempt %d: exited before %s (%s)" % (
                attempt, "the cancel" if res.event_seen else "any stream event", why[:_ERROR_LIMIT]))
        return StepOutcome("fail", "no_stream_event", "; ".join(inconclusive)[:_DETAIL_LIMIT],
                           {"attempts": CANCEL_ATTEMPTS})


class ClaudeAdapter(CliAdapter):
    """Claude Code print mode (flags confirmed against Claude Code 2.1.281).

    ``--permission-prompts none`` makes anything that would prompt be denied
    instead of waiting for an absent host, ``--no-session-persistence`` keeps
    probe sessions off disk. ``claude auth status`` prints JSON by default;
    ``--json`` pins that.
    """
    name = "claude"
    # 2.1.112 lacks --permission-prompts (seen 2026-10-18); 2.1.281 is verified.
    min_version = (2, 1, 281)
    version_argv = ["claude", "--version"]
    auth_argv = ["claude", "auth", "status", "--json"]
    credential_env = ("HOME", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR")
    # uv and the repo CI script: units must lock, sync and run their own gates
    # (uv run executes arbitrary code, as python3 already can, so this adds no
    # new capability).
    edit_tools = ("Read,Edit,Write,Bash(git *),Bash(python3 *),Bash(uv *),"
                  "Bash(tools/ci.sh),Bash(tools/ci.sh *),Bash(./tools/ci.sh),Bash(./tools/ci.sh *)")

    def _base(self, model, output_format="json"):
        argv = ["claude", "-p", "--output-format", output_format, "--no-session-persistence",
                "--permission-prompts", "none"]
        if output_format == "stream-json":
            # Claude Code 2.1.281: print mode refuses stream-json without --verbose.
            argv.append("--verbose")
        if model:
            argv += ["--model", model]
        if self.effort:
            argv += ["--effort", self.effort]
        return argv

    def build_invocation(self, prompt, model=None, cwd=None, writable=False):
        argv = self._base(model)
        if writable:
            # Project and local settings only: the owner's user-level plugins and hooks
            # (for example a guardrail that refuses edits to any file named secrets.py)
            # must not decide what an ECC unit may write (live finding, rmcp-m0f M0-05).
            argv += ["--setting-sources", "project,local",
                     "--permission-mode", "acceptEdits", "--allowedTools", self.edit_tools]
        return argv + ["--", prompt]

    def build_cancel_invocation(self, model=None, cwd=None):
        return self._base(model, "stream-json") + ["--", PROBE_PROMPT]

    def parse_auth(self, result):
        try:
            data = json.loads(result.stdout)
        except (ValueError, TypeError):
            data = None
        if not isinstance(data, dict):
            return False, "unrecognized auth status output"
        detail = "login via %s, provider %s" % (data.get("authMethod"), data.get("apiProvider"))
        return data.get("loggedIn") is True, detail

    def parse_output(self, stdout):
        data = None
        for line in reversed((stdout or "").strip().splitlines() or [""]):
            try:
                data = json.loads(line)
                break
            except ValueError:
                continue
        if data is None:
            try:
                data = json.loads(stdout)
            except (ValueError, TypeError):
                return {}
        if not isinstance(data, dict):
            return {}
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        models = data.get("modelUsage") if isinstance(data.get("modelUsage"), dict) else {}
        text = data.get("result") if isinstance(data.get("result"), str) else ""
        error = None
        if data.get("is_error") is True or (isinstance(data.get("subtype"), str)
                                            and data["subtype"].startswith("error")):
            status = data.get("api_error_status")
            error = (text or str(data.get("subtype") or "error")).strip()
            if status:
                error = "%s (HTTP %s)" % (error, status)
            text = ""
        return {"text": text,
                "model": next(iter(models), None),
                "cost_usd": data.get("total_cost_usd"),
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "error": error}


def uv_cache_dir():
    """uv's cache folder when it exists (UV_CACHE_DIR, else uv's macOS and Linux
    default). Granted to Codex's sandbox so `uv sync` and `uv run` work offline."""
    path = os.environ.get("UV_CACHE_DIR") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "uv")
    return path if os.path.isdir(path) else None


def git_write_dir(cwd):
    """The directory git writes when committing from ``cwd``.

    A normal checkout writes ``<cwd>/.git``. In a linked worktree ``.git`` is a
    file ("gitdir: <common>/worktrees/<name>"); a commit there writes that
    worktree's index under the common directory plus the shared objects and
    refs, so the common git directory is what must be writable."""
    dotgit = os.path.join(cwd, ".git")
    if not os.path.isfile(dotgit):
        return dotgit
    try:
        with open(dotgit, encoding="utf-8") as fh:
            first = fh.readline().strip()
    except OSError:
        return dotgit
    if not first.startswith("gitdir:"):
        return dotgit
    gitdir = first[len("gitdir:"):].strip()
    if not os.path.isabs(gitdir):
        gitdir = os.path.normpath(os.path.join(cwd, gitdir))
    try:
        with open(os.path.join(gitdir, "commondir"), encoding="utf-8") as fh:
            common = fh.readline().strip()
    except OSError:
        return gitdir
    return os.path.normpath(common if os.path.isabs(common) else os.path.join(gitdir, common))


class CodexAdapter(CliAdapter):
    """Codex exec mode (flags confirmed against codex-cli 0.158.0-alpha.7).

    ``--ephemeral`` keeps probe sessions off disk; writable runs add the
    repository's ``.git`` with ``--add-dir`` so the agent can commit; reasoning effort goes
    through ``-c model_reasoning_effort=...``. ``codex debug models`` renders
    the live model catalog used to pick the model (see ``certify``).
    """
    name = "codex"
    version_argv = ["codex", "--version"]
    auth_argv = ["codex", "login", "status"]
    models_argv = ["codex", "debug", "models"]
    credential_env = ("HOME", "CODEX_HOME")

    def build_invocation(self, prompt, model=None, cwd=None, writable=False):
        argv = ["codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check", "--sandbox",
                "workspace-write" if writable else "read-only"]
        if cwd:
            argv += ["-C", cwd]
            if writable:
                # workspace-write keeps <root>/.git read only, so the agent
                # cannot commit (live finding: ".git/index.lock" denied).
                # Grant exactly this repository's git directory; for a linked
                # worktree that is the common .git, not the ".git" pointer
                # file (live finding, preflight 2026-10-19).
                argv += ["--add-dir", git_write_dir(cwd)]
                cache = uv_cache_dir()
                if cache:
                    argv += ["--add-dir", cache]
        if model:
            argv += ["-m", model]
        if self.effort:
            argv += ["-c", 'model_reasoning_effort="%s"' % self.effort]
        return argv + ["--", prompt]

    def parse_auth(self, result):
        text = "%s\n%s" % (result.stdout or "", result.stderr or "")
        for line in text.splitlines():
            line = line.strip()
            if line.lower().startswith("logged in"):
                # "Logged in using ChatGPT" / "Logged in using an API key": keep
                # only the method words, never anything that could identify.
                return True, " ".join(line.split()[:4])
        return False, "not logged in"

    def parse_output(self, stdout):
        text, model, usage, error = "", None, {}, None
        for event in _json_lines(stdout):
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if item.get("type") in ("agent_message", "assistant_message") and isinstance(
                    item.get("text"), str):
                text = item["text"]
            if isinstance(event.get("usage"), dict):
                usage = event["usage"]
            if isinstance(event.get("model"), str):
                model = event["model"]
            if kind == "turn.failed":
                err = event.get("error")
                error = (err.get("message") if isinstance(err, dict) else None) or error or \
                    "turn failed"
            elif kind == "error" and isinstance(event.get("message"), str):
                error = event["message"]
        if error:
            text = ""
        return {"text": text, "model": model, "cost_usd": None,
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "error": error}
