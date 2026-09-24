import json
import os
import tempfile
import unittest
from unittest import mock

from openclaw_ecc_orchestrator.runs import fsutil
from openclaw_ecc_orchestrator.runs.fsutil import FileLock, LockTimeout, atomic_write_json
from openclaw_ecc_orchestrator.runs.store import RunStore, StoreError


class AtomicWriteTests(unittest.TestCase):
    def test_round_trip_and_no_temp_left(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "doc.json")
            atomic_write_json(path, {"schema_version": "1.0", "a": 1})
            with open(path) as fh:
                self.assertEqual(json.load(fh)["a"], 1)
            self.assertEqual(os.listdir(d), ["doc.json"])

    def test_failed_replace_keeps_old_content(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "doc.json")
            atomic_write_json(path, {"v": 1})
            with mock.patch.object(fsutil.os, "replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    atomic_write_json(path, {"v": 2})
            with open(path) as fh:
                self.assertEqual(json.load(fh)["v"], 1)
            self.assertEqual(os.listdir(d), ["doc.json"])


class LockTests(unittest.TestCase):
    def test_second_lock_times_out(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".lock")
            with FileLock(path, timeout=0):
                with self.assertRaises(LockTimeout):
                    with FileLock(path, timeout=0.05):
                        pass
            with FileLock(path, timeout=0):
                pass


class RunStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(self.tmp.name, clock=lambda: 1000.0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rejects_bad_run_ids(self):
        for bad in ["../x", "a/b", "", ".", "..", "a\x00b", "x" * 200]:
            with self.assertRaises(StoreError):
                self.store.run_dir(bad)

    def test_create_save_load(self):
        doc = {"run_id": "r1", "units": {}}
        self.store.create(doc)
        loaded = self.store.load("r1")
        self.assertEqual(loaded["schema_version"], "1.0")
        with self.assertRaises(StoreError):
            self.store.create(doc)
        loaded["x"] = 2
        self.store.save(loaded)
        self.assertEqual(self.store.load("r1")["x"], 2)
        self.assertEqual(self.store.list_runs(), ["r1"])

    def test_event_log_sequence_and_append_only(self):
        self.store.create({"run_id": "r1", "units": {}})
        e1 = self.store.append_event("r1", "a.b", {"n": 1})
        path = self.store.events_path("r1")
        with open(path, "rb") as fh:
            first = fh.read()
        e2 = self.store.append_event("r1", "a.b", {"n": 2})
        e3 = self.store.append_event("r1", "a.b", {"n": 3})
        self.assertEqual([e1["seq"], e2["seq"], e3["seq"]], [1, 2, 3])
        with open(path, "rb") as fh:
            self.assertTrue(fh.read().startswith(first))
        events = self.store.read_events("r1")
        self.assertEqual([e["seq"] for e in events], [1, 2, 3])
        self.assertTrue(all(e["schema_version"] == "1.0" for e in events))

    def test_truncated_tail_is_ignored_and_sequence_continues(self):
        self.store.create({"run_id": "r1", "units": {}})
        self.store.append_event("r1", "t", {})
        with open(self.store.events_path("r1"), "a") as fh:
            fh.write('{"seq": 2, "type": "t", "da')
        self.assertEqual(len(self.store.read_events("r1")), 1)
        e = self.store.append_event("r1", "t", {})
        self.assertEqual(e["seq"], 2)
        self.assertEqual([x["seq"] for x in self.store.read_events("r1")], [1, 2])


if __name__ == "__main__":
    unittest.main()
