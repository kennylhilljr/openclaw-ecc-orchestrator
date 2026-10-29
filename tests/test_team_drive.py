"""TEAM-DELIVERY-v2 sections 4.2 and 7: `ecc drive` (AT-TD-10, 11, 13, 15, 17, 21)."""

import json
import os
import unittest

try:
    from ._integration import PY, git
    from ._units import work_unit
    from .test_team_review import CATALOG, CERTIFIED, TeamBase, role_of
except ImportError:  # discovered as a top level module
    from _integration import PY, git
    from _units import work_unit
    from test_team_review import CATALOG, CERTIFIED, TeamBase, role_of

from openclaw_ecc_orchestrator.process.supervisor import Supervisor
from openclaw_ecc_orchestrator.team import config as TC
from openclaw_ecc_orchestrator.team import drive as D
from openclaw_ecc_orchestrator.team import personas as P
from openclaw_ecc_orchestrator.team.review import TeamStep

MARKERS = (("Before any unit of this milestone runs", "plan_check"), ("The milestone's units are merged", "drift"),
           ("You are the delivery lead", "delivery_lead"), ("You are a product owner", "product_owner"))


class DriveBase(TeamBase):
    def role_of_prompt(self, prompt):
        for marker, role in MARKERS:
            if marker in prompt:
                return role
        return role_of(prompt)

    def reply(self, role, head):
        if role in ("plan_check", "drift", "delivery_lead", "product_owner"):
            queue = self.replies.get(role) or ["ok"]
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if role == "product_owner":
                verdict = "reject" if item == "reject" else "accept"
                doc = {"verdict": verdict, "head": self.main_head(),
                       "criteria": [{"criterion": "all tests green", "met": verdict == "accept", "evidence": "x"}]}
                return f"{D.PRODUCT_MARK} {json.dumps(doc)}"
            verdict = item if item in P.REVIEW_VERDICTS else "approved"
            h = self.base if role == "plan_check" else self.main_head()
            return f"{P.REVIEW_MARK} {json.dumps({'verdict': verdict, 'head': h, 'findings': [{'severity': 'minor', 'what': role}]})}"
        return super().reply(role, self.current_head())

    def main_head(self):
        return git(self.repo, "rev-parse", "main")

    def current_head(self):
        run = self.m.load("r1")
        for u in run["units"].values():
            if u["state"] == "reviewing":
                return u["annotations"]["verification"]["head"]
        return self.main_head()

    def cli_override(self, role, runner, prompt):
        r = self.role_of_prompt(prompt)
        self.calls.append((r, runner))
        if (r, runner) in getattr(self, "silent", ()):
            return [PY, "-c", "import sys; sys.exit(3)"]
        text = self.reply(r, None)
        return [PY, "-c", f"print({json.dumps(text)})"]

    def http_post(self, url, headers, body, timeout):
        from openclaw_ecc_orchestrator.runners.discovery import HttpResponse
        prompt = (body.get("messages") or [{}])[0].get("content") or \
            ((body.get("contents") or [{}])[0].get("parts") or [{}])[0].get("text") or ""
        provider = "gemini" if "googleapis" in url else ("kimi" if "moonshot" in url else "groq")
        r = self.role_of_prompt(prompt)
        self.calls.append((r, provider))
        text = "" if (r, provider) in getattr(self, "silent", ()) else self.reply(r, None)
        data = {"candidates": [{"content": {"parts": [{"text": text}]}}]} if provider == "gemini" else \
            {"choices": [{"message": {"content": text}}]}
        return HttpResponse(200, json.dumps(data))

    def make(self, units, team=None, mode="write", max_cost=None):
        self.c, self.m, _ = self.conductor("c1", certified_runners=CERTIFIED, catalog=CATALOG, auto_retry=True,
                                           policy=self.policy)
        self.ok(self.c.create_run({"units": units}, run_id="r1"))
        self.base = self.m.load("r1")["metadata"]["base_commit"] if self.m.load("r1").get("metadata") else \
            git(self.repo, "rev-parse", "main")
        cfg = TC.load(team or {"merge_approval": "agents", "milestone": {"name": "M0"}})
        sup = Supervisor(base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.tmp, "USER": "t"},
                         grace=1.0)
        step = TeamStep(self.c, cfg, state_dir=self.state_root, worktree_root=os.path.join(self.tmp, "wt"),
                        logs_dir=os.path.join(self.state_root, "logs"), supervisor=sup, clock=self.clock,
                        cli_override=self.cli_override, http_post=self.http_post,
                        env={"GEMINI_API_KEY": "g", "MOONSHOT_API_KEY": "k", "GROQ_API_KEY": "q"})
        self.dispatched = []

        def dispatch(uid):
            self.dispatched.append(uid)
            files = next(u for u in units if u["id"] == uid)["scope"]["files"]
            res = self.c.assign_unit("r1", uid)
            if not res["ok"]:
                return res
            self.c.start_unit("r1", uid, self.argv(mode, *[f"{f}={uid}" for f in files]))
            return self.c.wait_unit("r1", uid)

        return D.Drive(self.c, step, cfg, "r1", dispatch=dispatch, clock=self.clock, max_cost_usd=max_cost,
                       reports_dir=os.path.join(self.state_root, "reports"))

    def units2(self):
        return [work_unit("alpha", files=["a.txt"], commands=[f"{PY} -c pass"], budget={"attempts": 2},
                          routing={"initial_tier": 0, "maximum_tier": 1}),
                work_unit("beta", deps=["alpha"], files=["b.txt"], commands=[f"{PY} -c pass"], budget={"attempts": 2},
                          routing={"initial_tier": 0, "maximum_tier": 1})]


