"""Runner process supervision.

- argv lists only; the child gets its own session (process group)
- minimal environment: only allowlisted variable names are inherited
- stdout/stderr lines are redacted (stateful, so a multi line private key is
  masked across lines) before reaching the callback or log file
- every ``extra_env`` value is a literal secret for that process's redactor;
  argv is redacted before it is written to status.json or returned
- hard timeout and cooperative cancel: SIGTERM to the group, SIGKILL after grace
- leftover group members are killed when the main child exits, so a
  lingering grandchild can neither leak nor hold the pipes open
- exit detection: ``os.waitid(WNOWAIT)`` where available, else kqueue
  EVFILT_PROC/NOTE_EXIT (darwin, BSD), else ``Popen.poll()``; a failing
  monitor thread kills the group and records an error, so ``wait()`` always
  returns
"""

import datetime
import os
import queue
import select
import signal
import subprocess
import threading
import time

from ..runs.envelope import SCHEMA_VERSION
from ..runs.fsutil import atomic_write_json
from ..runs.liveness import is_process_alive
from .redact import Redactor, StreamRedactor, looks_secret_name, redact_argv

DEFAULT_ENV_ALLOW = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR")
MAX_LINE = 65536


def pid_gone(pid):
    return not is_process_alive({"pid": pid})


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


def build_env(base_env, allow, extra=None, public=None):
    env = {}
    for name in allow:
        if name in base_env:
            env[name] = base_env[name]
    for source in (public, extra):
        for name, value in (source or {}).items():
            env[str(name)] = str(value)
    return env


