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
        handle = c._handles[("r1", "u1")]
        stop = threading.Event()

        # 20 fake seconds per 50 ms: 240 s over the run, four lease lifetimes.
        # Time races only while the runner works. Completion makes several
        # store writes without renewing; at this speed a loaded machine let
        # the lease lapse between them, which says nothing about renewal.
        def tick():
            while not stop.is_set() and handle.result is None:
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

    def test_lease_lost_during_completion_keeps_unit_retryable(self):
        """A failed terminal transition must not strand the unit in `running`
        with its process handle dropped: the wait reports the lost lease and
        a retry finishes the job."""
        c, m, _ = self.conductor("c1", lease_ttl=60)
        self.start(c, self.argv("nohandoff", "a.txt=x"))
        real = m.transition
        calls = {"n": 0}

        def lapse_once(run_id, cid, unit_id, target, **kw):
            if target == "failed" and calls["n"] == 0:
                calls["n"] += 1
                self.clock.t += 61  # the lease lapses mid-completion
            return real(run_id, cid, unit_id, target, **kw)

        with mock.patch.object(m, "transition", side_effect=lapse_once):
            first = c.wait_unit("r1", "u1")
            self.assertFalse(first["ok"])
            self.assertIn("lease_held", self.failed_checks(first))
            self.assertTrue(first["changed"])  # the exit and usage were recorded
            self.assertEqual(m.load("r1")["units"]["u1"]["state"], "running")
            self.assertIn(("r1", "u1"), c._handles)
            second = c.wait_unit("r1", "u1")
        self.assertIn("handoff_valid", self.failed_checks(second))
        self.assertEqual(m.load("r1")["units"]["u1"]["state"], "failed")
        self.assertNotIn(("r1", "u1"), c._handles)

    def test_store_error_after_terminal_write_finishes_on_retry(self):
        """The failed state was written but the call raised: the retry still
        records the failure evidence instead of dropping it."""
        from openclaw_ecc_orchestrator.runs.store import StoreError

        c, m, _ = self.conductor("c1", lease_ttl=60)
        self.start(c, self.argv("nohandoff", "a.txt=x"))
        real = m.transition
        calls = {"n": 0}

        def write_then_raise(run_id, cid, unit_id, target, **kw):
            res = real(run_id, cid, unit_id, target, **kw)
            if target == "failed" and calls["n"] == 0:
                calls["n"] += 1
                raise StoreError("event appended, snapshot write failed")
            return res

        with mock.patch.object(m, "transition", side_effect=write_then_raise):
            first = c.wait_unit("r1", "u1")
            self.assertIn("transient_failure", self.failed_checks(first))
            self.assertEqual(m.load("r1")["units"]["u1"]["state"], "failed")
            self.assertNotIn("last_failure", m.load("r1")["units"]["u1"]["annotations"])
            second = c.wait_unit("r1", "u1")
        self.assertIn("handoff_valid", self.failed_checks(second))
        unit = m.load("r1")["units"]["u1"]
        self.assertEqual(unit["annotations"]["last_failure"]["reason"], "handoff_missing")
        self.assertNotIn(("r1", "u1"), c._handles)

    def test_refused_failure_evidence_is_retried(self):
        c, m, _ = self.conductor("c1", lease_ttl=60)
        self.start(c, self.argv("nohandoff", "a.txt=x"))
        real = m.annotate_unit
        calls = {"n": 0}

        def refuse_once(run_id, cid, unit_id, key, value):
            if key == "last_failure" and calls["n"] == 0:
                calls["n"] += 1
                return {"ok": False, "checks": [{"name": "lease_held", "ok": False, "detail": "x"}]}
            return real(run_id, cid, unit_id, key, value)

        with mock.patch.object(m, "annotate_unit", side_effect=refuse_once):
            first = c.wait_unit("r1", "u1")
            self.assertIn("failure_recorded", self.failed_checks(first))
            self.assertEqual(m.load("r1")["units"]["u1"]["state"], "failed")
            self.assertIn(("r1", "u1"), c._handles)
            second = c.wait_unit("r1", "u1")
        self.assertIn("handoff_valid", self.failed_checks(second))
        unit = m.load("r1")["units"]["u1"]
        self.assertEqual(unit["annotations"]["last_failure"]["reason"], "handoff_missing")
        self.assertNotIn(("r1", "u1"), c._handles)

    def test_usage_written_then_raised_is_not_charged_twice(self):
        from openclaw_ecc_orchestrator.runs.store import StoreError

        c, m, _ = self.conductor("c1")
        self.start(c, self.argv("write", "a.txt=x"))
        real = m.record_usage
        calls = {"n": 0}

        def write_then_raise(*a, **kw):
            res = real(*a, **kw)
            calls["n"] += 1
            if calls["n"] == 1:
                raise StoreError("event appended, snapshot write failed")
            return res

        with mock.patch.object(m, "record_usage", side_effect=write_then_raise):
            first = c.wait_unit("r1", "u1")
            self.assertIn("transient_failure", self.failed_checks(first))
            charged = m.load("r1")["units"]["u1"]["cost_usd"]
            self.assertGreater(charged, 0)
            self.ok(c.wait_unit("r1", "u1"))
        self.assertEqual(calls["n"], 1)
        self.assertEqual(m.load("r1")["units"]["u1"]["cost_usd"], charged)

    def test_refused_verifying_transition_keeps_handle(self):
        c, m, _ = self.conductor("c1", lease_ttl=60)
        self.start(c, self.argv("write", "a.txt=x"))
        real = m.transition
        calls = {"n": 0}

        def lapse_once(run_id, cid, unit_id, target, **kw):
            if target == "verifying" and calls["n"] == 0:
                calls["n"] += 1
                self.clock.t += 61
            return real(run_id, cid, unit_id, target, **kw)

        with mock.patch.object(m, "transition", side_effect=lapse_once):
            first = c.wait_unit("r1", "u1")
            self.assertFalse(first["ok"])
            self.assertEqual(m.load("r1")["units"]["u1"]["state"], "running")
            self.assertIn(("r1", "u1"), c._handles)
            self.ok(c.wait_unit("r1", "u1"))
        self.assertEqual(m.load("r1")["units"]["u1"]["state"], "reviewing")

    def test_expired_but_unclaimed_lease_is_reacquired(self):
        c, m, _ = self.conductor("c1", lease_ttl=60)
        self.start(c, self.argv("write", "a.txt=x"))
        self.clock.t += 301
        self.ok(c.wait_unit("r1", "u1"))
        self.assertEqual(m.load("r1")["units"]["u1"]["state"], "reviewing")

    def test_lost_lease_surrenders_unit_with_pid(self):
        c, m, _ = self.conductor("c1", lease_ttl=60)
        started = self.start(c, self.argv("write", "a.txt=x"))
        # Surrender leaves the worker running for an operator to check. Stop it
        # before tearDown, as an operator would, so it cannot still be writing
        # into the temp dir while tearDown removes it (the source of a flake).
        handle = c._handles[("r1", "u1")]
        try:
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
        finally:
            handle.cancel("test cleanup")
            handle.wait(timeout=30)

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