class DriveTests(DriveBase):
    def test_at_td_10_drive_delivers_the_milestone(self):
        """AT-TD-10: dependents become ready after a merge and the drive runs to the end of the plan."""
        res = self.make(self.units2()).go()
        self.assertTrue(res["ok"], json.dumps(res, indent=1, default=str)[:3000])
        self.assertEqual({u["state"] for u in self.m.load("r1")["units"].values()}, {"merged"})
        self.assertEqual(git(self.repo, "show", "main:b.txt"), "beta")
        # Live finding (rmcp-m0f): a dependent unit branches from main after its dependency merged.
        beta_base = self.m.load("r1")["units"]["beta"]["workspace"]["base_commit"]
        self.assertEqual(git(self.repo, "show", f"{beta_base}:a.txt"), "alpha")
        self.assertTrue(os.path.isfile(res["data"]["report"]))
        roles = [r for r, _ in self.calls]
        self.assertEqual(roles[0], "plan_check")
        self.assertEqual(roles[-3:-2], ["delivery_lead"])

    def test_at_td_15_plan_check_blocks_before_any_unit(self):
        """AT-TD-15: a blocking plan finding stops the drive before any unit runs."""
        self.replies["plan_check"] = ["changes_requested"]
        drive = self.make(self.units2())
        res = drive.go()
        self.assertFalse(res["ok"])
        self.assertEqual(res["data"]["stop"], "plan_check_failed")
        self.assertEqual(self.dispatched, [])

    # Issue #8: check a plan without a run, once per plan sha.
    def plan_check(self, **kw):
        from openclaw_ecc_orchestrator.team import plan_check as PC
        run = self.m.load("r1")
        return PC.run_check(self.drive.step, self.repo, run["plan"]["units"], run["plan_sha256"], self.base, **kw)

    def architect_turns(self):
        return [r for r, _ in self.calls].count("plan_check")

    def test_plan_check_is_cached_by_plan_sha(self):
        self.replies["plan_check"] = ["changes_requested"]
        self.drive = self.make(self.units2())
        res, hit = self.plan_check()
        self.assertEqual((res["verdict"], hit), ("changes_requested", False))
        res, hit = self.plan_check()
        self.assertEqual((res["verdict"], hit), ("changes_requested", True))
        self.assertEqual(self.architect_turns(), 1)
        self.replies["plan_check"] = ["approved"]
        res, hit = self.plan_check(refresh=True)
        self.assertEqual((res["verdict"], hit, self.architect_turns()), ("approved", False, 2))

    def test_drive_skips_its_plan_check_when_plan_check_passed(self):
        self.drive = self.make(self.units2())
        self.assertEqual(self.plan_check()[0]["verdict"], "approved")
        self.assertIsNone(self.drive.milestone_start())
        self.assertEqual(self.architect_turns(), 1)
        self.assertTrue(self.drive.state["plan_check"]["cached"])

    def test_drive_checks_again_after_a_cached_failure(self):
        self.replies["plan_check"] = ["changes_requested"]
        self.drive = self.make(self.units2())
        self.plan_check()
        res = self.drive.milestone_start()
        self.assertEqual(res["data"]["stop"], "plan_check_failed")
        self.assertEqual(self.architect_turns(), 2)

    def test_at_td_11_and_21_product_pair_one_reject_rejects(self):
        """AT-TD-11, AT-TD-21: two product owners from different providers; one reject rejects."""
        self.replies["product_owner"] = ["accept", "reject"]
        res = self.make(self.units2()).go()
        self.assertFalse(res["ok"])
        self.assertEqual(res["data"]["stop"], "product_rejected")
        owners = [p for r, p in self.calls if r == "product_owner"]
        self.assertEqual(len(owners), 2)
        self.assertNotEqual(owners[0], owners[1])
        with open(res["data"]["report"]) as fh:
            self.assertIn("Verdict: **reject**", fh.read())

    def test_product_owner_pair_proceeds_on_one_provider(self):
        """Owner decision (rmcp-m0f): a silent provider is skipped, not a reject, and the pair may share
        a provider as two personas in separate sessions."""
        self.silent = {("product_owner", p) for p in ("claude", "codex", "gemini", "groq")}
        res = self.make(self.units2()).go()
        self.assertTrue(res["ok"], json.dumps(res, indent=1, default=str)[:3000])
        owners = [p for r, p in self.calls if r == "product_owner" and (r, p) not in self.silent]
        self.assertEqual(owners, ["kimi", "kimi"])

    def test_at_td_13_cost_ceiling_stops_cleanly(self):
        """AT-TD-13: the drive stops at its cost ceiling."""
        res = self.make(self.units2(), max_cost=0.005).go()
        self.assertFalse(res["ok"])
        self.assertEqual(res["data"]["stop"], "drive_cost_ceiling")

    def test_at_td_17_stuck_unit_is_triaged_before_the_owner(self):
        """AT-TD-17: a unit out of budget gets the delivery lead's diagnosis before the drive stops."""
        res = self.make(self.units2(), mode="fail").go()
        self.assertFalse(res["ok"])
        self.assertEqual(res["data"]["stop"], "needs_owner")
        self.assertIn("alpha", res["data"]["detail"])
        self.assertIsNotNone(res["data"]["diagnosis"])
        self.assertIn("delivery_lead", [r for r, _ in self.calls])


