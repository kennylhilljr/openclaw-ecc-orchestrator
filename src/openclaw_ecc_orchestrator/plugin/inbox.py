"""Language-neutral decision inbox.

An OpenClaw plugin (any language) writes one approval decision per `*.json`
file into the inbox directory, ideally via write-to-temp then rename. The
runtime validates each through the ApprovalBroker, then moves the file and a
`<name>.result.json` envelope into `processed/`.

Processing one file:

1. claim: open it without following links, take an exclusive `flock` on the
   open file and check that the name still points at it. Several consumers
   may share an inbox (the standing `approvals process-inbox` processor and
   an `approvals approve` command); a file claimed by another consumer, or
   gone, is skipped.
2. read (regular file, at most 64 KiB, a JSON object) and resolve it through
   the ApprovalBroker, which persists its state under its own lock and emits
   `approval.resolved`.
3. record: write `processed/<name>.result.json` atomically, then rename the
   decision into `processed/` and fsync both directories.

A crash after step 2 and before step 3 leaves the file in the inbox. The next
pass resolves it again; the broker refuses it (`request_pending`, single use)
and step 3 records that refusal. A decision is never applied twice.

Only one standing processor may consume an inbox: it holds an advisory
`flock` on `processor_lock_path(state_dir, inbox)`, a file in the state dir
(never in the inbox). `probe_processor_lock` reports whether it is held
without creating anything.
"""

import datetime
import errno
import fcntl
import hashlib
import json
import os
import stat
import time

from ..runs.envelope import check, envelope
from ..runs.fsutil import _fsync_dir, atomic_write_json

MAX_BYTES = 64 * 1024
MAX_LOCK_INFO_BYTES = 4096
_POLL = 0.05
_BUSY = (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK)
_SUMMARY_FIELDS = ("request_id", "decision", "decided_by")


def _try_flock(fd, mode):
    """True when the lock was taken, False when someone else holds it."""
    try:
        fcntl.flock(fd, mode | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in _BUSY:
            return False
        raise


def _flock_wait(fd, wait):
    deadline = time.monotonic() + max(0.0, float(wait))
    while not _try_flock(fd, fcntl.LOCK_EX):
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL)
    return True


def _summary(decision):
    """The identifying fields of a decision, for reporting only."""
    if not isinstance(decision, dict):
        return {}
    return {k: decision[k][:200] for k in _SUMMARY_FIELDS if isinstance(decision.get(k), str)}


