"""Versioned events emitted toward OpenClaw. See docs/openclaw-events.md.

The runtime only emits; OpenClaw owns sessions, approvals UI and rendering.
Every payload passes through secret redaction and string truncation.
"""

import datetime
import os
import threading
import time
import uuid

from ..process.redact import redact_obj
from ..runs.envelope import SCHEMA_VERSION
from ..runs.fsutil import FileLock, append_jsonl, read_jsonl
from ..runs.store import valid_identifier

EVENT_VERSION = 1
MAX_STRING = 2000

# type -> (required data fields, unit_id required)
EVENT_TYPES = {
    "run.created": (("plan_sha256", "unit_ids"), False),
    "unit.state_changed": (("from", "to"), True),
    "unit.progress": (("message",), True),
    "attention.required": (("reason",), False),
    "approval.requested": (("request_id", "action", "plan_sha256", "expires_at"), True),
    "approval.resolved": (("request_id", "decision"), True),
    "merge.completed": (("target_branch", "merged_commit"), True),
}


class EventError(ValueError):
    pass


def _truncate(obj):
    if isinstance(obj, str):
        return obj if len(obj) <= MAX_STRING else obj[:MAX_STRING] + "...[truncated]"
    if isinstance(obj, dict):
        return {str(k): _truncate(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_truncate(v) for v in obj]
    return obj


def make_event(event_type, run_id, unit_id=None, data=None, *, seq=0, clock=time.time, id_gen=None):
    if event_type not in EVENT_TYPES:
        raise EventError(f"unknown event type {event_type!r}")
    if not valid_identifier(run_id):
        raise EventError(f"invalid run id {run_id!r}")
    required, needs_unit = EVENT_TYPES[event_type]
    if needs_unit and not valid_identifier(unit_id or ""):
        raise EventError(f"{event_type} requires a valid unit_id")
    data = dict(data or {})
    missing = [f for f in required if f not in data]
    if missing:
        raise EventError(f"{event_type} missing fields {missing}")
    now = clock()
    return {
        "schema_version": SCHEMA_VERSION,
        "event_version": EVENT_VERSION,
        "type": event_type,
        "id": (id_gen or (lambda: uuid.uuid4().hex))(),
        "seq": int(seq),
        "emitted_at": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(),
        "run_id": run_id,
        "unit_id": unit_id,
        "data": _truncate(redact_obj(data)),
    }


class EventEmitter:
    """Interface: emit(event_type, run_id, unit_id=None, data=None) -> event."""

    def emit(self, event_type, run_id, unit_id=None, data=None):
        raise NotImplementedError


class MemoryEventSink(EventEmitter):
    def __init__(self, clock=time.time, id_gen=None):
        self.clock = clock
        self.id_gen = id_gen
        self.events = []
        self._lock = threading.Lock()

    def emit(self, event_type, run_id, unit_id=None, data=None):
        with self._lock:
            event = make_event(event_type, run_id, unit_id, data, seq=len(self.events) + 1, clock=self.clock,
                               id_gen=self.id_gen)
            self.events.append(event)
            return event


class JsonlEventSink(EventEmitter):
    """Append-only JSONL file; `seq` is monotonic across processes (file lock)."""

    def __init__(self, path, clock=time.time, id_gen=None):
        self.path = os.path.abspath(path)
        self.clock = clock
        self.id_gen = id_gen
        self._thread_lock = threading.Lock()

    def read(self, after_seq=0):
        return [e for e in read_jsonl(self.path) if int(e.get("seq", 0)) > after_seq]

    def emit(self, event_type, run_id, unit_id=None, data=None):
        with self._thread_lock, FileLock(self.path + ".lock"):
            last = max((int(e.get("seq", 0)) for e in read_jsonl(self.path)), default=0)
            event = make_event(event_type, run_id, unit_id, data, seq=last + 1, clock=self.clock, id_gen=self.id_gen)
            append_jsonl(self.path, event)
            return event


class FanoutEmitter(EventEmitter):
    def __init__(self, *emitters):
        self.emitters = list(emitters)

    def emit(self, event_type, run_id, unit_id=None, data=None):
        last = None
        for emitter in self.emitters:
            last = emitter.emit(event_type, run_id, unit_id=unit_id, data=data)
        return last