try:
    from . import test_cli_support as CS
except ImportError:  # discovered as a top level module
    import test_cli_support as CS


class DriveCliTests(CS.CliTestBase):
    def test_drive_and_team_review_plans_build(self):
        """The CLI wiring: drive and team-review plan from a created run."""
        self.create_run()
        code, doc = self.cli("drive", "--run-id", self.RUN_ID, "--dry-run")
        self.assertEqual(code, 0, json.dumps(doc)[:2000])
        plan = doc["data"]["plan"]
        self.assertEqual(plan["merge_approval"], "human")
        self.assertIn("ready", plan["unit_states"])
        code, doc = self.cli("team-review", "--run-id", self.RUN_ID, "--unit", "alpha", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("unit_reviewing", [c["name"] for c in doc["checks"] if not c["ok"]])

    def run_doc(self):
        with open(os.path.join(self.state, "runs", self.RUN_ID, "run.json")) as fh:
            return json.load(fh)

    def test_drive_refuses_a_run_whose_plan_file_changed_then_replace_keeps_the_id(self):
        """Issue #10: rm-m1e was driven again after plan v6 existed."""
        self.create_run()
        old = self.run_doc()
        self.assertEqual(old["metadata"]["plan_file"], self.plan_path)
        with open(self.plan_path, "w") as fh:  # formatting only: still the same plan
            json.dump(self.plan, fh, indent=2)
        self.assertEqual(self.cli("drive", "--run-id", self.RUN_ID, "--dry-run")[0], 0)
        self.plan["units"][0]["description"] = "revised after the plan check"
        self.write_json(os.path.basename(self.plan_path), self.plan)
        code, doc = self.cli("drive", "--run-id", self.RUN_ID, "--dry-run")
        self.assertNotEqual(code, 0)
        self.assertEqual([c["name"] for c in doc["checks"] if not c["ok"]], ["plan_file_unchanged"])
        replace = f"ecc create-run --run-id {self.RUN_ID} --plan {self.plan_path} --replace"
        self.assertEqual(doc["data"]["next_steps"], [replace])
        self.ok(self.reviewed("create-run", "--run-id", self.RUN_ID, "--plan", self.plan_path, "--replace"))
        new = self.run_doc()
        self.assertNotEqual(new["plan_sha256"], old["plan_sha256"])
        self.assertEqual(new["metadata"]["replaced"][0]["plan_sha256"], old["plan_sha256"])
        self.assertEqual(self.cli("drive", "--run-id", self.RUN_ID, "--dry-run")[0], 0)

    def test_plan_check_answers_from_the_cache_with_an_exit_code(self):
        """Issue #8: no run is created, a cached verdict costs nothing, findings exit 1."""
        from openclaw_ecc_orchestrator.runs.dag import plan_sha256
        from openclaw_ecc_orchestrator.team import plan_check as PC
        sha = plan_sha256(self.plan)
        head = git(self.repo, "rev-parse", "main")
        PC.store(self.state, sha, {"verdict": "changes_requested", "provider": "claude", "findings": [
            {"severity": "blocker", "where": "alpha", "what": "missing depends_on"}]}, head)
        code, doc = self.cli("plan-check", "--plan", self.plan_path)
        self.assertEqual(code, 1, json.dumps(doc)[:2000])
        self.assertEqual((doc["data"]["cached"], doc["data"]["plan_sha256"], doc["data"]["cost_usd"]),
                         (True, sha, 0.0))
        self.assertEqual(doc["data"]["findings"][0]["what"], "missing depends_on")
        PC.store(self.state, sha, {"verdict": "approved", "provider": "claude", "findings": []}, head)
        self.ok(self.cli("plan-check", "--plan", self.plan_path))
        self.assertFalse(os.path.exists(os.path.join(self.state, "runs", self.RUN_ID)))

    def test_create_run_on_an_existing_id_names_the_replace_command(self):
        self.create_run()
        code, doc = self.cli("create-run", "--run-id", self.RUN_ID, "--plan", self.plan_path, "--dry-run")
        self.assertNotEqual(code, 0)
        self.assertIn("--replace", doc["required_user_actions"][0]["detail"])


class ProductParserTests(unittest.TestCase):
    def test_accept_needs_every_criterion_met(self):
        head = "b" * 40
        doc, _ = D.parse_product(f'{D.PRODUCT_MARK} {{"verdict": "accept", "head": "{head}", "criteria": '
                                 '[{"criterion": "x", "met": false}]}', head)
        self.assertEqual(doc["verdict"], "reject")
        doc, _ = D.parse_product(f'{D.PRODUCT_MARK} {{"verdict": "accept", "head": "{head}", "criteria": []}}', head)
        self.assertEqual(doc["verdict"], "reject")


if __name__ == "__main__":
    unittest.main()