class DecisionInbox:
    def __init__(self, inbox_dir, broker):
        self.inbox_dir = os.path.abspath(inbox_dir)
        self.processed_dir = os.path.join(self.inbox_dir, "processed")
        self.broker = broker

    def pending_files(self):
        """`*.json` entries that are regular files or symlinks (symlinks are
        refused when processed). Hidden names and other types are ignored."""
        try:
            names = os.listdir(self.inbox_dir)
        except FileNotFoundError:
            return []
        pending = []
        for name in names:
            if not name.endswith(".json") or name.startswith("."):
                continue
            try:
                mode = os.lstat(os.path.join(self.inbox_dir, name)).st_mode
            except OSError:  # vanished mid-scan
                continue
            if stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                pending.append(name)
        return sorted(pending)

    # == reading ==
    def _open(self, name):
        """("fd", fd) for the regular file `name` opened without following
        links, ("refuse", reason), or ("gone", None)."""
        path = os.path.join(self.inbox_dir, name)
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            return "gone", None
        if stat.S_ISLNK(mode):
            return "refuse", "decision file is a symlink"
        if not stat.S_ISREG(mode):
            return "gone", None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return "gone", None
        except OSError as exc:
            if exc.errno == errno.ELOOP:  # replaced by a symlink mid-scan
                return "refuse", "decision file is a symlink"
            return "refuse", f"decision file cannot be opened ({errno.errorcode.get(exc.errno, exc.errno)})"
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return "refuse", "decision file is not a regular file"
        return "fd", fd

    def _same_file(self, name, fd):
        try:
            cur = os.lstat(os.path.join(self.inbox_dir, name))
        except FileNotFoundError:
            return False
        st = os.fstat(fd)
        return (cur.st_dev, cur.st_ino) == (st.st_dev, st.st_ino)

    @staticmethod
    def _read_fd(fd):
        if os.fstat(fd).st_size > MAX_BYTES:
            return None, "decision file too large"
        chunks, size = [], 0
        while size <= MAX_BYTES:
            chunk = os.read(fd, MAX_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > MAX_BYTES:
            return None, "decision file too large"
        try:
            obj = json.loads(b"".join(chunks).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            return None, "decision file is not valid JSON"
        if not isinstance(obj, dict):
            return None, "decision must be a JSON object"
        return obj, None

    # == recording ==
    def _record(self, name, result):
        """Persist the result, then move the decision into processed/."""
        os.makedirs(self.processed_dir, exist_ok=True)
        atomic_write_json(os.path.join(self.processed_dir, name + ".result.json"), result)
        try:
            os.replace(os.path.join(self.inbox_dir, name), os.path.join(self.processed_dir, name))
        except FileNotFoundError:  # moved by another consumer meanwhile
            pass
        _fsync_dir(self.inbox_dir)
        _fsync_dir(self.processed_dir)

    def _finish(self, name, result, decision=None):
        result.setdefault("data", {})
        result["source_file"] = name
        self._record(name, result)
        return dict(result, source_decision=_summary(decision))

    @staticmethod
    def _refusal(check_name, reason):
        return envelope("approval.inbox", ok=False, checks=[check(check_name, False, reason)])

    # == processing ==
    def process_one(self, name, wait=0.0):
        """Process one file. Returns its result (as recorded, plus a
        `source_decision` summary), or None when the file is gone, not a
        regular file or claimed by another consumer for longer than `wait`."""
        kind, value = self._open(name)
        if kind == "gone":
            return None
        if kind == "refuse":
            return self._finish(name, self._refusal("decision_readable", value))
        fd = value
        try:
            if not _flock_wait(fd, wait) or not self._same_file(name, fd):
                return None
            decision, error = self._read_fd(fd)
            if error:
                return self._finish(name, self._refusal("decision_readable", error))
            result = self.broker.resolve(decision)
            return self._finish(name, result, decision)
        finally:
            os.close(fd)  # releases the claim

    def reject(self, name, check_name, reason, wait=0.0):
        """Set `name` aside into processed/ with a failed check, without
        resolving it (for a file whose processing keeps failing)."""
        kind, value = self._open(name)
        if kind == "gone":
            return None
        if kind == "refuse":
            return self._finish(name, self._refusal("decision_readable", value))
        fd = value
        try:
            if not _flock_wait(fd, wait) or not self._same_file(name, fd):
                return None
            decision, _ = self._read_fd(fd)
            return self._finish(name, self._refusal(check_name, reason), decision)
        finally:
            os.close(fd)

    def process(self, dry_run=False, wait=0.0):
        names = self.pending_files()
        if dry_run:
            return [envelope("approval.inbox", changed=False, data={"files": names})] if names else []
        results = []
        for name in names:
            result = self.process_one(name, wait=wait)
            if result is not None:
                results.append(result)
        return results

    # == read only ==
    def preview_one(self, name):
        """What processing `name` would do now, changing nothing: the
        broker's read only checks, or the read refusal. None when gone."""
        kind, value = self._open(name)
        if kind == "gone":
            return None
        if kind == "refuse":
            result, decision = self._refusal("decision_readable", value), None
        else:
            try:
                decision, error = self._read_fd(value)
            finally:
                os.close(value)
            if error:
                result = self._refusal("decision_readable", error)
            else:
                result = self.broker.preview(decision)
        result["source_file"] = name
        result["source_decision"] = _summary(decision)
        return result

    def preview(self):
        return [r for r in (self.preview_one(n) for n in self.pending_files()) if r is not None]

    def read_result(self, name):
        """The recorded result for `name`, or None."""
        path = os.path.join(self.processed_dir, name + ".result.json")
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return None
        try:
            obj, error = self._read_fd(fd)
        finally:
            os.close(fd)
        return obj if error is None else None


# == the single standing processor ==
def processor_lock_path(state_dir, inbox_dir):
    """Lock file for the processor of `inbox_dir`: in the state dir, named
    after a digest of the inbox's real path."""
    digest = hashlib.sha256(os.path.realpath(inbox_dir).encode("utf-8")).hexdigest()[:16]
    return os.path.join(os.path.abspath(state_dir), f"inbox-processor-{digest}.lock")


def _lock_info(fd):
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, MAX_LOCK_INFO_BYTES)
        info = json.loads(raw.decode("utf-8")) if raw else {}
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return info if isinstance(info, dict) else {}


class ProcessorLock:
    """Exclusive advisory lock held for the life of a processor."""

    def __init__(self, path):
        self.path = path
        self._fd = None

    def acquire(self, wait=1.0, inbox=None, clock=time.time):
        """True when acquired. `wait` covers a probe holding the lock briefly."""
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if not _flock_wait(fd, wait):
                os.close(fd)
                return False
            info = {"pid": os.getpid(), "inbox": inbox,
                    "acquired_at": datetime.datetime.fromtimestamp(clock(), datetime.timezone.utc).isoformat()}
            os.ftruncate(fd, 0)
            os.pwrite(fd, json.dumps(info, sort_keys=True).encode("utf-8"), 0)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return True

    def holder(self):
        """Who holds the lock, as recorded in the lock file."""
        return probe_processor_lock(self.path)

    def release(self):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


def probe_processor_lock(path):
    """Read only: {"path", "held", "pid", "inbox", "acquired_at"}. Creates
    nothing. `held` is None (with `error`) when the lock cannot be probed."""
    out = {"path": path, "held": False, "pid": None, "inbox": None, "acquired_at": None}
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return out
    except OSError as exc:
        return dict(out, held=None, error=errno.errorcode.get(exc.errno, str(exc.errno)))
    try:
        if _try_flock(fd, fcntl.LOCK_SH):
            fcntl.flock(fd, fcntl.LOCK_UN)
            return out
        info = _lock_info(fd)
        pid = info.get("pid")
        return dict(out, held=True, pid=pid if isinstance(pid, int) else None,
                    inbox=info.get("inbox") if isinstance(info.get("inbox"), str) else None,
                    acquired_at=info.get("acquired_at") if isinstance(info.get("acquired_at"), str) else None)
    except OSError as exc:
        return dict(out, held=None, error=errno.errorcode.get(exc.errno, str(exc.errno)))
    finally:
        os.close(fd)
