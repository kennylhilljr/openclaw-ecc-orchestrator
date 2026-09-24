"""File-backed run store: JSON snapshot, per-run lock, append-only event log.

Layout under the configured root:

    <root>/<run_id>/run.json      snapshot (atomic replace)
    <root>/<run_id>/events.jsonl  append-only log with monotonic `seq`
    <root>/<run_id>/.lock         per-run flock
"""

import datetime
import os
import re
import time

from .envelope import SCHEMA_VERSION
from .fsutil import FileLock, append_jsonl, atomic_write_json, read_json, read_jsonl

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


class StoreError(RuntimeError):
    pass


def valid_identifier(value):
    return isinstance(value, str) and bool(_ID_RE.match(value)) and ".." not in value


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


class RunStore:
    def __init__(self, root, clock=time.time, lock_timeout=10.0):
        self.root = os.path.abspath(root)
        self.clock = clock
        self.lock_timeout = lock_timeout

    def run_dir(self, run_id):
        if not valid_identifier(run_id):
            raise StoreError(f"invalid run id {run_id!r}")
        return os.path.join(self.root, run_id)

    def snapshot_path(self, run_id):
        return os.path.join(self.run_dir(run_id), "run.json")

    def events_path(self, run_id):
        return os.path.join(self.run_dir(run_id), "events.jsonl")

    def lock(self, run_id, timeout=None):
        path = os.path.join(self.run_dir(run_id), ".lock")
        return FileLock(path, timeout=self.lock_timeout if timeout is None else timeout)

    def exists(self, run_id):
        return os.path.exists(self.snapshot_path(run_id)) or os.path.exists(self.events_path(run_id))

    def create(self, doc):
        run_id = doc["run_id"]
        if self.exists(run_id):
            raise StoreError(f"run {run_id!r} already exists")
        os.makedirs(self.run_dir(run_id), exist_ok=True)
        self.save(doc)

    def save(self, doc):
        doc = dict(doc)
        doc["schema_version"] = SCHEMA_VERSION
        atomic_write_json(self.snapshot_path(doc["run_id"]), doc)

    def load(self, run_id):
        path = self.snapshot_path(run_id)
        if not os.path.exists(path):
            raise StoreError(f"run {run_id!r} has no snapshot")
        return read_json(path)

    def read_events(self, run_id, after_seq=0):
        return [e for e in read_jsonl(self.events_path(run_id)) if int(e.get("seq", 0)) > after_seq]

    def last_seq(self, run_id):
        events = read_jsonl(self.events_path(run_id))
        return max((int(e.get("seq", 0)) for e in events), default=0)

    def append_event(self, run_id, event_type, data):
        """Append one event. Callers that mutate should hold `lock(run_id)`."""
        seq = self.last_seq(run_id) + 1
        now = self.clock()
        event = {
            "schema_version": SCHEMA_VERSION,
            "seq": seq,
            "type": event_type,
            "run_id": run_id,
            "at": iso(now),
            "ts": now,
            "data": data,
        }
        append_jsonl(self.events_path(run_id), event)
        return event

    def list_runs(self):
        if not os.path.isdir(self.root):
            return []
        return sorted(
            name for name in os.listdir(self.root)
            if valid_identifier(name) and os.path.isdir(os.path.join(self.root, name))
        )