class ExitWatcher:
    """Tells whether the main child exited, without reaping it where possible.

    Modes, chosen at construction and degraded on failure:
    ``waitid`` (``os.waitid`` with ``WNOWAIT``), ``kqueue`` (EVFILT_PROC with
    NOTE_EXIT on darwin and BSD), ``poll`` (``Popen.poll()``, which reaps; the
    group is then signalled right away, a small pgid reuse window that the
    other modes avoid).
    """

    WAITID_NAMES = ("waitid", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT")

    def __init__(self, proc):
        self.proc = proc
        self.pid = proc.pid
        self.mode = None
        self.reaped = False
        self._kq = None
        if all(hasattr(os, name) for name in self.WAITID_NAMES):
            self.mode = "waitid"
        else:
            self._fallback()

    def _fallback(self):
        self._close()
        kqueue = getattr(select, "kqueue", None)
        if kqueue is not None and all(hasattr(select, n) for n in (
                "kevent", "KQ_FILTER_PROC", "KQ_NOTE_EXIT", "KQ_EV_ADD", "KQ_EV_ENABLE")):
            try:
                kq = kqueue()
                try:
                    event = select.kevent(self.pid, filter=select.KQ_FILTER_PROC,
                                          flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                                          fflags=select.KQ_NOTE_EXIT)
                    kq.control([event], 0, 0)
                except BaseException:
                    kq.close()
                    raise
                self._kq = kq
                self.mode = "kqueue"
                return
            except ProcessLookupError:
                # Our unreaped child is gone from the process table: it is a zombie.
                self.mode = "exited"
                return
            except (OSError, AttributeError, TypeError, ValueError, NotImplementedError):
                pass
        self.mode = "poll"

    def _close(self):
        if self._kq is not None:
            try:
                self._kq.close()
            except OSError:
                pass
            self._kq = None

    def degrade_to_poll(self):
        self._close()
        if self.mode not in ("exited",):
            self.mode = "poll"

    def exited(self):
        if self.mode == "waitid":
            try:
                info = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                return True
            except (NotImplementedError, AttributeError, OSError):
                self._fallback()
                return self.exited()
            return info is not None
        if self.mode == "exited":
            return True
        if self.mode == "kqueue":
            try:
                events = self._kq.control(None, 4, 0)
            except (OSError, AttributeError, TypeError, ValueError):
                self.degrade_to_poll()
                return self.exited()
            if any(getattr(e, "ident", self.pid) == self.pid for e in (events or ())):
                self._close()
                self.mode = "exited"
                return True
            return False
        if self.proc.poll() is not None:
            self.reaped = True
            return True
        return False

    def close(self):
        self._close()


class ProcessHandle:
    def __init__(self, argv, cwd, env, redactor, on_line, log_path, status_path, timeout, grace, clock, on_start):
        self.argv = list(argv)
        self.cwd = cwd
        self._env = env
        self.redactor = redactor
        self.on_line = on_line
        self.log_path = log_path
        self.status_path = status_path
        self.timeout = timeout
        self.grace = grace
        self.clock = clock
        self.on_start = on_start
        self.pid = None
        self._proc = None
        self._cancel = threading.Event()
        self._cancel_reason = None
        self._done = threading.Event()
        self._result = None
        self._thread = None
        self._watcher = None
        self._fail_lock = threading.Lock()

    # == lifecycle ==
    def _start(self):
        self._started_wall = self.clock()
        self._started_mono = time.monotonic()
        try:
            self._proc = subprocess.Popen(
                self.argv, cwd=self.cwd, env=self._env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, close_fds=True,
            )
        except OSError as exc:
            self._finish(exit_code=None, sig=None, error=f"spawn_failed: {exc.__class__.__name__}: {exc.strerror or 'error'}")
            return self
        self.pid = self._proc.pid
        if self.on_start:
            try:
                self.on_start(self.pid)
            except Exception:
                pass
        self._thread = threading.Thread(target=self._monitor, name=f"supervise-{self.pid}", daemon=True)
        self._thread.start()
        return self

    def cancel(self, reason="cancelled"):
        if self._cancel_reason is None:
            self._cancel_reason = reason
        self._cancel.set()

    def wait(self, timeout=None):
        """Block until the process is finished; ``timeout`` (seconds) raises
        TimeoutError. Never blocks forever on a dead monitor thread."""
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        while not self._done.is_set():
            step = 0.5
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("process still running")
                step = min(step, remaining)
            if self._done.wait(step):
                break
            thread = self._thread
            if thread is not None and not thread.is_alive() and not self._done.is_set():
                self._fail_safe(RuntimeError("monitor thread ended without a result"))
        return self._result

    @property
    def result(self):
        return self._result

    # == internals ==
    def _exit_probe(self):
        return self._watcher.exited()

    def _signal_group(self, sig):
        try:
            os.killpg(self.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _reader(self, stream, name, q):
        try:
            while True:
                raw = stream.readline(MAX_LINE)
                if not raw:
                    break
                q.put((name, raw.decode("utf-8", errors="replace").rstrip("\r\n")))
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass
            q.put((name, None))

    def _monitor(self):
        try:
            self._watcher = ExitWatcher(self._proc)
            self._monitor_loop()
        except BaseException as exc:  # noqa: BLE001 - must always produce a result
            self._fail_safe(exc)
        finally:
            watcher = getattr(self, "_watcher", None)
            if watcher is not None:
                watcher.close()

    def _fail_safe(self, exc):
        """Monitor failure: kill the group, reap, and record an error result."""
        with self._fail_lock:
            if self._done.is_set():
                return
            self._signal_group(signal.SIGKILL)
            returncode = None
            try:
                returncode = self._proc.wait(timeout=max(self.grace, 1.0))
            except Exception:  # noqa: BLE001
                pass
            detail = self.redactor.redact(str(exc))[:200]
            error = f"monitor_failed: {type(exc).__name__}: {detail}"
            exit_code = returncode if returncode is not None and returncode >= 0 else None
            sig = -returncode if returncode is not None and returncode < 0 else None
            try:
                self._finish(exit_code=exit_code, sig=sig, error=error)
            except BaseException:  # noqa: BLE001
                self._result = {"schema_version": SCHEMA_VERSION, "pid": self.pid, "exit_code": exit_code,
                                "signal": sig, "timed_out": False, "cancelled": False, "error": error,
                                "argv": redact_argv(self.argv, self.redactor), "lines": {"stdout": 0, "stderr": 0}}
                self._done.set()

    def _monitor_loop(self):
        q = queue.Queue()
        readers = [
            threading.Thread(target=self._reader, args=(self._proc.stdout, "stdout", q), daemon=True),
            threading.Thread(target=self._reader, args=(self._proc.stderr, "stderr", q), daemon=True),
        ]
        for t in readers:
            t.start()
        log = None
        if self.log_path:
            os.makedirs(os.path.dirname(os.path.abspath(self.log_path)), exist_ok=True)
            log = open(self.log_path, "a", encoding="utf-8")
        counts = {"stdout": 0, "stderr": 0}
        streams = {"stdout": StreamRedactor(self.redactor), "stderr": StreamRedactor(self.redactor)}
        open_streams = 2
        streams_closed_at = None
        deadline = self._started_mono + self.timeout if self.timeout else None
        timed_out = cancelled = escalated = False
        kill_at = None
        exited_at = None
        group_reaped = False
        try:
            while True:
                try:
                    name, line = q.get(timeout=0.05)
                    if line is None:
                        open_streams -= 1
                        if open_streams == 0:
                            streams_closed_at = time.monotonic()
                    else:
                        counts[name] += 1
                        clean = streams[name].feed(line)
                        if log:
                            log.write(f"[{name}] {clean}\n")
                            log.flush()
                        if self.on_line:
                            try:
                                self.on_line(name, clean)
                            except Exception:
                                pass
                except queue.Empty:
                    pass
                now = time.monotonic()
                if (self._watcher.mode == "kqueue" and streams_closed_at is not None
                        and now - streams_closed_at > 1.0):
                    # Safety net: a NOTE_EXIT that never fires must not stall us.
                    self._watcher.degrade_to_poll()
                exited = self._exit_probe()
                if exited and not group_reaped:
                    # The main child is a zombie, so its pgid cannot be reused yet.
                    self._signal_group(signal.SIGKILL)
                    group_reaped = True
                    exited_at = now
                if exited and (open_streams == 0 or now - exited_at > 2.0):
                    break
                if not exited:
                    if kill_at is None:
                        if self._cancel.is_set():
                            cancelled = True
                        elif deadline is not None and now >= deadline:
                            timed_out = True
                        if cancelled or timed_out:
                            self._signal_group(signal.SIGTERM)
                            kill_at = now + self.grace
                    elif now >= kill_at and not escalated:
                        escalated = True
                        self._signal_group(signal.SIGKILL)
            returncode = self._proc.wait()
            # Drain anything still buffered.
            while True:
                try:
                    name, line = q.get_nowait()
                except queue.Empty:
                    break
                if line is not None:
                    counts[name] += 1
                    clean = streams[name].feed(line)
                    if log:
                        log.write(f"[{name}] {clean}\n")
                    if self.on_line:
                        try:
                            self.on_line(name, clean)
                        except Exception:
                            pass
        finally:
            # Readers close their own stream at EOF. Closing here could block on
            # the buffer lock of a reader still inside readline().
            if log:
                log.close()
        exit_code = returncode if returncode >= 0 else None
        sig = -returncode if returncode < 0 else None
        if timed_out:
            exit_code = None if sig else exit_code
        self._finish(exit_code=exit_code, sig=sig, timed_out=timed_out, cancelled=cancelled,
                     escalated=escalated, counts=counts)

    def _finish(self, exit_code, sig, error=None, timed_out=False, cancelled=False, escalated=False, counts=None):
        if self._done.is_set():
            return
        ended = self.clock()
        result = {
            "schema_version": SCHEMA_VERSION,
            "argv": redact_argv(self.argv, self.redactor),
            "cwd": self.redactor.redact(self.cwd) if isinstance(self.cwd, str) else self.cwd,
            "pid": self.pid,
            "started_at": _iso(self._started_wall),
            "ended_at": _iso(ended),
            "duration_s": round(time.monotonic() - self._started_mono, 3),
            "exit_code": exit_code,
            "signal": sig,
            "timed_out": timed_out,
            "cancelled": cancelled,
            "cancel_reason": self._cancel_reason if cancelled else None,
            "escalated_to_kill": escalated,
            "error": self.redactor.redact(error) if error else error,
            "log_path": self.log_path,
            "lines": counts or {"stdout": 0, "stderr": 0},
        }
        if timed_out:
            result["exit_code"] = None
        if self.status_path:
            try:
                atomic_write_json(self.status_path, result)
            except OSError as exc:
                result["error"] = (result["error"] or "") + " status_write_failed: " + self.redactor.redact(
                    f"{type(exc).__name__}: {exc.strerror or exc}")
        self._result = result
        self._done.set()


class Supervisor:
    def __init__(self, env_allow=(), base_env=None, redactor=None, clock=time.time, grace=5.0):
        self.env_allow = tuple(env_allow)
        self.base_env = dict(os.environ if base_env is None else base_env)
        self.redactor = redactor or Redactor()
        self.clock = clock
        self.grace = float(grace)

    def start(self, argv, cwd, *, env_allow=(), extra_env=None, public_env=None, on_line=None, log_path=None,
              status_path=None, timeout=None, grace=None, on_start=None):
        """Start ``argv`` in ``cwd``.

        ``extra_env`` values are secrets: every value is registered with this
        process's redactor whatever the variable name. ``public_env`` values
        (for example a per unit TMPDIR) are set without being treated as secrets.
        """
        if isinstance(argv, (str, bytes)) or not isinstance(argv, (list, tuple)) or not argv:
            raise TypeError("argv must be a non-empty list of strings (no shell strings)")
        if not all(isinstance(a, str) for a in argv):
            raise TypeError("argv entries must be strings")
        allow = tuple(dict.fromkeys(DEFAULT_ENV_ALLOW + self.env_allow + tuple(env_allow)))
        env = build_env(self.base_env, allow, extra_env, public_env)
        if hasattr(self.redactor, "copy"):
            redactor = self.redactor.copy()
        else:
            redactor = Redactor(getattr(self.redactor, "_secrets", ()), getattr(self.redactor, "patterns", None))
        for name, value in env.items():
            if looks_secret_name(name):
                redactor.add_secret(value)
        for value in (extra_env or {}).values():
            redactor.add_secret(str(value))
        handle = ProcessHandle(argv, cwd, env, redactor, on_line, log_path, status_path, timeout,
                               self.grace if grace is None else float(grace), self.clock, on_start)
        return handle._start()

    def run(self, argv, cwd, **kwargs):
        return self.start(argv, cwd, **kwargs).wait()
