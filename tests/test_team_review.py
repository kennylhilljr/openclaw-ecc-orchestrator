"""TEAM-DELIVERY-v2 sections 3.1 to 3.3, 4.1 and 6.3: the team review step
(AT-TD-06 to 09, 14 to 16, 18 to 20, 22)."""

import json
import os
import shlex
import tempfile
import unittest

try:
    from ._integration import PY, IntegrationBase, git
    from ._units import policy as make_policy
    from ._units import work_unit
except ImportError:  # discovered as a top level module
    from _integration import PY, IntegrationBase, git
    from _units import policy as make_policy
    from _units import work_unit

from openclaw_ecc_orchestrator.process.supervisor import Supervisor
from openclaw_ecc_orchestrator.runners.discovery import HttpResponse
from openclaw_ecc_orchestrator.team import config as TC
from openclaw_ecc_orchestrator.team import personas as P
from openclaw_ecc_orchestrator.team.review import TeamStep
from openclaw_ecc_orchestrator.team.rotation import Rotation

CERTIFIED = ["codex", "claude", "gemini", "kimi", "groq"]
CATALOG = {"providers": {
    "gemini": {"status": "ok", "models": [{"id": "gemini-3.5-flash-lite"}]},
    "kimi": {"status": "ok", "models": [{"id": "kimi-k3"}, {"id": "kimi-k2.7-code"}]},
    "groq": {"status": "ok", "models": [{"id": "openai/gpt-oss-20b"}]},
}}
PREFS = {"gemini": {"economical": ["gemini-3.5-flash-lite"], "standard": ["gemini-3.5-flash-lite"]},
         "kimi": {"standard": ["kimi-k2.7-code"], "advanced": ["kimi-k3"]},
         "groq": {"economical": ["openai/gpt-oss-20b"]}}
ROLE_OF = {"You are the PR reviewer": "pr_reviewer", "You are the security reviewer": "security_reviewer",
           "You are the architect": "architect", "You are the designer": "designer",
           "You are the QA verifier": "verifier"}


def role_of(prompt):
    for marker, role in ROLE_OF.items():
        if marker in prompt:
            return role
    return "unknown"


class TeamBase(IntegrationBase):
    CERTIFIED = CERTIFIED

    def setUp(self):
        super().setUp()
        self.policy = make_policy(model_preferences=PREFS)
        self.calls = []          # (role, provider)
        self.replies = {}        # role -> list of reply builders(head) or verdict strings
        self.verifier_cwds = []

    def reply(self, role, head):
        queue = self.replies.get(role) or ["approved" if role != "verifier" else "pass"]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(item):
            return item(head)
        mark = P.VERIFY_MARK if role == "verifier" else P.REVIEW_MARK
        doc = {"verdict": item, "head": head, "findings": []}
        if item == "changes_requested":
            doc["findings"] = [{"severity": "blocker", "where": "a.txt:1", "what": "missing edge case"}]
        if role == "verifier":
            doc["checks"] = [{"what": "cli", "how": "x", "expected": "y", "actual": "y", "ok": item == "pass"}]
        return f"Reviewed.\n{mark} {json.dumps(doc)}"

    def cli_override(self, role, runner, prompt):
        if role_of(prompt) in getattr(self, "slow_roles", ()):
            self.clock.t += 600  # the turn outlasts the 60 s test lease
        if (role_of(prompt), runner) in getattr(self, "silent", ()):
            self.calls.append((role_of(prompt), runner))
            return [PY, "-c", "import sys; sys.exit(3)"]
        self.calls.append((role_of(prompt), runner))
        text = self.reply(role_of(prompt), self.head)
        script = ("import subprocess,sys,os\n"
                  "b=subprocess.run(['git','rev-parse','--abbrev-ref','HEAD'],capture_output=True,text=True).stdout\n"
                  f"open({json.dumps(os.path.join(self.tmp, 'cwds.txt'))},'a').write(os.getcwd()+' '+b)\n"
                  f"print({json.dumps(text)})\n")
        return [PY, "-c", script]

    def http_post(self, url, headers, body, timeout):
        prompt = (body.get("messages") or [{}])[0].get("content") or \
            ((body.get("contents") or [{}])[0].get("parts") or [{}])[0].get("text") or ""
        provider = "gemini" if "googleapis" in url else ("kimi" if "moonshot" in url else "groq")
        self.calls.append((role_of(prompt), provider))
        if role_of(prompt) in getattr(self, "slow_roles", ()):
            self.clock.t += 600
        text = self.reply(role_of(prompt), self.head)
        if (role_of(prompt), provider) in getattr(self, "silent", ()):
            text = ""
        if provider == "gemini":
            data = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
        else:
            data = {"choices": [{"message": {"content": text}}], "model": "m"}
        return HttpResponse(200, json.dumps(data))

    def setup_unit(self, files=("a.txt",), team=None, high_risk=False):
        self.c, self.m, _ = self.conductor("c1", certified_runners=CERTIFIED, catalog=CATALOG, auto_retry=True,
                                           policy=self.policy)
        u = work_unit("u1", files=list(files), commands=[f"{PY} -c pass"], budget={"attempts": 3},
                      routing={"initial_tier": 0, "maximum_tier": 2}, risk="high" if high_risk else "low")
        self.ok(self.c.create_run({"units": [u]}, run_id="r1"))
        specs = [f"{f}=content" for f in files]
        res = self.drive_to_review(self.c, "r1", "u1", self.argv("write", *specs))
        self.ok(res)
        unit = self.m.load("r1")["units"]["u1"]
        self.assertEqual(unit["state"], "reviewing")
        self.head = unit["annotations"]["verification"]["head"]
        self.author = unit["routing"]["runner"]
        cfg = TC.load(team or {"merge_approval": "agents"})
        self.step = TeamStep(self.c, cfg, state_dir=self.state_root, worktree_root=os.path.join(self.tmp, "wt"),
                             logs_dir=os.path.join(self.state_root, "logs"),
                             supervisor=Supervisor(base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                                             "HOME": self.tmp, "USER": "t"}, grace=1.0),
                             clock=self.clock, cli_override=self.cli_override, http_post=self.http_post,
                             env={"GEMINI_API_KEY": "g-test", "MOONSHOT_API_KEY": "k-test", "GROQ_API_KEY": "q-test"})

    def unit(self):
        return self.m.load("r1")["units"]["u1"]

    def providers(self, role):
        return [p for r, p in self.calls if r == role]


