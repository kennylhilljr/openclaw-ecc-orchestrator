import unittest

from openclaw_ecc_orchestrator.plugin.summary import summarize_required_user_actions


def urec(state, reason=None, owner=None):
    return {"state": state, "last_reason": reason, "owner": owner}


class SummaryTests(unittest.TestCase):
    def test_summarizes_all_sources(self):
        run = {"run_id": "r1", "units": {
            "a": urec("needs_user", "budget_exhausted", "w1"),
            "b": urec("interrupted", "process_gone", "w2"),
            "c": urec("blocked", "conflict"),
            "d": urec("running", owner="w3"),
            "e": urec("merged"),
            "f": urec("failed", "gates_failed", "w4"),
        }}
        pending = [
            {"request_id": "q1", "run_id": "r1", "unit_id": "d", "action": "merge", "expires_at": 2e9,
             "summary": "merge d", "status": "pending"},
            {"request_id": "q2", "run_id": "r1", "unit_id": "d", "action": "merge", "expires_at": 1.0,
             "summary": "old", "status": "pending"},
            {"request_id": "q3", "run_id": "other", "unit_id": "x", "action": "merge", "expires_at": 2e9,
             "status": "pending"},
        ]
        queue = {"history": [{"run_id": "r1", "unit_id": "c", "status": "conflict", "conflicts": ["README.md"]}]}
        actions = summarize_required_user_actions(run, pending, queue_state=queue, now=1_700_000_000.0)
        kinds = [(a["kind"], a.get("unit_id")) for a in actions]
        self.assertIn(("approve", "d"), kinds)
        self.assertNotIn("q2", [a.get("request_id") for a in actions])
        self.assertNotIn("q3", [a.get("request_id") for a in actions])
        self.assertIn(("budget_exhausted", "a"), kinds)
        self.assertIn(("reassign_interrupted_unit", "b"), kinds)
        self.assertIn(("unblock_unit", "c"), kinds)
        self.assertIn(("resolve_conflict", "c"), kinds)
        self.assertIn(("retry_or_reassign_failed_unit", "f"), kinds)
        self.assertFalse([k for k in kinds if k[1] in ("d", "e") and k[0] != "approve"])
        self.assertEqual(kinds, sorted(kinds, key=lambda k: (k[1] or "", k[0])))
        for a in actions:
            self.assertEqual(a["run_id"], "r1")
            self.assertTrue(a["detail"] or a["kind"] == "approve")

    def test_possibly_running_worker_asks_for_process_check(self):
        # A unit surrendered after a lost lease, or whose worker's identity
        # could not be verified on resume, may still have a live process.
        for reason in ("lease_lost", "liveness_unverified"):
            with self.subTest(reason):
                unit = dict(urec("needs_user", reason, "w1"), process={"pid": 4242})
                actions = summarize_required_user_actions({"run_id": "r1", "units": {"a": unit}})
                self.assertEqual([a["kind"] for a in actions], ["verify_worker_process"])
                self.assertIn("4242", actions[0]["detail"])
        blocked = urec("needs_user", "shared_git_state_changed", "w1")
        actions = summarize_required_user_actions({"run_id": "r1", "units": {"a": blocked}})
        self.assertEqual([(a["kind"], a["detail"]) for a in actions],
                         [("resolve_needs_user", "shared_git_state_changed")])

    def test_empty(self):
        self.assertEqual(summarize_required_user_actions({"run_id": "r1", "units": {}}), [])


if __name__ == "__main__":
    unittest.main()
