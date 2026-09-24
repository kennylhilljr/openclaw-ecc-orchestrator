"""Language-neutral decision inbox.

An OpenClaw plugin (any language) writes one approval decision per `*.json`
file into the inbox directory, ideally via write-to-temp then rename. The
runtime validates each through the ApprovalBroker, then moves the file and a
`<name>.result.json` envelope into `processed/`.
"""

import json
import os

from ..runs.envelope import check, envelope
from ..runs.fsutil import atomic_write_json

MAX_BYTES = 64 * 1024


class DecisionInbox:
    def __init__(self, inbox_dir, broker):
        self.inbox_dir = os.path.abspath(inbox_dir)
        self.processed_dir = os.path.join(self.inbox_dir, "processed")
        self.broker = broker

    def pending_files(self):
        if not os.path.isdir(self.inbox_dir):
            return []
        return sorted(n for n in os.listdir(self.inbox_dir)
                      if n.endswith(".json") and not n.startswith(".")
                      and os.path.isfile(os.path.join(self.inbox_dir, n))
                      and not os.path.islink(os.path.join(self.inbox_dir, n)))

    def _read(self, path):
        if os.path.getsize(path) > MAX_BYTES:
            return None, "decision file too large"
        try:
            with open(path, "r", encoding="utf-8") as fh:
                obj = json.load(fh)
        except (OSError, ValueError, UnicodeDecodeError):
            return None, "decision file is not valid JSON"
        if not isinstance(obj, dict):
            return None, "decision must be a JSON object"
        return obj, None

    def process(self, dry_run=False):
        names = self.pending_files()
        if dry_run:
            return [envelope("approval.inbox", changed=False, data={"files": names})] if names else []
        results = []
        for name in names:
            path = os.path.join(self.inbox_dir, name)
            decision, error = self._read(path)
            if error:
                result = envelope("approval.inbox", ok=False, checks=[check("decision_readable", False, error)])
            else:
                result = self.broker.resolve(decision)
            result.setdefault("data", {})
            result["source_file"] = name
            os.makedirs(self.processed_dir, exist_ok=True)
            atomic_write_json(os.path.join(self.processed_dir, name + ".result.json"), result)
            os.replace(path, os.path.join(self.processed_dir, name))
            results.append(result)
        return results
