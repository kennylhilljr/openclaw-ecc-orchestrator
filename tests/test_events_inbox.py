import fcntl
import json
import os
import tempfile
import unittest
from unittest import mock

from openclaw_ecc_orchestrator.plugin.approvals import ApprovalBroker
from openclaw_ecc_orchestrator.plugin.inbox import DecisionInbox


class InboxTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = self._tmp.name
        self.broker = ApprovalBroker(os.path.join(d, "approvals.json"), clock=lambda: 1_700_000_000.0)
        self.inbox_dir = os.path.join(d, "inbox")
        self.inbox = DecisionInbox(self.inbox_dir, self.broker)
        req = self.broker.request(run_id="r1", unit_id="u1", action="merge", plan_sha256="e" * 64,
                                  session_id="s1")["data"]
        self.decision = {k: req[k] for k in ("request_id", "run_id", "unit_id", "action", "plan_sha256", "session_id")}
        self.decision.update(decision="approved", decided_by="op")

    def tearDown(self):
        self._tmp.cleanup()

    def drop(self, name, payload):
        os.makedirs(self.inbox_dir, exist_ok=True)
        with open(os.path.join(self.inbox_dir, name), "w") as fh:
            fh.write(payload if isinstance(payload, str) else json.dumps(payload))

    def test_valid_then_replay(self):
        self.drop("001.json", self.decision)
        self.drop("002.json", self.decision)
        results = self.inbox.process()
        self.assertEqual([r["ok"] for r in results], [True, False])
        self.assertEqual(os.listdir(self.inbox_dir), ["processed"])
        processed = sorted(os.listdir(os.path.join(self.inbox_dir, "processed")))
        self.assertIn("001.json", processed)
        self.assertIn("001.json.result.json", processed)
        with open(os.path.join(self.inbox_dir, "processed", "002.json.result.json")) as fh:
            self.assertFalse(json.load(fh)["ok"])
        self.assertEqual(self.inbox.process(), [])

    def test_malformed_and_oversized(self):
        self.drop("a.json", "{nope")
        self.drop("b.json", json.dumps(dict(self.decision, pad="x" * 70_000)))
        self.drop("ignored.txt", "not a decision")
        results = self.inbox.process()
        self.assertEqual([r["ok"] for r in results], [False, False])
        self.assertEqual(self.broker.get(self.decision["request_id"])["status"], "pending")
        self.assertTrue(os.path.exists(os.path.join(self.inbox_dir, "ignored.txt")))

    def test_dry_run(self):
        self.drop("001.json", self.decision)
        results = self.inbox.process(dry_run=True)
        self.assertEqual(results[0]["data"]["files"], ["001.json"])
        self.assertTrue(os.path.exists(os.path.join(self.inbox_dir, "001.json")))
        self.assertEqual(self.broker.get(self.decision["request_id"])["status"], "pending")

    def test_file_claimed_by_another_consumer_is_skipped(self):
        self.drop("001.json", self.decision)
        fd = os.open(os.path.join(self.inbox_dir, "001.json"), os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.assertEqual(self.inbox.process(), [])
            self.assertIsNone(self.inbox.process_one("001.json", wait=0.2))
            self.assertEqual(self.broker.get(self.decision["request_id"])["status"], "pending")
        finally:
            os.close(fd)
        self.assertEqual([r["ok"] for r in self.inbox.process()], [True])

    def test_file_vanishing_mid_scan_is_skipped(self):
        self.drop("001.json", self.decision)
        with mock.patch.object(DecisionInbox, "pending_files", return_value=["000-gone.json", "001.json"]):
            results = self.inbox.process()
        self.assertEqual([r["source_file"] for r in results], ["001.json"])
        self.assertIsNone(self.inbox.process_one("000-gone.json"))
        self.assertIsNone(self.inbox.preview_one("000-gone.json"))

    def test_symlink_refused_into_processed(self):
        target = os.path.join(self._tmp.name, "target.json")
        with open(target, "w") as fh:
            json.dump(self.decision, fh)
        os.makedirs(self.inbox_dir, exist_ok=True)
        os.symlink(target, os.path.join(self.inbox_dir, "001.json"))
        results = self.inbox.process()
        self.assertEqual([r["ok"] for r in results], [False])
        self.assertTrue(os.path.islink(os.path.join(self.inbox_dir, "processed", "001.json")))
        self.assertEqual(self.broker.get(self.decision["request_id"])["status"], "pending")

    def test_preview_matches_resolution_and_writes_nothing(self):
        self.drop("001.json", self.decision)
        self.drop("002.json", dict(self.decision, session_id="other"))
        before = open(self.broker.state_path).read()
        previews = self.inbox.preview()
        self.assertEqual([p["ok"] for p in previews], [True, False])
        self.assertEqual(open(self.broker.state_path).read(), before)
        self.assertEqual(sorted(os.listdir(self.inbox_dir)), ["001.json", "002.json"])
        self.assertEqual([r["ok"] for r in self.inbox.process()], [True, False])


if __name__ == "__main__":
    unittest.main()
