"""Regression tests: the conductor keeps its lease alive while a runner works,
recovers from transient failures, and surrenders a unit when the lease is lost."""

import threading
import time
import unittest
from unittest import mock

try:
    from ._integration import IntegrationBase, exists_cmd
    from ._units import work_unit
except ImportError:  # discovered as a top level module
    from _integration import IntegrationBase, exists_cmd
    from _units import work_unit


def plan(*units):
    return {"units": list(units)}


class LeaseDuringWaitTests(IntegrationBase):
    def start(self, c, argv):
        unit = work_unit("u1", files=["a.txt"], commands=[exists_cmd("a.txt")])
        self.ok(c.create_run(plan(unit), run_id="r1"))
        self.ok(c.assign_unit("r1", "u1"))
        return self.ok(c.start_unit("r1", "u1", argv))

    def test_lease_renewed_while_runner_works(self):
        c, m, _ = self.conductor("c1", lease_ttl=60, lease_renew_interval=0.02)
        self.start(c, self.argv("sleep:0.6"))
        stop = threading.Event()

        def tick():  # 20 fake seconds per 50 ms: 240 s over the run, four lease lifetimes
            while not stop.is_set():
                self.clock.t += 20
                time.sleep(0.05)

        ticker = threading.Thread(target=tick)
        ticker.start()
        try:
            res = c.wait_unit("r1", "u1")
        finally:
            stop.set()
            ticker.join()
        # The runner wrote nothing, so the unit fails on its (missing) handoff,
        # but every state change went through: the lease never lapsed.
        self.assertNotIn("lease_held", self.failed_checks(res))
        u = m.load("r1")["units"]["u1"]
        self.assertEqual(u["state"], "failed")
        self.assertEqual(m.load("r1")["lease"]["conductor_id"], "c1")

    def test_expired_but_unclaimed_lease_is_reacquired(self):
        c, m, _ = self.conductor("c1", lease_ttl=60)
        self.start(c, self.argv("write", "a.txt=x"))
        self.clock.t += 301
        self.ok(c.wait_unit("r1", "u1"))
        self.assertEqual(m.load("r1")["units"]["u1"]["state"], "reviewing")

    def test_lost_lease_surrenders_unit_with_pid(self):
        c, m, _ = self.conductor("c1", lease_ttl=60)
        started = self.start(c, self.argv("write", "a.txt=x"))
        self.clock.t += 61
        other = self.conductor("c2")[1]
        self.ok(other.acquire_lease("r1", "c2"))
        res = c.wait_unit("r1", "u1")
        self.assertFalse(res["ok"])
        self.assertIn("lease_held", self.failed_checks(res))
        u = m.load("r1")["units"]["u1"]
        self.assertEqual((u["state"], u["last_reason"]), ("needs_user", "lease_lost"))
        self.assertEqual(u["process"]["pid"], started["data"]["pid"])
        kinds = [a["kind"] for a in res["required_user_actions"]]
        self.assertIn("verify_worker_process", kinds)

    def test_wait_retryable_after_transient_failure(self):
        c, m, _ = self.conductor("c1")
        self.start(c, self.argv("write", "a.txt=x"))
        real = m.record_usage
        calls = {"n": 0}

        def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk hiccup")
            return real(*a, **kw)

        with mock.patch.object(m, "record_usage", side_effect=flaky):
            first = c.wait_unit("r1", "u1")
            self.assertFalse(first["ok"])
            self.assertIn("transient_failure", self.failed_checks(first))
            self.assertEqual(m.load("r1")["units"]["u1"]["state"], "running")
            self.ok(c.wait_unit("r1", "u1"))
        self.assertEqual(m.load("r1")["units"]["u1"]["state"], "reviewing")

    def test_wait_timeout_keeps_handle(self):
        c, m, _ = self.conductor("c1", lease_renew_interval=0.02)
        self.start(c, self.argv("sleep:0.5"))
        res = c.wait_unit("r1", "u1", timeout=0.05)
        self.assertFalse(res["ok"])
        self.assertIn("runner_finished", self.failed_checks(res))
        self.assertEqual(m.load("r1")["units"]["u1"]["state"], "running")
        res = c.wait_unit("r1", "u1")
        self.assertNotIn("unit_has_process", self.failed_checks(res))


if __name__ == "__main__":
    unittest.main()
