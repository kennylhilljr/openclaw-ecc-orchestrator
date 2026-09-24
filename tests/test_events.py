import json
import os
import tempfile
import threading
import unittest

from openclaw_ecc_orchestrator.plugin.events import (
    EVENT_TYPES,
    EventError,
    JsonlEventSink,
    MemoryEventSink,
    make_event,
)


class EventTests(unittest.TestCase):
    def test_contract_types(self):
        self.assertEqual(set(EVENT_TYPES), {"run.created", "unit.state_changed", "unit.progress", "attention.required",
                                            "approval.requested", "approval.resolved", "merge.completed"})

    def test_make_event_shape(self):
        ev = make_event("unit.progress", "r1", unit_id="u1", data={"message": "halfway"}, seq=7,
                        clock=lambda: 1_700_000_000.0, id_gen=lambda: "e1")
        self.assertEqual(ev["schema_version"], "1.0")
        self.assertEqual(ev["event_version"], 1)
        self.assertEqual((ev["type"], ev["id"], ev["seq"], ev["run_id"], ev["unit_id"]), ("unit.progress", "e1", 7, "r1", "u1"))
        self.assertTrue(ev["emitted_at"].startswith("2023-11-14T"))

    def test_validation(self):
        with self.assertRaises(EventError):
            make_event("bogus.type", "r1", data={})
        with self.assertRaises(EventError):
            make_event("unit.state_changed", "r1", unit_id="u1", data={"from": "ready"})
        with self.assertRaises(EventError):
            make_event("unit.state_changed", "r1", data={"from": "a", "to": "b"})
        with self.assertRaises(EventError):
            make_event("run.created", "../evil", data={"plan_sha256": "x", "unit_ids": []})

    def test_no_secrets(self):
        ev = make_event("unit.progress", "r1", unit_id="u1",
                        data={"message": "using token=abcdef123456 and ghp_" + "z" * 36, "api_key": "k-123456",  # gitleaks:allow
                              "env": {"GITHUB_TOKEN": "raw-secret-value"}})
        blob = json.dumps(ev)
        for secret in ("abcdef123456", "z" * 36, "k-123456", "raw-secret-value"):
            self.assertNotIn(secret, blob)

    def test_long_strings_truncated(self):
        ev = make_event("unit.progress", "r1", unit_id="u1", data={"message": "x" * 50_000})
        self.assertLess(len(ev["data"]["message"]), 5000)


class SinkTests(unittest.TestCase):
    def test_jsonl_sink_monotonic_across_instances(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "events", "openclaw.jsonl")
            sink = JsonlEventSink(path, clock=lambda: 1_700_000_000.0)
            sink.emit("run.created", "r1", data={"plan_sha256": "a" * 64, "unit_ids": ["u1"]})
            sink.emit("unit.state_changed", "r1", unit_id="u1", data={"from": "pending", "to": "ready"})
            sink2 = JsonlEventSink(path)
            ev = sink2.emit("unit.progress", "r1", unit_id="u1", data={"message": "m"})
            self.assertEqual(ev["seq"], 3)
            with open(path) as fh:
                lines = [json.loads(line) for line in fh]
            self.assertEqual([e["seq"] for e in lines], [1, 2, 3])
            self.assertEqual(sink2.read(after_seq=1)[0]["type"], "unit.state_changed")

    def test_concurrent_emits_unique_seq(self):
        with tempfile.TemporaryDirectory() as d:
            sink = JsonlEventSink(os.path.join(d, "e.jsonl"))
            threads = [threading.Thread(target=lambda: [sink.emit("unit.progress", "r1", unit_id="u", data={"message": "m"}) for _ in range(20)])
                       for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            seqs = [e["seq"] for e in sink.read()]
            self.assertEqual(sorted(seqs), list(range(1, 81)))

    def test_memory_sink(self):
        sink = MemoryEventSink()
        sink.emit("attention.required", "r1", unit_id="u1", data={"reason": "needs_user"})
        self.assertEqual(sink.events[0]["type"], "attention.required")
        self.assertEqual(sink.events[0]["seq"], 1)


if __name__ == "__main__":
    unittest.main()
