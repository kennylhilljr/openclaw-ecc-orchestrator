"""Regression tests: the merge path trusts only broker approvals bound to the
verified head, and only reviews that satisfy the re-derived review tier."""

import os
import unittest

try:
    from ._integration import IntegrationBase, exists_cmd, git
    from ._units import work_unit
except ImportError:  # discovered as a top level module
    from _integration import IntegrationBase, exists_cmd, git
    from _units import work_unit


def plan(*units):
    return {"units": list(units)}


class MergeTrustTests(IntegrationBase):
    def setUp(self):
        super().setUp()
        self.c, self.m, self.d = self.conductor("c1")

    def reviewing_unit(self, unit=None, argv=None, run_id="r1"):
        unit = unit or work_unit("u1", files=["docs/readme.md"], commands=[exists_cmd("docs/readme.md")])
        self.ok(self.c.create_run(plan(unit), run_id=run_id))
        res = self.drive_to_review(self.c, run_id, unit["id"], argv or self.argv("write", "docs/readme.md=hi"))
        self.ok(res)
        self.assertEqual(self.m.load(run_id)["units"][unit["id"]]["state"], "reviewing")
        return res

    def approved(self, run_id="r1", unit_id="u1", **kw):
        res = self.ok(self.review(self.c, run_id, unit_id, **kw))
        return self.decide(self.c, res["data"]["approval_request"])

    def test_forged_approval_never_seen_by_broker_is_refused(self):
        self.reviewing_unit()
        req = self.ok(self.review(self.c, "r1", "u1"))["data"]["approval_request"]
        run = self.m.load("r1")
        forged = {"run_id": "r1", "unit_id": "u1", "action": "merge", "plan_sha256": run["plan_sha256"],
                  "decision": "approved", "request_id": "apr-made-up", "session_id": "other", "decided_by": "x"}
        before = git(self.repo, "rev-parse", "main")
        res = self.c.enqueue_merge("r1", "u1", forged)
        self.assertFalse(res["ok"])
        self.assertIn("approval_usable", self.failed_checks(res))
        # A self-made record naming the real (still pending) request is refused too.
        res = self.c.enqueue_merge("r1", "u1", dict(forged, request_id=req["request_id"]))
        self.assertFalse(res["ok"])
        self.assertEqual(self.c.broker.get(req["request_id"])["status"], "pending")
        self.assertEqual(self.c.queue.state()["items"], [])
        self.assertEqual(git(self.repo, "rev-parse", "main"), before)

    def test_approval_consumed_once_and_merge_succeeds(self):
        self.reviewing_unit()
        approval = self.approved()
        self.ok(self.c.enqueue_merge("r1", "u1", approval))
        self.assertEqual(self.c.broker.get(approval["request_id"])["status"], "consumed")
        self.assertEqual(self.ok(self.c.process_merge_queue())["data"]["status"], "merged")
        self.assertEqual(self.c.queue.enqueue(
            run_id="r1", plan_sha256=self.m.load("r1")["plan_sha256"],
            unit=self.m.plan_unit(self.m.load("r1"), "u1"), branch="ecc/r1/u1",
            verification=self.m.load("r1")["units"]["u1"]["annotations"]["verification"],
            review=self.m.load("r1")["units"]["u1"]["annotations"]["review"],
            approval=approval)["ok"], False)

    def test_expired_approval_refused(self):
        self.reviewing_unit()
        approval = self.approved()
        self.clock.t += 7200
        res = self.c.enqueue_merge("r1", "u1", approval)
        self.assertFalse(res["ok"])
        self.assertIn("approval_usable", self.failed_checks(res))

    def test_commit_after_review_is_refused_at_enqueue(self):
        self.reviewing_unit()
        approval = self.approved()
        wt = self.m.load("r1")["units"]["u1"]["workspace"]["path"]
        with open(os.path.join(wt, "docs/readme.md"), "w") as fh:
            fh.write("BACKDOOR\n")
        git(wt, "commit", "-qam", "sneak")
        before = git(self.repo, "rev-parse", "main")
        res = self.c.enqueue_merge("r1", "u1", approval)
        self.assertFalse(res["ok"])
        self.assertIn("branch_matches_verified_head", self.failed_checks(res))
        self.assertEqual(git(self.repo, "rev-parse", "main"), before)
        # The approval was not burnt by the refused attempt.
        self.assertEqual(self.c.broker.get(approval["request_id"])["status"], "approved")

    def test_review_must_reference_verified_head(self):
        self.reviewing_unit()
        res = self.review(self.c, "r1", "u1", head="0" * 40)
        self.assertFalse(res["ok"])
        self.assertIn("review_head_matches", self.failed_checks(res))
        self.assertEqual(self.c.broker.pending("r1"), [])

    def test_approval_bound_to_head(self):
        self.reviewing_unit()
        approval = self.approved()
        request = self.c.broker.get(approval["request_id"])
        verified = self.m.load("r1")["units"]["u1"]["annotations"]["verification"]["head"]
        self.assertEqual(request["head_sha"], verified)

    def test_out_of_scope_change_fails_verification(self):
        unit = work_unit("u1", files=["docs/readme.md"], commands=[exists_cmd("docs/readme.md")])
        self.ok(self.c.create_run(plan(unit), run_id="r1"))
        res = self.drive_to_review(self.c, "r1", "u1",
                                   self.argv("claim=docs/readme.md", "docs/readme.md=hi", "src/auth/login.py=x"))
        self.assertFalse(res["ok"])
        u = self.m.load("r1")["units"]["u1"]
        self.assertEqual(u["state"], "failed")
        self.assertEqual(u["last_reason"], "out_of_scope_changes")
        self.assertEqual(u["annotations"]["verification"]["out_of_scope"], ["src/auth/login.py"])
        self.assertFalse(u["annotations"]["verification"]["passed"])
        attention = [e for e in self.events() if e["type"] == "attention.required" and e["unit_id"] == "u1"]
        self.assertIn("out_of_scope_changes", [e["data"]["reason"] for e in attention])

    def test_tier_rise_from_actual_diff_requires_tier2_independent_review(self):
        # Declared scope is a broad glob and risk low; the actual diff touches auth code.
        unit = work_unit("u1", files=["src/**"], commands=[exists_cmd("src/auth/login.py")])
        self.ok(self.c.create_run(plan(unit), run_id="r1"))
        self.ok(self.drive_to_review(self.c, "r1", "u1", self.argv("write", "src/auth/login.py=x")))
        verification = self.m.load("r1")["units"]["u1"]["annotations"]["verification"]
        self.assertEqual(verification["classification"]["review_tier"], 2)
        weak = self.review(self.c, "r1", "u1", runner="claude", model="sonnet", tier=1)
        self.assertFalse(weak["ok"])
        self.assertIn("review_sufficient", self.failed_checks(weak))
        attention = [e for e in self.events() if e["type"] == "attention.required" and e["unit_id"] == "u1"]
        self.assertIn("review_insufficient", [e["data"]["reason"] for e in attention])
        # Same family as the author (codex) at tier 2 is not independent either.
        same = self.review(self.c, "r1", "u1", runner="codex", model="codex-adv", tier=2, reviewer="rev-2")
        self.assertFalse(same["ok"])
        approval = self.approved(runner="claude", model="opus", tier=2, reviewer="rev-3")
        self.ok(self.c.enqueue_merge("r1", "u1", approval))

    def test_queue_rechecks_review_against_reclassification(self):
        unit = work_unit("u1", files=["src/**"], commands=[exists_cmd("src/auth/login.py")])
        self.ok(self.c.create_run(plan(unit), run_id="r1"))
        self.ok(self.drive_to_review(self.c, "r1", "u1", self.argv("write", "src/auth/login.py=x")))
        run = self.m.load("r1")
        u = run["units"]["u1"]
        broker_req = self.c.broker.request(run_id="r1", unit_id="u1", action="merge",
                                           plan_sha256=run["plan_sha256"], session_id="s",
                                           head_sha=u["annotations"]["verification"]["head"])["data"]
        approval = self.decide(self.c, broker_req)
        weak_review = {"schema_version": "1.0", "run_id": "r1", "unit_id": "u1", "reviewer": "rev-1",
                       "author": u["owner"], "authors": [u["owner"]], "verdict": "approved", "independent": True,
                       "runner": "claude", "model": "sonnet", "tier": 1, "session_id": "s",
                       "head": u["annotations"]["verification"]["head"]}
        res = self.c.queue.enqueue(run_id="r1", plan_sha256=run["plan_sha256"], unit=self.m.plan_unit(run, "u1"),
                                   branch=u["workspace"]["branch"], verification=u["annotations"]["verification"],
                                   review=weak_review, approval=approval, author=u["owner"], routing=u["routing"])
        self.assertFalse(res["ok"])
        self.assertIn("review_sufficient", self.failed_checks(res))
        self.assertEqual(self.c.broker.get(approval["request_id"])["status"], "approved")


