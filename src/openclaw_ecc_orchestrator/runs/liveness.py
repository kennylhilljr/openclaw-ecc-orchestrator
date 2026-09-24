"""Best effort process liveness checks used on resume.

A process record is {"pid": int, ...} plus optional identity fields:

- "start_token" / "start_method": the process start time as captured by the
  probe that recorded it ("proc_stat_ticks" on Linux, "ps_lstart" on
  macOS/BSD). Comparing it later detects pid reuse.
- "start_ticks": legacy Linux-only field (the /proc start time). Still
  written on every platform (None where /proc is absent) and still honoured
  when "start_token" is missing.

`process_liveness` returns one of:

- DEAD: the pid is gone, a zombie, or now belongs to a different process.
- ALIVE: the pid exists and its start time matches the record.
- UNVERIFIED: the pid exists (or may exist) but its identity could not be
  checked (no start time recorded, probe unavailable/timed out, or the
  record came from a different method). Callers must treat this as
  "possibly still running": never auto-reassign, flag for the user.

The platform strategy is a probe object with `kill(pid, sig)` and
`identity(pid)`; `default_probe()` picks one from `sys.platform` at call time
so tests can patch the platform or pass their own probe.
"""

import os
import subprocess
import sys
from collections import namedtuple

DEAD = "dead"
ALIVE = "alive"
UNVERIFIED = "unverified"

METHOD_PROC = "proc_stat_ticks"
METHOD_PS = "ps_lstart"

PS_TIMEOUT = 2.0

# (state, token, method): state is the kernel/ps state string ("S", "Z", "Ss"...).
Identity = namedtuple("Identity", "state token method")


class _Gone:
    def __repr__(self):
        return "GONE"


# identity() result meaning "the pid definitely no longer exists".
GONE = _Gone()


def _proc_stat(pid):
    try:
        with open(f"/proc/{int(pid)}/stat", "r") as fh:
            raw = fh.read()
    except OSError:
        return None
    # comm may contain spaces; fields after the closing paren are stable.
    rest = raw[raw.rfind(")") + 2:].split()
    return rest


def process_start_ticks(pid):
    """Kernel start time of a pid (Linux), used to detect pid reuse."""
    rest = _proc_stat(pid)
    if not rest or len(rest) < 20:
        return None
    return rest[19]


class KillOnlyProbe:
    """Existence via signal 0 only; identity is never available."""

    def kill(self, pid, sig):
        os.kill(pid, sig)

    def identity(self, pid):
        return None


class ProcProbe(KillOnlyProbe):
    """Linux: state and start ticks from /proc/<pid>/stat."""

    def identity(self, pid):
        try:
            with open(f"/proc/{int(pid)}/stat", "r") as fh:
                raw = fh.read()
        except FileNotFoundError:
            return GONE if os.path.isdir("/proc/self") else None
        except OSError:
            return None
        rest = raw[raw.rfind(")") + 2:].split()
        if len(rest) < 20:
            return None
        return Identity(state=rest[0], token=rest[19], method=METHOD_PROC)


class PsProbe(KillOnlyProbe):
    """macOS/BSD: `ps -o stat= -o lstart= -p <pid>` under the C locale.

    lstart has one-second resolution, so a pid reused within the same second
    is not detected; that window is accepted.
    """

    def __init__(self, runner=None, timeout=PS_TIMEOUT):
        self.runner = runner
        self.timeout = timeout

    def identity(self, pid):
        runner = self.runner or subprocess.run
        env = {"LC_ALL": "C", "PATH": os.environ.get("PATH", "/bin:/usr/bin")}
        argv = ["ps", "-o", "stat=", "-o", "lstart=", "-p", str(int(pid))]
        try:
            proc = runner(argv, capture_output=True, text=True, timeout=self.timeout,
                          env=env, stdin=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        parts = (proc.stdout or "").split()
        if not parts:
            # ps exits 1 with no output when the pid does not exist.
            return GONE if proc.returncode == 1 else None
        if proc.returncode != 0 or len(parts) < 6:
            return None
        return Identity(state=parts[0], token=" ".join(parts[1:]), method=METHOD_PS)


def default_probe(platform=None):
    platform = sys.platform if platform is None else platform
    if platform.startswith("linux"):
        return ProcProbe()
    if platform == "darwin" or "bsd" in platform or platform.startswith("dragonfly"):
        return PsProbe()
    return KillOnlyProbe()


def _valid_pid(record):
    pid = record.get("pid") if isinstance(record, dict) else None
    return pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else None


def capture_identity(pid, probe=None):
    """Identity fields to store alongside a pid when a process is recorded."""
    probe = probe or default_probe()
    ident = probe.identity(pid) if isinstance(pid, int) and pid > 0 else None
    if not isinstance(ident, Identity):
        return {"start_ticks": None, "start_token": None, "start_method": None}
    return {
        "start_ticks": ident.token if ident.method == METHOD_PROC else None,
        "start_token": ident.token,
        "start_method": ident.method,
    }


def _expected_identity(record):
    if record.get("start_token") is not None and record.get("start_method"):
        return str(record["start_token"]), record["start_method"]
    if record.get("start_ticks") is not None:
        return str(record["start_ticks"]), METHOD_PROC
    return None


def process_liveness(record, probe=None):
    """Classify a process record as DEAD, ALIVE, or UNVERIFIED."""
    pid = _valid_pid(record)
    if pid is None:
        return DEAD
    probe = probe or default_probe()
    try:
        probe.kill(pid, 0)
    except ProcessLookupError:
        return DEAD
    except PermissionError:
        pass  # EPERM: the pid exists but belongs to another user
    except OSError:
        return UNVERIFIED
    ident = probe.identity(pid)
    if ident is GONE:
        return DEAD
    if not isinstance(ident, Identity):
        return UNVERIFIED
    if ident.state and ident.state[0] in ("Z", "X"):
        return DEAD
    expected = _expected_identity(record)
    if expected is None or expected[1] != ident.method:
        return UNVERIFIED
    return ALIVE if expected[0] == str(ident.token) else DEAD


def is_process_alive(record, probe=None):
    """True when the recorded process may still be running (ALIVE or UNVERIFIED)."""
    return process_liveness(record, probe=probe) != DEAD
