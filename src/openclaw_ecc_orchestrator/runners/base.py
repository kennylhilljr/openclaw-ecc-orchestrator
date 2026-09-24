"""Shared probe types: command results, probe context, step outcomes, timeouts."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, NamedTuple

from .discovery import HttpGetter, HttpResponse, default_http_get, tls_context


class CommandResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


CommandRunner = Callable[..., CommandResult]  # (argv: list[str], timeout: float, cwd=None)
HttpPoster = Callable[[str, Mapping[str, str], dict, float], HttpResponse]

_MAX_CAPTURE = 1024 * 1024

# The only parent variables a probed CLI inherits, plus the adapter's declared
# credential names (see ``CliAdapter.credential_env``).
PROBE_ENV_ALLOW = ("PATH", "HOME", "USER", "LANG", "TERM", "TMPDIR")
KILL_GRACE_SECONDS = 2.0


def minimal_env(source: Mapping[str, str] | None = None, extra_names: Iterable[str] = ()) -> dict:
    """``source`` (default ``os.environ``) reduced to the allowlist plus ``extra_names``."""
    source = os.environ if source is None else source
    names = dict.fromkeys(tuple(PROBE_ENV_ALLOW) + tuple(extra_names or ()))
    return {name: str(source[name]) for name in names if source.get(name) is not None}


def _decode(data) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data[:_MAX_CAPTURE]
    return data[:_MAX_CAPTURE].decode("utf-8", "replace")


def _exited_nowait(proc) -> bool:
    """True once the child exited; keeps it unreaped when ``os.waitid`` allows,
    so its process group id cannot be recycled while we still signal it."""
    try:
        return os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        return True
    except (AttributeError, NotImplementedError, OSError):
        return proc.poll() is not None


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _terminate_group(proc, grace: float) -> None:
    """SIGTERM the whole group, SIGKILL it after ``grace`` seconds, then reap."""
    _signal_group(proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and not _exited_nowait(proc):
        time.sleep(0.05)
    # Always follow with SIGKILL: grandchildren may ignore SIGTERM even when
    # the direct child exited.
    _signal_group(proc.pid, signal.SIGKILL)
    try:
        proc.wait(timeout=max(grace, 1.0))
    except subprocess.TimeoutExpired:
        pass


def default_command_runner(argv: list[str], timeout: float, cwd: str | None = None,
                           env: Mapping[str, str] | None = None) -> CommandResult:
    """Run ``argv`` without a shell, with a hard timeout and no stdin.

    The child gets a minimal environment (``env``, or ``minimal_env()`` of the
    parent) and its own process group; on timeout the whole group gets
    SIGTERM, then SIGKILL after ``KILL_GRACE_SECONDS``.
    """
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise TypeError("argv must be a non-empty list of strings (no shell strings)")
    child_env = minimal_env() if env is None else {str(k): str(v) for k, v in env.items()}
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=child_env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                                start_new_session=True, close_fds=True)
    except FileNotFoundError:
        return CommandResult(127, "", "executable not found")
    except PermissionError:
        return CommandResult(126, "", "permission denied")
    except NotADirectoryError:
        return CommandResult(127, "", "invalid working directory")
    try:
        out, err = proc.communicate(timeout=timeout)
        return CommandResult(proc.returncode, _decode(out), _decode(err))
    except subprocess.TimeoutExpired as exc:
        partial_out, partial_err = exc.stdout, exc.stderr
    grace = float(KILL_GRACE_SECONDS)
    _terminate_group(proc, grace)
    try:
        out, err = proc.communicate(timeout=grace)
    except subprocess.TimeoutExpired as exc:
        # A process outside the group still holds the pipes; stop reading.
        out, err = exc.stdout or partial_out, exc.stderr or partial_err
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
    except ValueError:
        out, err = partial_out, partial_err
    return CommandResult(-9, _decode(out), _decode(err), True)


class CancelResult(NamedTuple):
    """Outcome of starting a long task and cancelling it (see ``default_cancel_runner``)."""
    started: bool                 # the process was spawned
    exited_early: bool            # it exited on its own before the cancel point
    returncode: int | None
    group_gone: bool              # after the kill, no process of its group remains
    members_before: int           # processes in the group just before the kill
    elapsed_seconds: float
    stdout: str = ""
    stderr: str = ""


CancelRunner = Callable[..., CancelResult]  # (argv, cancel_after, cwd=None)


def _group_members(pgid: int) -> int:
    """Number of live processes in process group ``pgid`` (0 when none)."""
    try:
        out = subprocess.run(["pgrep", "-g", str(pgid)], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=5, env=minimal_env())
        if out.returncode in (0, 1):
            return len([line for line in out.stdout.split() if line.strip().isdigit()])
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        os.killpg(pgid, 0)
        return 1
    except ProcessLookupError:
        return 0
    except PermissionError:
        return 1


def _group_gone(pgid: int, wait: float) -> bool:
    deadline = time.monotonic() + wait
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        if _group_members(pgid) == 0:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def default_cancel_runner(argv: list[str], cancel_after: float, cwd: str | None = None,
                          env: Mapping[str, str] | None = None,
                          verify_wait: float = 5.0) -> CancelResult:
    """Start ``argv`` in its own process group, cancel it after ``cancel_after``
    seconds (SIGTERM to the group, SIGKILL after the grace period) and prove
    that no process of the group survives.

    A process that exits on its own before the cancel point is reported with
    ``exited_early``; that does not exercise cancellation.
    """
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise TypeError("argv must be a non-empty list of strings (no shell strings)")
    child_env = minimal_env() if env is None else {str(k): str(v) for k, v in env.items()}
    started_at = time.monotonic()
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=child_env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                                start_new_session=True, close_fds=True)
    except (FileNotFoundError, PermissionError, NotADirectoryError):
        return CancelResult(False, False, None, True, 0, 0.0, "", "could not start process")
    pgid = proc.pid
    try:
        out, err = proc.communicate(timeout=cancel_after)
        return CancelResult(True, True, proc.returncode, _group_gone(pgid, verify_wait), 0,
                            round(time.monotonic() - started_at, 3), _decode(out), _decode(err))
    except subprocess.TimeoutExpired as exc:
        partial_out, partial_err = exc.stdout, exc.stderr
    members = _group_members(pgid)
    _terminate_group(proc, float(KILL_GRACE_SECONDS))
    try:
        out, err = proc.communicate(timeout=float(KILL_GRACE_SECONDS))
    except (subprocess.TimeoutExpired, ValueError):
        out, err = partial_out, partial_err
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except (OSError, AttributeError):
                pass
    gone = _group_gone(pgid, verify_wait)
    return CancelResult(True, False, proc.returncode, gone, members,
                        round(time.monotonic() - started_at, 3), _decode(out), _decode(err))


def default_write_file(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def default_http_post(url: str, headers: Mapping[str, str], body: dict,
                      timeout: float) -> HttpResponse:
    data = json.dumps(body).encode("utf-8")
    hdrs = {"Content-Type": "application/json", **dict(headers)}
    request = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout,  # noqa: S310
                                    context=tls_context()) as resp:
            return HttpResponse(resp.status, resp.read(_MAX_CAPTURE).decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, "")


@dataclass
class ProbeContext:
    run_command: CommandRunner = default_command_runner
    http_get: HttpGetter = default_http_get
    http_post: HttpPoster = default_http_post
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    clock: Callable[[], float] = time.time
    step_timeout: float = 120.0
    cancel_timeout: float = 5.0
    make_workdir: Callable[[], str] = lambda: tempfile.mkdtemp(prefix="runner-probe-")
    cleanup_workdir: Callable[[str], None] = lambda path: shutil.rmtree(path, ignore_errors=True)
    model: str | None = None            # model id (catalog) or alias (cli) to exercise
    policy: Mapping[str, Any] = field(default_factory=dict)
    certification_ttl_seconds: float = 7 * 24 * 3600
    # Cancellation runner proving process-group cleanup. None: the probe uses
    # ``run_command`` with ``cancel_timeout`` (injected runners), or
    # ``default_cancel_runner`` with the minimal env (default command runner).
    cancel_command: CancelRunner | None = None
    write_file: Callable[[str, str], None] = default_write_file
    # Progress hook ``on_step(check_name, phase)``; phase is "start" or the
    # finished status. Used to see which step hangs.
    on_step: Callable[[str, str], None] | None = None
    # When a list, the probe appends every piece of runner output, redacted.
    transcript: list | None = None


@dataclass
class StepOutcome:
    status: str                 # "pass" | "fail" | "skip"
    reason: str = ""
    detail: str = ""
    metadata: dict = field(default_factory=dict)


def run_with_timeout(fn: Callable[[], StepOutcome], timeout: float) -> StepOutcome:
    """Run ``fn`` in a daemon thread; never wait longer than ``timeout``.

    A step that does not finish in time becomes a failed outcome with reason
    ``"timeout"``. The worker thread is abandoned (the injected command
    runner is responsible for killing real subprocesses on its own timeout;
    the default runner kills the step's whole process group).
    """
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - reported as a failed check
            box["error"] = exc

    worker = threading.Thread(target=target, name="runner-probe-step", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return StepOutcome("fail", "timeout")
    if "error" in box:
        exc = box["error"]
        return StepOutcome("fail", "error", "%s: %s" % (type(exc).__name__, exc))
    value = box.get("value")
    if not isinstance(value, StepOutcome):
        return StepOutcome("fail", "error", "step returned no outcome")
    return value
