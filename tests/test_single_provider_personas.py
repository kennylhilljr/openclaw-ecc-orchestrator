"""Single-provider teams: review independence by persona, and repository
personas (sdd_framework `.claude/agents/<name>.md`) in role prompts."""

import copy
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # shares fixtures with test_routing_select

from openclaw_ecc_orchestrator.routing import classify as C
from openclaw_ecc_orchestrator.routing.selection import (review_independence, review_satisfies,
                                                         select_reviewer)
from openclaw_ecc_orchestrator.schemas import validate_repository_policy
from openclaw_ecc_orchestrator.team import config as TC
from openclaw_ecc_orchestrator.team import personas as P

from test_routing_select import CATALOG, NOW, POLICY, sel, unit

PERSONA_POLICY = dict(copy.deepcopy(POLICY), review_independence="persona")


class ReviewIndependenceTests(unittest.TestCase):
    def strict_claude(self, policy):
        classification = C.classify(unit(traits=["security"]), policy)
        author = sel(2, runners=["claude"])
        self.assertTrue(author.ok, author.reasons)
        return classification, author

    def test_default_is_provider(self):
        self.assertEqual(review_independence(POLICY), "provider")
        self.assertEqual(review_independence(None), "provider")
        self.assertEqual(review_independence({"review_independence": "other"}), "provider")

    def test_provider_mode_blocks_claude_reviewing_claude(self):
        classification, author = self.strict_claude(POLICY)
        chosen = select_reviewer(author, classification, ["claude"], CATALOG, POLICY, now=NOW)
        self.assertFalse(chosen.ok)
        review = {"runner": "claude", "model": "opus", "tier": 2}
        ok, reasons = review_satisfies(review, author, classification, policy=POLICY)
        self.assertFalse(ok)
        self.assertTrue(any("author runner" in r for r in reasons), reasons)

    def test_persona_mode_lets_claude_review_claude(self):
        classification, author = self.strict_claude(PERSONA_POLICY)
        chosen = select_reviewer(author, classification, ["claude"], CATALOG, PERSONA_POLICY, now=NOW)
        self.assertTrue(chosen.ok, chosen.reasons)
        self.assertEqual(chosen.runner, "claude")
        review = {"runner": "claude", "model": "opus", "tier": 2}
        ok, reasons = review_satisfies(review, author, classification, ["claude"], CATALOG,
                                       PERSONA_POLICY, now=NOW)
        self.assertTrue(ok, reasons)

    def test_persona_mode_still_enforces_the_tier(self):
        classification, author = self.strict_claude(PERSONA_POLICY)
        ok, reasons = review_satisfies({"runner": "claude", "model": "sonnet", "tier": 1}, author,
                                       classification, policy=PERSONA_POLICY)
        self.assertFalse(ok)
        self.assertTrue(any("below the required tier" in r for r in reasons), reasons)

    def test_policy_schema(self):
        self.assertTrue(validate_repository_policy(PERSONA_POLICY).ok)
        bad = dict(copy.deepcopy(POLICY), review_independence="vibes")
        report = validate_repository_policy(bad)
        self.assertFalse(report.ok)


class PersonaFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        os.makedirs(os.path.join(self.repo, ".claude", "agents"))
        with open(os.path.join(self.repo, ".claude", "agents", "verifier.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: verifier\ndescription: \"x\"\n---\n\n# verifier\n\nNever verify your own work.\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_strips_front_matter(self):
        text = P.load_persona(self.repo, "verifier")
        self.assertTrue(text.startswith("# verifier"))
        self.assertNotIn("description:", text)

    def test_missing_or_invalid_names(self):
        self.assertIsNone(P.load_persona(self.repo, "architect"))
        self.assertIsNone(P.load_persona(self.repo, "../verifier"))
        self.assertIsNone(P.load_persona(self.repo, None))

    def test_missing_personas(self):
        cfg = {"personas": {"pr_reviewer": "verifier", "architect": "architect"}}
        self.assertEqual(P.missing_personas(self.repo, cfg), ["architect"])

    def test_with_persona_keeps_the_task_last(self):
        prompt = P.with_persona("TASK", "verifier", P.load_persona(self.repo, "verifier"))
        self.assertIn('<persona name="verifier">', prompt)
        self.assertTrue(prompt.endswith("TASK"))
        self.assertEqual(P.with_persona("TASK", "verifier", None), "TASK")

    def test_review_prompt_turn_carries_the_persona(self):
        from openclaw_ecc_orchestrator.team.review import TeamStep

        seen = {}

        class FakeConductor:
            class worktrees:
                repo = self.repo

        step = TeamStep(FakeConductor(), {"personas": {"pr_reviewer": "verifier"}}, state_dir=self.repo,
                        worktree_root=self.repo, logs_dir=self.repo, supervisor=None, clock=lambda: NOW)

        def fake_cli(adapter, model, prompt, **kw):
            seen["prompt"] = prompt
            return {"text": "ok"}

        import openclaw_ecc_orchestrator.team.review as R
        original = R.I.run_cli
        R.I.run_cli = fake_cli
        try:
            step._adapters = {"claude": object()}
            step._turn_unkept("pr_reviewer", {"runner": "claude", "model": "opus"}, "REVIEW THIS",
                              self.repo, False, "run", "U-1", 10)
        finally:
            R.I.run_cli = original
        self.assertIn("Never verify your own work.", seen["prompt"])
        self.assertTrue(seen["prompt"].endswith("REVIEW THIS"))

    def test_coder_prompt_carries_the_persona(self):
        from openclaw_ecc_orchestrator.cli.commands import runner_prompt
        punit = {"id": "U-1", "title": "t"}
        plain = runner_prompt(punit)
        withp = runner_prompt(punit, persona=("implementer", "Write tests first."))
        self.assertNotIn("persona", plain)
        self.assertIn("Write tests first.", withp)
        self.assertIn("You are implementing work unit U-1", withp)


class TeamConfigPersonaTests(unittest.TestCase):
    def test_valid_map(self):
        cfg = TC.load({"personas": {"pr_reviewer": "verifier", "coder": "implementer", "steward": "doc-steward"}})
        self.assertEqual(cfg["personas"]["coder"], "implementer")

    def test_unknown_role_or_bad_name(self):
        with self.assertRaises(TC.TeamConfigError):
            TC.load({"personas": {"juggler": "verifier"}})
        with self.assertRaises(TC.TeamConfigError):
            TC.load({"personas": {"pr_reviewer": "../etc/passwd"}})
        with self.assertRaises(TC.TeamConfigError):
            TC.load({"personas": ["verifier"]})

    def test_default_has_no_personas(self):
        self.assertNotIn("personas", TC.load({}))


if __name__ == "__main__":
    unittest.main()