class ReviewFlowTests(TeamBase):
    def test_at_td_06_reviewers_are_independent_and_verdicts_parse(self):
        """AT-TD-06: the reviewer and verifier are never the author's provider; verdict lines parse."""
        self.setup_unit()
        res = self.step.run("r1", "u1")
        self.assertTrue(res["ok"], json.dumps(res, indent=1, default=str)[:3000])
        self.assertNotIn(self.author, self.providers("pr_reviewer"))
        self.assertNotIn(self.author, self.providers("verifier"))
        team = self.unit()["annotations"]["team"]
        self.assertEqual(team["reviews"][0]["verdict"], "approved")
        self.assertEqual(team["verification"]["verdict"], "pass")

    def test_wrong_head_is_reasked_then_accepted(self):
        self.setup_unit()
        wrong = lambda head: f'{P.REVIEW_MARK} {json.dumps({"verdict": "approved", "head": "0" * 40})}'  # noqa: E731
        self.replies["pr_reviewer"] = [wrong, "approved"]
        self.ok(self.step.run("r1", "u1"))
        self.assertEqual(len(self.providers("pr_reviewer")), 2)
        self.assertEqual(len(set(self.providers("pr_reviewer"))), 1, "the re-ask goes to the same provider")

    def test_unparsable_twice_counts_as_changes_requested(self):
        self.setup_unit()
        self.replies["pr_reviewer"] = [lambda head: "looks fine to me"]
        self.step.run("r1", "u1")
        u = self.unit()
        self.assertEqual(u["state"], "ready")
        self.assertIn("unusable", json.dumps(u["annotations"]["last_failure"]))

    def test_at_td_07_changes_requested_returns_to_coder_with_findings(self):
        """AT-TD-07: changes requested goes back to ready with the findings; rejected stops."""
        self.setup_unit()
        self.replies["pr_reviewer"] = ["changes_requested"]
        self.step.run("r1", "u1")
        u = self.unit()
        self.assertEqual(u["state"], "ready")
        self.assertEqual(u["last_reason"], "review:pr_reviewer_changes_requested")
        self.assertIn("missing edge case", json.dumps(u["annotations"]["last_failure"]))

    def test_rejected_stops_for_the_owner(self):
        self.setup_unit()
        self.replies["pr_reviewer"] = ["rejected"]
        self.step.run("r1", "u1")
        self.assertEqual(self.unit()["state"], "needs_user")

    def test_verifier_failure_returns_to_coder_and_runs_in_a_throwaway_checkout(self):
        self.setup_unit()
        self.replies["verifier"] = ["fail"]
        self.step.run("r1", "u1")
        u = self.unit()
        self.assertEqual(u["last_reason"], "review:verification_failed")
        with open(os.path.join(self.tmp, "cwds.txt")) as fh:
            lines = fh.read().split("\n")
        verify_line = [ln for ln in lines if "_verify" in ln]
        self.assertTrue(verify_line and verify_line[0].endswith("HEAD"), lines)
        self.assertNotIn("_verify", git(self.repo, "worktree", "list"), "the throwaway checkout is removed")

    def test_at_td_08_agents_approve_and_merge(self):
        """AT-TD-08: approving review and verification on the same head merge without operator input."""
        self.setup_unit()
        self.ok(self.step.run("r1", "u1"))
        self.assertEqual(self.unit()["state"], "merged")
        self.assertEqual(git(self.repo, "show", "main:a.txt"), "content")
        team = self.unit()["annotations"]["team"]
        self.assertEqual(team["merge"]["status"], "merged")

    def test_at_td_09_human_mode_leaves_the_approval_to_the_owner(self):
        """AT-TD-09: with merge_approval human the unit waits for the broker approval as before."""
        self.setup_unit(team={"merge_approval": "human"})
        res = self.step.run("r1", "u1")
        self.assertEqual(self.unit()["state"], "reviewing")
        self.assertEqual([a["kind"] for a in res["required_user_actions"]], ["approve"])