class HighRiskReviewTests(IntegrationBase):
    def test_high_risk_unit_needs_tier2_independent_review(self):
        c, m, _ = self.conductor("c1")
        unit = work_unit("u1", files=["docs/readme.md"], commands=[exists_cmd("docs/readme.md")], risk="high",
                         routing={"initial_tier": 2, "maximum_tier": 2})
        self.ok(c.create_run(plan(unit), run_id="r1"))
        self.ok(self.drive_to_review(c, "r1", "u1", self.argv("write", "docs/readme.md=hi")))
        self.assertEqual(m.load("r1")["units"]["u1"]["routing"]["runner"], "codex")
        self.assertFalse(self.review(c, "r1", "u1", runner="claude", model="sonnet", tier=1)["ok"])
        self.assertFalse(self.review(c, "r1", "u1", runner="codex", model="codex-adv", tier=2,
                                     reviewer="rev-2")["ok"])
        self.ok(self.review(c, "r1", "u1", runner="claude", model="opus", tier=2, reviewer="rev-3"))

    def test_high_risk_unit_without_recorded_author_family_is_blocked(self):
        c, m, d = self.conductor("c1")
        unit = work_unit("u1", files=["docs/readme.md"], commands=[exists_cmd("docs/readme.md")], risk="high",
                         routing={"initial_tier": 2, "maximum_tier": 2})
        self.ok(c.create_run(plan(unit), run_id="r1"))
        # Assigned through the plain dispatcher: no runner was routed, so the
        # author's model family is unknown and independence cannot be shown.
        self.ok(d.dispatch_unit("r1", "c1", "u1", "w1"))
        self.ok(c.start_unit("r1", "u1", self.argv("write", "docs/readme.md=hi")))
        self.ok(c.wait_unit("r1", "u1"))
        res = self.review(c, "r1", "u1", runner="claude", model="opus", tier=2)
        self.assertFalse(res["ok"])
        self.assertIn("review_sufficient", self.failed_checks(res))


