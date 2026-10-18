"""Health monitoring for supervised processes: tell a hung process from a slow one.

There is no wall clock limit on a runner or a gate. Instead, while a process
runs, its health is checked every ``interval_seconds``. Each check samples
progress signals and compares them with the previous check:

* ``cpu``: cumulative CPU time of the whole process group (``/proc`` on
  Linux, ``ps`` elsewhere) increased by more than a small noise floor;
* ``output``: new stdout or stderr bytes;
* ``events``: new parsed stream events (JSON object lines);
* ``worktree``: the worktree changed (``git status`` digest plus the mtime and
  size of every changed path, and the HEAD commit);
* ``members``: the set of processes in the group changed.

A check is *flat* only when the main process is alive and every signal was
read successfully and none moved. A signal that could not be read makes the
check indeterminate: it neither counts as flat nor resets the count, so a
broken probe can never make a busy process look hung. ``flat_checks_for_hang``
consecutive flat checks mean the process is hung; any movement resets the
count. A process that is no longer alive is never hung: its exit path
handles it.

States: ``healthy``, ``suspected_hang`` (at least one flat check),
``hung`` (the threshold was reached; the supervisor then cancels the group).
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import math
import os
import subprocess
import sys
import time
from typing import Any, Callable

from ..runs import liveness as L

DEFAULT_INTERVAL_SECONDS = 120.0
DEFAULT_FLAT_CHECKS_FOR_HANG = 5
INTERVAL_RANGE = (30.0, 1800.0)
FLAT_CHECKS_RANGE = (2, 20)
# CPU growth below this fraction of the interval counts as noise (timer ticks
# of an idle event loop). 0.001 of 120 seconds is 0.12 CPU seconds.
CPU_NOISE_FRACTION = 0.001
PS_TIMEOUT = 5.0
MAX_PS_LINES = 200_000
MAX_WORKTREE_PATHS = 2000
GIT_STATUS_TIMEOUT = 60

HEALTHY = "healthy"
SUSPECTED_HANG = "suspected_hang"
HUNG = "hung"
STATES = (HEALTHY, SUSPECTED_HANG, HUNG)
SIGNALS = ("cpu", "output", "events", "worktree", "members")


class _Unknown:
    def __repr__(self):
        return "UNKNOWN"


# A signal whose source failed on this check.
UNKNOWN = _Unknown()


class HealthSettingsError(ValueError):
    pass


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def health_block_errors(block: Any, label: str = "health") -> list[str]:
    """Validation errors for a ``health`` block of a repository policy or a
    work unit: ``interval_seconds`` 30 to 1800, ``flat_checks_for_hang`` 2 to 20."""
    if not isinstance(block, dict):
        return ["%s must be an object" % label]
    errors = []
    for key in block:
        if key not in ("interval_seconds", "flat_checks_for_hang"):
            errors.append("%s has unknown field %r" % (label, str(key)[:40]))
    if "interval_seconds" in block:
        value = block["interval_seconds"]
        lo, hi = INTERVAL_RANGE
        if not (_is_num(value) and lo <= value <= hi):
            errors.append("%s.interval_seconds must be a number from %d to %d" % (label, lo, hi))
    if "flat_checks_for_hang" in block:
        value = block["flat_checks_for_hang"]
        lo, hi = FLAT_CHECKS_RANGE
        if not (isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi):
            errors.append("%s.flat_checks_for_hang must be an integer from %d to %d" % (label, lo, hi))
    return errors


@dataclasses.dataclass(frozen=True)
class HealthSettings:
    """``interval_seconds`` between checks; ``flat_checks_for_hang``
    consecutive flat checks before a process counts as hung.

    Values outside the policy ranges are refused unless ``test_override`` is
    set, which only tests use (see :meth:`for_tests`)."""

    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    flat_checks_for_hang: int = DEFAULT_FLAT_CHECKS_FOR_HANG
    test_override: bool = dataclasses.field(default=False, compare=False)

    def __post_init__(self):
        if self.test_override:
            if not (_is_num(self.interval_seconds) and self.interval_seconds > 0):
                raise HealthSettingsError("interval_seconds must be positive")
            if not (isinstance(self.flat_checks_for_hang, int) and self.flat_checks_for_hang >= 1):
                raise HealthSettingsError("flat_checks_for_hang must be a positive integer")
            return
        errors = health_block_errors({"interval_seconds": self.interval_seconds,
                                      "flat_checks_for_hang": self.flat_checks_for_hang})
        if errors:
            raise HealthSettingsError("; ".join(errors))

    @classmethod
    def for_tests(cls, interval_seconds: float, flat_checks_for_hang: int) -> "HealthSettings":
        """Test only: allows intervals below the 30 second policy minimum."""
        return cls(float(interval_seconds), int(flat_checks_for_hang), test_override=True)

    def to_dict(self) -> dict:
        return {"interval_seconds": self.interval_seconds, "flat_checks_for_hang": self.flat_checks_for_hang}


def resolve_health_settings(policy: dict | None = None, unit: dict | None = None,
                            override: HealthSettings | None = None) -> HealthSettings:
    """Repository policy ``health`` block, then the work unit's ``health``
    block on top; an explicit ``override`` wins outright."""
    if override is not None:
        return override
    merged = {"interval_seconds": DEFAULT_INTERVAL_SECONDS,
              "flat_checks_for_hang": DEFAULT_FLAT_CHECKS_FOR_HANG}
    for source in ((policy or {}).get("health"), (unit or {}).get("health")):
        if isinstance(source, dict):
            merged.update({k: v for k, v in source.items() if k in merged})
    return HealthSettings(float(merged["interval_seconds"]), int(merged["flat_checks_for_hang"]))


# == signal sources ==

def parse_cpu_time(text: str) -> float:
    """``ps`` time column: ``[dd-][hh:]mm:ss[.cc]`` (procps and BSD forms)."""
    text = text.strip()
    days = 0
    if "-" in text:
        day_part, text = text.split("-", 1)
        days = int(day_part)
    parts = text.split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError("bad cpu time %r" % text)
    seconds = float(parts[-1])
    minutes = int(parts[-2]) if len(parts) >= 2 else 0
    hours = int(parts[-3]) if len(parts) == 3 else 0
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


@dataclasses.dataclass(frozen=True)
class GroupSnapshot:
    cpu_seconds: float
    members: frozenset


def _proc_group_snapshot(pgid: int) -> GroupSnapshot | None:
    """Linux: sum utime, stime and the reaped children's cutime and cstime of
    every process whose pgrp is ``pgid``. None when /proc is unusable."""
    if not os.path.isdir("/proc/self"):
        return None
    try:
        ticks = float(os.sysconf("SC_CLK_TCK"))
        entries = os.listdir("/proc")
    except (OSError, ValueError, AttributeError):
        return None
    cpu = 0.0
    members = set()
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name, "r") as fh:
                raw = fh.read()
        except OSError:
            continue
        rest = raw[raw.rfind(")") + 2:].split()
        if len(rest) < 15:
            continue
        try:
            if int(rest[2]) != pgid:
                continue
            cpu += sum(int(rest[i]) for i in (11, 12, 13, 14)) / ticks
        except ValueError:
            continue
        if rest[0] not in ("Z", "X"):
            members.add(int(name))
    return GroupSnapshot(cpu, frozenset(members))


def _ps_group_snapshot(pgid: int, runner: Callable | None = None) -> GroupSnapshot:
    """``ps -A -o pid= -o pgid= -o stat= -o time=`` under the C locale,
    filtered to ``pgid``. Works on macOS, the BSDs and Linux procps."""
    runner = runner or subprocess.run
    env = {"LC_ALL": "C", "PATH": os.environ.get("PATH", "/bin:/usr/bin")}
    argv = ["ps", "-A", "-o", "pid=", "-o", "pgid=", "-o", "stat=", "-o", "time="]
    proc = runner(argv, capture_output=True, text=True, timeout=PS_TIMEOUT, env=env,
                  stdin=subprocess.DEVNULL, check=False)
    if proc.returncode != 0:
        raise OSError("ps exited %s" % proc.returncode)
    cpu = 0.0
    members = set()
    for index, line in enumerate((proc.stdout or "").splitlines()):
        if index >= MAX_PS_LINES:
            break
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            pid, group = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if group != pgid:
            continue
        cpu += parse_cpu_time(parts[3])
        if not parts[2].startswith(("Z", "X")):
            members.add(pid)
    return GroupSnapshot(cpu, frozenset(members))


def group_snapshot(pgid: int, platform: str | None = None, ps_runner: Callable | None = None) -> GroupSnapshot:
    """CPU seconds and live member pids of process group ``pgid``."""
    platform = sys.platform if platform is None else platform
    if platform.startswith("linux") and ps_runner is None:
        snap = _proc_group_snapshot(pgid)
        if snap is not None:
            return snap
    return _ps_group_snapshot(pgid, ps_runner)


def _porcelain_paths(output: str) -> list[str]:
    """Paths from ``git status --porcelain=v2 -z`` output."""
    tokens = output.split("\0")
    paths = []
    skip = False
    for token in tokens:
        if skip:
            skip = False
            continue
        if not token or token.startswith("#"):
            continue
        kind = token[0]
        if kind == "1":
            fields = token.split(" ", 8)
        elif kind == "2":
            fields = token.split(" ", 9)
            skip = True     # the original path follows as its own token
        elif kind == "u":
            fields = token.split(" ", 10)
        elif kind in ("?", "!"):
            fields = token.split(" ", 1)
        else:
            continue
        if fields:
            paths.append(fields[-1])
    return paths


def worktree_digest(path: str) -> str:
    """Digest of the worktree state: HEAD, ``git status`` (porcelain v2,
    untracked files included) and mtime plus size of each changed path.
    Runs through the hardened orchestrator git wrapper."""
    from ..worktrees.git import run_git  # local import keeps this module light

    proc = run_git(["status", "--porcelain=v2", "--branch", "--untracked-files=all", "-z"], path,
                   check=False, timeout=GIT_STATUS_TIMEOUT)
    if proc.returncode != 0:
        raise OSError("git status exited %s" % proc.returncode)
    digest = hashlib.sha256(proc.stdout.encode("utf-8", "surrogateescape"))
    for rel in _porcelain_paths(proc.stdout)[:MAX_WORKTREE_PATHS]:
        try:
            st = os.lstat(os.path.join(path, rel))
            digest.update(("%s\0%d\0%d\0" % (rel, st.st_mtime_ns, st.st_size)).encode("utf-8", "surrogateescape"))
        except OSError:
            digest.update(("%s\0gone\0" % rel).encode("utf-8", "surrogateescape"))
    return digest.hexdigest()


# == monitor ==

@dataclasses.dataclass(frozen=True)
class Sample:
    """One reading. ``signals`` maps a signal name to its value or UNKNOWN;
    signals without a configured source are absent."""
    alive: bool
    signals: dict
    at: float = 0.0


@dataclasses.dataclass(frozen=True)
class CheckResult:
    state: str
    previous_state: str
    changed: bool
    alive: bool
    flat: bool
    flat_checks: int
    moved: tuple
    unknown: tuple
    evidence: dict
    checks: int
    elapsed_seconds: float = 0.0

    @property
    def hung(self) -> bool:
        return self.state == HUNG


def _iso(ts: float) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


class HealthMonitor:
    """Compares each sample with the previous one and tracks the flat streak.

    ``sampler()`` returns a :class:`Sample`. Call :meth:`start` once for the
    baseline, then :meth:`check` every interval. The clock is injected."""

    def __init__(self, settings: HealthSettings, sampler: Callable[[], Sample], clock: Callable[[], float] = time.time):
        self.settings = settings
        self.sampler = sampler
        self.clock = clock
        self.state = HEALTHY
        self.flat_checks = 0
        self.checks = 0
        self.previous: Sample | None = None
        self.last: CheckResult | None = None

    def start(self) -> Sample:
        self.previous = self.sampler()
        return self.previous

    def _moved(self, name: str, cur: Any, prev: Any) -> bool:
        if name == "cpu":
            floor = CPU_NOISE_FRACTION * float(self.settings.interval_seconds)
            return float(cur) - float(prev) > floor
        if name in ("output", "events"):
            return cur > prev
        return cur != prev

    def check(self) -> CheckResult:
        sample = self.sampler()
        prev = self.previous
        self.checks += 1
        previous_state = self.state
        moved, unknown = [], []
        evidence: dict[str, Any] = {"checked_at": _iso(sample.at or self.clock()), "alive": bool(sample.alive),
                                    "interval_seconds": self.settings.interval_seconds,
                                    "flat_checks_for_hang": self.settings.flat_checks_for_hang}
        if not sample.alive:
            # Not hung: the exit path owns a process that is gone.
            evidence.update({"flat_checks": self.flat_checks, "note": "process not alive"})
            self.previous = sample
            result = CheckResult(self.state, previous_state, False, False, False, self.flat_checks, (), (),
                                 evidence, self.checks)
            self.last = result
            return result
        names = [n for n in SIGNALS if n in sample.signals or (prev is not None and n in prev.signals)]
        for name in names:
            cur = sample.signals.get(name, UNKNOWN)
            before = prev.signals.get(name, UNKNOWN) if prev is not None else UNKNOWN
            if cur is UNKNOWN or before is UNKNOWN:
                unknown.append(name)
            elif self._moved(name, cur, before):
                moved.append(name)
        if moved:
            self.flat_checks = 0
            flat = False
        elif unknown:
            flat = False     # indeterminate: keep the streak as it is
        else:
            flat = True
            self.flat_checks += 1
        if self.flat_checks >= self.settings.flat_checks_for_hang:
            self.state = HUNG
        elif self.flat_checks >= 1:
            self.state = SUSPECTED_HANG
        else:
            self.state = HEALTHY
        cpu, cpu_prev = sample.signals.get("cpu"), prev.signals.get("cpu") if prev else None
        if _is_num(cpu):
            evidence["cpu_seconds"] = round(float(cpu), 3)
            if _is_num(cpu_prev):
                evidence["cpu_delta_seconds"] = round(float(cpu) - float(cpu_prev), 3)
        for name in ("output", "events"):
            cur, before = sample.signals.get(name), prev.signals.get(name) if prev else None
            if isinstance(cur, int):
                evidence["%s_total" % ("output_bytes" if name == "output" else "events")] = cur
                if isinstance(before, int):
                    evidence["%s_delta" % ("output_bytes" if name == "output" else "events")] = cur - before
        members = sample.signals.get("members")
        if isinstance(members, frozenset):
            evidence["group_processes"] = len(members)
            evidence["group_changed"] = "members" in moved
        if "worktree" in sample.signals:
            evidence["worktree_changed"] = "worktree" in moved
        evidence.update({"flat": flat, "flat_checks": self.flat_checks, "moved": list(moved),
                         "unknown": list(unknown)})
        self.previous = sample
        result = CheckResult(self.state, previous_state, self.state != previous_state, True, flat,
                             self.flat_checks, tuple(moved), tuple(unknown), evidence, self.checks)
        self.last = result
        return result


@dataclasses.dataclass
class HealthConfig:
    """What a supervised process is monitored with.

    ``worktree`` enables the worktree signal; ``on_check(result)`` is called
    after every check (from the monitor thread; exceptions are ignored);
    ``parse_events`` counts JSON object lines as stream events."""
    settings: HealthSettings = dataclasses.field(default_factory=HealthSettings)
    worktree: str | None = None
    on_check: Callable[[CheckResult], None] | None = None
    parse_events: bool = True
    group_snapshot: Callable[[int], GroupSnapshot] | None = None


def process_sampler(pid: int, activity: Callable[[], dict], config: HealthConfig,
                    clock: Callable[[], float] = time.time, identity: dict | None = None,
                    probe: Any = None) -> Callable[[], Sample]:
    """Sampler for a real process group led by ``pid``: liveness through
    ``runs.liveness``, group CPU and members, output counters from
    ``activity()`` and, when configured, the worktree digest."""
    record = {"pid": pid}
    record.update(identity if identity is not None else L.capture_identity(pid, probe=probe))
    snapshot = config.group_snapshot or group_snapshot
    # Only a git checkout has a worktree signal; elsewhere it is absent, not
    # permanently unreadable (which would keep every check indeterminate).
    worktree = config.worktree if config.worktree and os.path.exists(os.path.join(config.worktree, ".git")) \
        else None

    def sample() -> Sample:
        alive = L.process_liveness(record, probe=probe) != L.DEAD
        signals: dict[str, Any] = {}
        try:
            snap = snapshot(pid)
            signals["cpu"] = float(snap.cpu_seconds)
            signals["members"] = frozenset(snap.members)
        except Exception:  # noqa: BLE001 - an unreadable source is indeterminate, never flat
            signals["cpu"] = UNKNOWN
            signals["members"] = UNKNOWN
        try:
            counts = activity()
            signals["output"] = int(counts.get("bytes", 0))
            if config.parse_events:
                signals["events"] = int(counts.get("events", 0))
        except Exception:  # noqa: BLE001
            signals["output"] = UNKNOWN
        if worktree:
            try:
                signals["worktree"] = worktree_digest(worktree)
            except Exception:  # noqa: BLE001
                signals["worktree"] = UNKNOWN
        return Sample(alive=alive, signals=signals, at=clock())

    return sample
