import os
import tempfile
import unittest

from openclaw_ecc_orchestrator.plugin.approvals import ApprovalBroker, plan_digest
from openclaw_ecc_orchestrator.plugin.events import MemoryEventSink

PLAN = plan_digest({"action": "merge", "unit": "u1", "branch": "ecc/r1/u1"})


class FakeClock:
    def __init__(self):
        self.t = 1_700_000_000.0

    def __call__(self):
        return self.t


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.sink = MemoryEventSink()
        n = iter(range(1, 1000))
        self.broker = ApprovalBroker(os.path.join(self._tmp.name, "approvals.json"), clock=self.clock,
                                     id_gen=lambda: f"req{next(n)}", emitter=self.sink)
        res = self.broker.request(run_id="r1", unit_id="u1", action="merge", plan_sha256=PLAN, session_id="sess-A",
                                  ttl_s=600, summary="merge u1")
        self.assertTrue(res["ok"], res)
        self.req = res["data"]

    def tearDown(self):
        self._tmp.cleanup()

    def decision(self, **over):
        d = {"request_id": self.req["request_id"], "run_id": "r1", "unit_id": "u1", "action": "merge",
             "plan_sha256": PLAN, "session_id": "sess-A", "decision": "approved", "decided_by": "operator"}
        d.update(over)
        return d

    def test_request_emits_event_and_persists(self):
        self.assertEqual(self.sink.events[-1]["type"], "approval.requested")
        self.assertEqual(self.sink.events[-1]["data"]["request_id"], self.req["request_id"])
        again = ApprovalBroker(self.broker.state_path, clock=self.clock)
        self.assertEqual([p["request_id"] for p in again.pending()], [self.req["request_id"]])

    def test_valid_decision_then_replay_rejected(self):
        res = self.broker.resolve(self.decision())
        self.assertTrue(res["ok"], res)
        rec = res["data"]
        self.assertEqual((rec["run_id"], rec["unit_id"], rec["action"], rec["plan_sha256"], rec["decision"]),
                         ("r1", "u1", "merge", PLAN, "approved"))
        self.assertEqual(self.sink.events[-1]["type"], "approval.resolved")
        replay = self.broker.resolve(self.decision())
        self.assertFalse(replay["ok"])
        self.assertIn("request_pending", [c["name"] for c in replay["checks"] if not c["ok"]])
        fresh = ApprovalBroker(self.broker.state_path, clock=self.clock)
        self.assertFalse(fresh.resolve(self.decision())["ok"])

    def test_mismatches_rejected_without_consuming(self):
        for field, value in [("run_id", "r2"), ("unit_id", "u2"), ("action", "cleanup"),
                             ("plan_sha256", "0" * 64), ("session_id", "sess-B")]:
            res = self.broker.resolve(self.decision(**{field: value}))
            self.assertFalse(res["ok"], field)
            self.assertIn("binding_matches", [c["name"] for c in res["checks"] if not c["ok"]], field)
        self.assertTrue(self.broker.resolve(self.decision())["ok"])
        audit = self.broker.state()["rejected_attempts"]
        self.assertEqual(len(audit), 5)

    def test_unknown_request_and_bad_decision(self):
        self.assertFalse(self.broker.resolve(self.decision(request_id="nope"))["ok"])
        self.assertFalse(self.broker.resolve(self.decision(decision="maybe"))["ok"])
        self.assertFalse(self.broker.resolve(self.decision(decided_by=""))["ok"])
        self.assertFalse(self.broker.resolve("approved")["ok"])

    def test_expired_rejected(self):
        self.clock.t += 601
        res = self.broker.resolve(self.decision())
        self.assertFalse(res["ok"])
        self.assertIn("not_expired", [c["name"] for c in res["checks"] if not c["ok"]])
        self.assertEqual(self.broker.pending(), [])

    def test_request_from_another_run_cannot_approve_this_one(self):
        other = self.broker.request(run_id="r9", unit_id="u1", action="merge", plan_sha256=PLAN,
                                    session_id="sess-A")["data"]
        res = self.broker.resolve(self.decision(request_id=other["request_id"]))
        self.assertFalse(res["ok"])

    def test_rejection_decision_is_recorded(self):
        res = self.broker.resolve(self.decision(decision="rejected"))
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["decision"], "rejected")

    def test_dry_run_request_does_not_persist(self):
        before = len(self.broker.pending())
        res = self.broker.request(run_id="r1", unit_id="u2", action="merge", plan_sha256=PLAN, session_id="s",
                                  dry_run=True)
        self.assertTrue(res["ok"])
        self.assertFalse(res["changed"])
        self.assertEqual(len(self.broker.pending()), before)

    def test_plan_digest_stable(self):
        self.assertEqual(plan_digest({"b": 1, "a": 2}), plan_digest({"a": 2, "b": 1}))


if __name__ == "__main__":
    unittest.main()
