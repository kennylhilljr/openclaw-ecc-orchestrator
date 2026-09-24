import json
import os
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