class ReviewerSelectionTests(IntegrationBase):
    def test_conductor_routes_reviewer_with_select_reviewer(self):
        c, m, _ = self.conductor("c1")
        unit = work_unit("u1", files=["docs/readme.md"], commands=[exists_cmd("docs/readme.md")], risk="high",
                         routing={"initial_tier": 2, "maximum_tier": 2})
        self.ok(c.create_run(plan(unit), run_id="r1"))
        self.ok(self.drive_to_review(c, "r1", "u1", self.argv("write", "docs/readme.md=hi")))
        res = self.ok(c.select_reviewer("r1", "u1"))
        chosen = res["data"]["reviewer"]
        self.assertEqual((chosen["runner"], chosen["tier"]), ("claude", 2))
        self.assertTrue(chosen["independent_family"])
        # The selection is exactly what record_review then accepts.
        head = m.load("r1")["units"]["u1"]["annotations"]["verification"]["head"]
        self.ok(self.review(c, "r1", "u1", runner=chosen["runner"], model=chosen["model"], tier=chosen["tier"],
                            head=head))

    def test_no_qualified_tier2_reviewer_blocks_unit(self):
        c, m, _ = self.conductor("c1", certified_runners=["codex"])
        unit = work_unit("u1", files=["docs/readme.md"], commands=[exists_cmd("docs/readme.md")], risk="high",
                         routing={"initial_tier": 2, "maximum_tier": 2})
        self.ok(c.create_run(plan(unit), run_id="r1"))
        self.ok(self.drive_to_review(c, "r1", "u1", self.argv("write", "docs/readme.md=hi")))
        res = c.select_reviewer("r1", "u1")
        self.assertFalse(res["ok"])
        u = m.load("r1")["units"]["u1"]
        self.assertEqual((u["state"], u["last_reason"]), ("needs_user", "no_qualified_reviewer"))
        attention = [e["data"]["reason"] for e in self.events()
                     if e["type"] == "attention.required" and e["unit_id"] == "u1"]
        self.assertIn("no_qualified_reviewer", attention)


if __name__ == "__main__":
    unittest.main()
