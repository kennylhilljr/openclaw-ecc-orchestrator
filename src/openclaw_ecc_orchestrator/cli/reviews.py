"""Plan review records: the only thing `--yes` accepts.

A record lives at `<state>/reviews/<review_id>.json` and binds the SHA-256
of the exact generated plan (canonical JSON) to an explicit operator,
session and approval time. It is valid for non destructive plans only, for
`review_ttl` seconds, and once: using it moves it to `reviews/consumed/`.
"""

import datetime
import json
import os

from ..runs.envelope import SCHEMA_VERSION, check
from ..runs.fsutil import atomic_write_json
from ..runs.store import iso
from .config import valid_handle, valid_id

MAX_RECORD_BYTES = 256 * 1024


def _parse_iso(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


class ReviewStore:
    def __init__(self, reviews_dir):
        self.dir = reviews_dir
        self.consumed_dir = os.path.join(reviews_dir, "consumed")

    def path(self, review_id):
        if not valid_id(review_id):
            raise ValueError(f"invalid review id {review_id!r}")
        return os.path.join(self.dir, f"{review_id}.json")

    def consumed_path(self, review_id):
        return os.path.join(self.consumed_dir, f"{review_id}.json")

    def exists(self, review_id):
        return os.path.lexists(self.path(review_id)) or os.path.lexists(self.consumed_path(review_id))

    def write(self, *, review_id, operation, plan_sha256, operator, session, now, ttl, plan_redacted):
        record = {
            "schema_version": SCHEMA_VERSION,
            "review_id": review_id,
            "operation": operation,
            "plan_sha256": plan_sha256,
            "destructive": False,
            "approved_by": operator,
            "session": session,
            "approved_at": iso(now),
            "expires_at": iso(now + float(ttl)),
            "plan": plan_redacted,
        }
        atomic_write_json(self.path(review_id), record)
        return record

    def _read(self, path):
        if os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) > MAX_RECORD_BYTES:
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        return doc if isinstance(doc, dict) else None

    def validate(self, review_id, operation, plan_sha256, now):
        """Checks for using `review_id` to run `operation` with `plan_sha256`."""
        path = self.path(review_id)
        if not os.path.lexists(path):
            if os.path.lexists(self.consumed_path(review_id)):
                return [check("review_record_found", True, review_id),
                        check("review_not_consumed", False, "review record was already used")], None
            return [check("review_record_found", False, review_id)], None
        record = self._read(path)
        if record is None:
            return [check("review_record_found", True, review_id),
                    check("review_record_readable", False, "not a regular JSON object file")], None
        expires = _parse_iso(record.get("expires_at"))
        approved = _parse_iso(record.get("approved_at"))
        checks = [
            check("review_record_found", True, review_id),
            check("review_not_consumed", True),
            check("review_schema_version", record.get("schema_version") == SCHEMA_VERSION,
                  str(record.get("schema_version"))),
            check("review_id_matches", record.get("review_id") == review_id, str(record.get("review_id"))),
            check("review_operation_matches", record.get("operation") == operation,
                  f"record={record.get('operation')} command={operation}"),
            check("review_plan_hash_matches", record.get("plan_sha256") == plan_sha256,
                  f"record={record.get('plan_sha256')} plan={plan_sha256}"),
            check("review_not_destructive", record.get("destructive") is False,
                  f"destructive={record.get('destructive')!r}"),
            check("review_operator_present", valid_handle(record.get("approved_by")), ""),
            check("review_session_present", valid_handle(record.get("session")), ""),
            check("review_approved_at_valid", approved is not None and approved <= now + 60,
                  str(record.get("approved_at"))),
            check("review_not_expired", expires is not None and now < expires, str(record.get("expires_at"))),
        ]
        return checks, record

    def consume(self, review_id, now, operation):
        """Move the record aside so it can never authorize a second run. The
        rename is atomic, so of two concurrent users exactly one wins.
        Returns False when the record was already gone."""
        os.makedirs(self.consumed_dir, exist_ok=True)
        target = self.consumed_path(review_id)
        if os.path.lexists(target):
            return False
        try:
            os.rename(self.path(review_id), target)
        except FileNotFoundError:
            return False
        record = self._read(target) or {}
        record["consumed_at"] = iso(now)
        record["consumed_by_operation"] = operation
        atomic_write_json(target, record)
        return True
