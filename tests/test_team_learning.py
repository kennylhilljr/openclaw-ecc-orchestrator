"""TEAM-DELIVERY v7: personas record lessons; the delivery lead turns them into
practice changes that the owning personas review and the gates check."""

import json
import os
import re
import tempfile
import unittest

try:
    from ._integration import PY, git
    from .test_team_drive import DriveBase
except ImportError:  # discovered as a top level module
    from _integration import PY, git
    from test_team_drive import DriveBase

from openclaw_ecc_orchestrator.runners.discovery import HttpResponse
from openclaw_ecc_orchestrator.team import learning as L
from openclaw_ecc_orchestrator.team import personas as P

PRACTICES = "changed the repository's practice files"


class CaptureTests(unittest.TestCase):
    def test_verdicts_feed_the_log_once(self):
        with tempfile.TemporaryDirectory() as d:
            log = L.Lessons(d, "r1")
            head = "a" * 40
            doc, _ = P.parse_verdict(P.REVIEW_MARK + " " + json.dumps({
                "verdict": "changes_requested", "head": head,
                "findings": [{"severity": "blocker", "where": "tests/conftest.py:9", "what": "sendmsg unguarded"},
                             {"severity": "minor", "what": "naming"}],
                "lessons": [{"practice": ".agents/skills/security-review", "lesson": "Guard sendmsg and DNS too"}]}),
                P.REVIEW_MARK, head)
            record = dict(doc, role="security_reviewer", provider="codex")
            L.capture_verdict(log, "M0-01", record)
            L.capture_verdict(log, "M0-01", record)  # the same lesson again is not duplicated
            L.capture_handoff(log, "M0-02", {"notes": "done\nLesson: the spec says nothing about trailing dots"})
            L.capture_failure(log, "M0-03", "gates_failed",
                              {"gates": [{"name": "ci", "command": "tools/ci.sh", "status": "failed"}]})
            items = log.all()
            self.assertEqual([(x["id"], x["source"], x["role"]) for x in items],
                             [("L0001", "finding", "security_reviewer"), ("L0002", "lesson", "security_reviewer"),
                              ("L0003", "coder", "coder"), ("L0004", "gates", "gates")])
            self.assertEqual(items[1]["practice"], ".agents/skills/security-review")

    def test_prompts_ask_every_persona_for_lessons(self):
        self.assertIn('"lessons"', P.review_prompt("pr_reviewer", "pack", "a" * 40))
        self.assertIn('"lessons"', P.verify_prompt("pack", "a" * 40))


class StewardTests(DriveBase):
    def setUp(self):
        super().setUp()
        self.steward_script = os.path.join(self.tmp, "steward.py")
        self.touch = "AGENTS.md"

    def write_steward(self, files):
        body = "".join(f"open({f!r}, 'a').write('- Guard sendmsg and DNS lookups (L0001).\\n')\n" for f in files)
        with open(self.steward_script, "w") as fh:
            fh.write("import os, subprocess\nos.makedirs('.agents', exist_ok=True)\n" + body +
                     "subprocess.run(['git', 'add', '-A'], check=True)\n"
                     "subprocess.run(['git', 'commit', '-qm', 'Practices: L0001'], check=True)\n"
                     f"print({P.STEWARD_MARK!r} + ' ' + '{{\"applied\": [\"L0001\"], \"proposed\": [], \"skipped\": []}}')\n")

    def reply(self, role, head):
        if role == "pr_reviewer":
            doc = {"verdict": "approved", "head": head or self.current_head(), "findings": [],
                   "lessons": [{"practice": "AGENTS.md", "lesson": "Guard sendmsg and DNS lookups"}]}
            return f"{P.REVIEW_MARK} {json.dumps(doc)}"
        return super().reply(role, head)

    def practices_reply(self, prompt):
        head = re.search(r'"head": "([0-9a-f]{40})"', prompt).group(1)
        return f"{P.REVIEW_MARK} {json.dumps({'verdict': 'approved', 'head': head, 'findings': []})}"

    def cli_override(self, role, runner, prompt):
        if role == "steward":
            self.calls.append(("steward", runner))
            return [PY, self.steward_script]
        if PRACTICES in prompt:
            self.calls.append(("practices_review", runner))
            return [PY, "-c", f"print({json.dumps(self.practices_reply(prompt))})"]
        return super().cli_override(role, runner, prompt)

    def http_post(self, url, headers, body, timeout):
        prompt = (body.get("messages") or [{}])[0].get("content") or \
            ((body.get("contents") or [{}])[0].get("parts") or [{}])[0].get("text") or ""
        if PRACTICES in prompt:
            self.calls.append(("practices_review", "api"))
            text = self.practices_reply(prompt)
            data = {"candidates": [{"content": {"parts": [{"text": text}]}}]} if "googleapis" in url else \
                {"choices": [{"message": {"content": text}}]}
            return HttpResponse(200, json.dumps(data))
        return super().http_post(url, headers, body, timeout)

    def drive_state(self):
        with open(os.path.join(self.state_root, "team", "r1-drive.json")) as fh:
            return json.load(fh)

    def team(self):
        return {"merge_approval": "agents", "milestone": {"name": "M0"},
                "learning": {"after_merged_units": 1, "min_new_lessons": 1}}

    def test_lessons_reach_main_during_the_milestone(self):
        self.write_steward(["AGENTS.md", ".agents/LESSONS.md"])
        res = self.make(self.units2(), team=self.team()).go()
        self.assertTrue(res["ok"], json.dumps(res, indent=1, default=str)[:3000])
        self.assertIn("Guard sendmsg and DNS lookups", git(self.repo, "show", "main:AGENTS.md"))
        state = self.drive_state()
        runs = state["learning"]["runs"]
        self.assertEqual(runs[0]["status"], "merged")
        self.assertEqual(runs[0]["applied"], ["L0001"])
        self.assertIn("practices_review", [r for r, _ in self.calls])
        roles = [r for r, _ in self.calls]
        self.assertLess(roles.index("steward"), len(roles) - 1)  # ran during the milestone, not only at its end
        with open(res["data"]["report"]) as fh:
            self.assertIn("Practices learned during the milestone", fh.read())

    def test_steward_may_not_touch_other_files(self):
        self.write_steward(["src.py"])
        res = self.make(self.units2(), team=self.team()).go()
        self.assertTrue(res["ok"], json.dumps(res, indent=1, default=str)[:3000])
        state = self.drive_state()
        self.assertTrue(state["learning"]["runs"][0]["status"].startswith("rejected"))
        self.assertNotIn("src.py", git(self.repo, "ls-tree", "--name-only", "main").split())

    def test_learning_can_be_turned_off(self):
        self.write_steward(["AGENTS.md"])
        team = dict(self.team(), learning={"enabled": False})
        self.assertTrue(self.make(self.units2(), team=team).go()["ok"])
        self.assertNotIn("steward", [r for r, _ in self.calls])


if __name__ == "__main__":
    unittest.main()