class TriggerTests(TeamBase):
    def test_at_td_14_and_20_security_reviewer_for_high_risk_paths(self):
        """AT-TD-14, AT-TD-20: high-risk files bring the security reviewer, whose provider differs from
        the author's and the PR reviewer's."""
        self.setup_unit(files=("src/security/guard.py",))
        self.ok(self.step.run("r1", "u1"))
        sec = self.providers("security_reviewer")
        self.assertEqual(len(sec), 1)
        self.assertNotIn(sec[0], {self.author, self.providers("pr_reviewer")[0]})

    def test_independence_is_the_persona_not_the_provider(self):
        """Owner decision (rmcp-m0f): when no other provider answers, a provider already playing the
        author or PR reviewer plays the security reviewer persona in its own session; nothing blocks."""
        self.setup_unit(files=("src/security/guard.py",))
        self.silent = {("security_reviewer", p) for p in ("claude", "gemini", "groq")}
        self.ok(self.step.run("r1", "u1"))
        reviews = self.m.load("r1")["units"]["u1"]["annotations"]["team"]["reviews"]
        pr = [r for r in reviews if r["role"] == "pr_reviewer"][-1]
        sec = [r for r in reviews if r["role"] == "security_reviewer"][-1]
        self.assertEqual(pr["independence"], "provider")
        self.assertEqual(sec["verdict"], "approved")
        self.assertIn(sec["provider"], {self.author, pr["provider"]})
        self.assertEqual(sec["independence"], "persona")

    def test_a_turn_longer_than_the_lease_still_records_its_verdict(self):
        """Live finding (rmcp-m0f): a 5.5 minute review outlasted the 5 minute lease and its verdict was lost."""
        self.setup_unit()
        self.slow_roles = ("pr_reviewer", "verifier")
        self.ok(self.step.run("r1", "u1"))
        unit = self.m.load("r1")["units"]["u1"]
        reviews = unit["annotations"]["team"]["reviews"]
        self.assertEqual(reviews[-1]["verdict"], "approved")
        self.assertIn(unit["state"], ("queued_for_merge", "merged"))

    def test_no_security_reviewer_for_ordinary_paths(self):
        self.setup_unit()
        self.ok(self.step.run("r1", "u1"))
        self.assertEqual(self.providers("security_reviewer"), [])

    def test_at_td_15_16_architect_and_designer_triggers(self):
        """AT-TD-15 (contract units), AT-TD-16 (designer only for UI units)."""
        team = {"merge_approval": "agents", "roles": {"architect": {"contract_paths": ["core/**"]},
                                                      "designer": {"when_paths": ["ui/**"]}}}
        self.setup_unit(files=("core/specs.py",), team=team)
        self.ok(self.step.run("r1", "u1"))
        self.assertEqual(len(self.providers("architect")), 1)
        self.assertEqual(self.providers("designer"), [])


class RotationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "team", "r1.json")
        self.cands = [{"runner": r, "model": "m", "estimated_cost_usd": 0.01, "cost_source": "catalog"} for r in ("gemini", "groq", "kimi",
                                                                                         "codex", "claude")]

    def tearDown(self):
        self._tmp.cleanup()

    def test_at_td_18_rotation_walks_the_panel_and_persists(self):
        """AT-TD-18: each assignment takes the next eligible provider; the cursor survives a restart."""
        panel = ["gemini", "groq", "kimi", "codex", "claude"]
        rot = Rotation(self.path)
        picks = [rot.choose("pr", panel, self.cands, exclude={"codex"})[0]["runner"] for _ in range(3)]
        self.assertEqual(picks, ["gemini", "groq", "kimi"])
        again = Rotation(self.path)
        self.assertEqual(again.choose("pr", panel, self.cands, exclude={"codex"})[0]["runner"], "claude")

    def test_skips_are_explained(self):
        panel = ["groq", "gemini"]
        choice, skipped = Rotation(self.path).choose("pr", panel, self.cands, pack_tokens=10000,
                                                     max_pack={"groq": 6000})
        self.assertEqual(choice["runner"], "gemini")
        self.assertIn(("groq", "evidence pack too large (10000 tokens)"), skipped)
        _, skipped = Rotation(self.path).choose("pr", ["claude"], self.cands, cost_cap=0.001)
        self.assertIn(("claude", "over the per-turn cost cap"), skipped)

    def test_at_td_22_share_cap(self):
        """AT-TD-22: with three or more eligible providers none exceeds the share cap."""
        rot = Rotation(self.path)
        panel = ["gemini", "kimi", "claude"]
        picks = [rot.choose("x", ["gemini"] + panel[1:], self.cands, share_cap=0.4)[0]["runner"] for _ in range(10)]
        for provider in set(picks):
            self.assertLessEqual(picks.count(provider) / len(picks), 0.4 + 1e-9, picks)


class AuthorProvidersPolicyTests(unittest.TestCase):
    def test_api_providers_review_but_do_not_author(self):
        """author_providers keeps API providers out of authoring while they still review."""
        from openclaw_ecc_orchestrator import schemas
        from openclaw_ecc_orchestrator.routing.selection import select_runner
        pol = make_policy(model_preferences=PREFS, author_providers=["claude", "codex"])
        self.assertTrue(schemas.validate_repository_policy(pol).ok)
        authors = select_runner(1, CERTIFIED, CATALOG, pol, role="author").candidates
        self.assertEqual({c["runner"] for c in authors}, {"claude", "codex"})
        reviewers = select_runner(1, CERTIFIED, CATALOG, pol, role="review").candidates
        self.assertIn("kimi", {c["runner"] for c in reviewers})
        bad = make_policy(allowed_providers=["claude"], author_providers=["codex"])
        self.assertFalse(schemas.validate_repository_policy(bad).ok)


class ApiPersonaTests(TeamBase):
    def test_at_td_19_api_personas_get_the_pack_and_return_verdicts(self):
        """AT-TD-19: an API provider plays a text-only role from the evidence pack."""
        self.setup_unit(team={"merge_approval": "agents",
                              "roles": {"pr_reviewer": {"panels": {"economical": ["gemini"],
                                                                   "standard": ["kimi"], "advanced": ["kimi"]}}}})
        self.ok(self.step.run("r1", "u1"))
        self.assertIn(self.providers("pr_reviewer")[0], ("gemini", "kimi"))
        self.assertEqual(self.unit()["state"], "merged")

    def test_verdict_parser_rules(self):
        head = "a" * 40
        ok, _ = P.parse_verdict(f'x\n{P.REVIEW_MARK} {{"verdict": "approved", "head": "{head[:12]}", '
                                '"findings": [{"severity": "blocker", "what": "x"}]}', P.REVIEW_MARK, head)
        self.assertEqual(ok["verdict"], "changes_requested")
        self.assertIsNone(P.parse_verdict("no line", P.REVIEW_MARK, head)[0])


if __name__ == "__main__":
    unittest.main()
