"""TEAM-DELIVERY-v2 section 5: repair loops (AT-TD-01 to AT-TD-05)."""

import json
import os
import unittest

try:
    from ._integration import PY, IntegrationBase, git
    from ._units import work_unit
except ImportError:  # discovered as a top level module
    from _integration import PY, IntegrationBase, git
    from _units import work_unit

from openclaw_ecc_orchestrator.cli.commands import retry_brief, runner_prompt
from openclaw_ecc_orchestrator.runs import repair as R

# Modes: write SPEC.. | commitfail SPEC.. | badhandoff SPEC.. | nohandoff SPEC.. | forged SPEC..
#        auth | denied | fixhandoff PROMPT
RUNNER = r'''
import json, os, subprocess, sys
mode = sys.argv[1]
path = os.environ.get("ECC_HANDOFF_PATH")

def head():
    return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

def handoff(files, status="succeeded", commands=None, **bad):
    doc = {"schema_version": "1.0", "unit_id": os.environ["ECC_UNIT_ID"], "status": status, "outcome": "done",
           "files_changed": files, "behavior": "as specified",
           "commands": commands or [{"command": "git commit", "exit_code": 0, "result": "ok"}],
           "unresolved_failures": [], "assumptions": [], "risks": [], "next_action": "review",
           "commit": {"sha": head(), "branch": "unit", "worktree": "."}, "usage": None,
           "user_input_required": False}
    doc.update(bad)
    with open(path, "w") as fh:
        json.dump(doc, fh)

if mode == "auth":
    print(json.dumps({"type": "result", "is_error": True, "result": "Not logged in · Please run /login"}))
    sys.exit(1)
if mode == "denied":
    print('{"permission_denials":[{"tool_name":"Bash","tool_input":{}}],"result":"could not run uv"}')
    sys.exit(1)
if mode == "fixhandoff":
    with open(path + ".prompt", "w") as fh:
        fh.write(sys.argv[2])
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"], capture_output=True, text=True,
                           check=True).stdout.split()
    handoff(files)
    sys.exit(0)
names = []
for spec in sys.argv[2:]:
    name, _, content = spec.partition("=")
    with open(name, "w") as fh:
        fh.write(content + "\n")
    subprocess.run(["git", "add", name], check=True)
    names.append(name)
subprocess.run(["git", "commit", "-q", "-m", "work " + mode], check=True)
if mode == "commitfail":
    print("tests failed", file=sys.stderr)
    sys.exit(1)
if mode == "nohandoff":
    sys.exit(0)
if mode == "badhandoff":
    handoff(names, status="completed", behavior=["a list, not a string"])
elif mode == "forged":
    handoff(names, commands=[{"command": "pytest", "exit_code": 1, "result": "1 failed"}])
else:
    handoff(names)
'''


def plan(*units):
    return {"units": list(units)}


class RepairBase(IntegrationBase):
    FILES = ["a.txt", "b.txt"]

    def setUp(self):
        super().setUp()
        self.script = os.path.join(self.tmp, "team_runner.py")
        with open(self.script, "w") as fh:
            fh.write(RUNNER)

    def make(self, attempts=3, repair_turns=1, **unit):
        self.c, self.m, _ = self.conductor("c1", auto_retry=True, handoff_repair_turns=repair_turns)
        u = work_unit("u1", files=self.FILES, commands=[f"{PY} -c pass"], budget={"attempts": attempts},
                      routing={"initial_tier": 0, "maximum_tier": 0}, **unit)
        self.ok(self.c.create_run(plan(u), run_id="r1"))

    def run_attempt(self, *argv, repair=True):
        self.ok(self.c.assign_unit("r1", "u1"))
        repair_argv = [PY, self.script, "fixhandoff", R.REPAIR_PROMPT_TOKEN] if repair else None
        self.ok(self.c.start_unit("r1", "u1", [PY, self.script, *argv], repair_argv=repair_argv))
        return self.c.wait_unit("r1", "u1")

    def unit(self):
        return self.m.load("r1")["units"]["u1"]

    def attempts_recorded(self):
        return len(((self.unit()["annotations"].get("escalation") or {}).get("history")
                    or (self.unit()["annotations"].get("escalation") or {}).get("attempts") or []))


class RetryTests(RepairBase):
    def test_at_td_01_failed_unit_returns_to_ready_within_budget(self):
        """AT-TD-01: within budget a failure goes back to ready with the evidence; outside it, needs_user."""
        self.make(attempts=2)
        self.run_attempt("commitfail", "a.txt=1")
        u = self.unit()
        self.assertEqual(u["state"], "ready")
        self.assertEqual(u["last_reason"], "retry:runner_exit_1")
        self.assertEqual(u["annotations"]["last_failure"]["reason"], "runner_exit_1")
        for _ in range(4):
            if self.unit()["state"] != "ready":
                break
            self.run_attempt("commitfail", "a.txt=again")
        self.assertEqual(self.unit()["state"], "needs_user")
        self.assertEqual(self.unit()["last_reason"], "escalation_stopped")

    def test_at_td_02_retry_reuses_worktree_and_keeps_commits(self):
        """AT-TD-02: the second attempt starts on the first attempt's commits."""
        self.make()
        self.run_attempt("commitfail", "a.txt=first")
        self.assertEqual(self.unit()["state"], "ready")
        path = self.unit()["workspace"]["path"]
        self.assertTrue(os.path.isfile(os.path.join(path, "a.txt")))
        self.ok(self.run_attempt("write", "b.txt=second"))
        self.assertEqual(self.unit()["state"], "reviewing")
        branch = self.unit()["workspace"]["branch"]
        tree = git(self.repo, "ls-tree", "--name-only", branch)
        self.assertIn("a.txt", tree)
        self.assertIn("b.txt", tree)


class BriefTests(unittest.TestCase):
    def test_at_td_03_brief_carries_the_evidence_and_is_bounded(self):
        """AT-TD-03: gate tails, handoff errors, review and verifier evidence; at most 6,000 characters."""
        gates = [{"name": "acceptance[0]", "command": "uv run pytest", "status": "failed", "exit_code": 1,
                  "output_tail": "E   assert 1 == 2\n" * 400},
                 {"name": "unit-tests", "command": "x", "status": "passed", "exit_code": 0, "output_tail": ""}]
        ev = R.failure_evidence("gates_failed", gates=gates,
                                review={"verdict": "changes_requested", "findings": [{"what": "missing test"}]},
                                verification={"verdict": "fail", "checks": [{"what": "cli --help", "ok": False}]})
        text = R.failure_brief("M0-01", 2, 3, ev)
        self.assertLessEqual(len(text), R.MAX_BRIEF_CHARS)
        self.assertIn("Attempt 2 of 3 for M0-01", text)
        self.assertIn("uv run pytest", text)
        self.assertIn("assert 1 == 2", text)
        self.assertNotIn("unit-tests", text)
        self.assertIn("missing test", text)
        self.assertIn("cli --help", text)
        self.assertTrue(text.rstrip().endswith("write the handoff."))
        handoff = R.failure_brief("U", 2, 3, R.failure_evidence("handoff_invalid", handoff_problems=["status x"]))
        self.assertIn("handoff validation errors: status x", handoff)
        self.assertIn("The code may already be complete", handoff)

    def test_brief_reaches_the_next_prompt_only_on_a_retry(self):
        punit = {"id": "u1", "title": "t", "budget": {"attempts": 3}}
        run = {"units": {"u1": {"attempts": 1, "last_reason": "retry:gates_failed", "annotations": {
            "last_failure": {"reason": "gates_failed", "evidence": {"reason": "gates_failed"}}}}}}
        brief = retry_brief(run, "u1", punit)
        self.assertIn("Attempt 2 of 3 for u1", brief)
        self.assertIn(brief, runner_prompt(punit, brief))
        run["units"]["u1"]["last_reason"] = "dependencies_merged"
        self.assertIsNone(retry_brief(run, "u1", punit))


class HandoffRepairTests(RepairBase):
    def test_at_td_04_invalid_handoff_repaired_without_an_attempt(self):
        """AT-TD-04: one repair turn fixes a malformed handoff; the escalation record is not charged."""
        self.make()
        self.ok(self.run_attempt("badhandoff", "a.txt=1"))
        u = self.unit()
        self.assertEqual(u["state"], "reviewing")
        self.assertTrue(u["annotations"]["handoff_repair"]["handoff_found"])
        self.assertEqual(self.attempts_recorded(), 1)  # the one passing attempt
        with open(self._prompt_file()) as fh:
            prompt = fh.read()
        self.assertIn("status must be one of", prompt)
        self.assertNotIn(R.REPAIR_PROMPT_TOKEN, prompt)
        self.assertNotIn("{ECC_HANDOFF_PATH}", prompt)

    def _prompt_file(self):
        logs = os.path.join(self.state_root, "logs", "r1")
        names = [n for n in os.listdir(logs) if n.endswith(".handoff.json.prompt")]
        self.assertEqual(len(names), 1)
        return os.path.join(logs, names[0])

    def test_missing_handoff_repaired(self):
        self.make()
        self.ok(self.run_attempt("nohandoff", "a.txt=1"))
        self.assertEqual(self.unit()["state"], "reviewing")

    def test_semantic_violation_is_never_repaired(self):
        """A handoff claiming success with a failing command goes to a normal retry, not a repair turn."""
        self.make()
        self.run_attempt("forged", "a.txt=1")
        u = self.unit()
        self.assertNotIn("handoff_repair", u["annotations"])
        self.assertEqual(u["last_reason"], "retry:handoff_invalid")
        self.assertIn("success_with_failures", json.dumps(u["annotations"]["last_failure"]))

    def test_repair_turns_zero_disables_repair(self):
        self.make(repair_turns=0)
        self.run_attempt("badhandoff", "a.txt=1")
        self.assertEqual(self.unit()["last_reason"], "retry:handoff_invalid")


class StopTests(RepairBase):
    def test_at_td_05_not_logged_in_stops_without_an_attempt(self):
        """AT-TD-05: a login failure goes to needs_user with the fix and does not charge the budget."""
        self.make()
        res = self.run_attempt("auth")
        self.assertFalse(res["ok"])
        u = self.unit()
        self.assertEqual(u["state"], "needs_user")
        self.assertEqual(u["last_reason"], "runner_auth")
        self.assertIn("/login", u["annotations"]["last_failure"]["fix"])
        self.assertEqual(self.attempts_recorded(), 0)
        self.assertIn("/login", json.dumps(res["required_user_actions"]))

    def test_denied_tool_stops_as_platform_defect(self):
        self.make()
        self.run_attempt("denied")
        u = self.unit()
        self.assertEqual(u["last_reason"], "runner_permission_denied")
        self.assertIn("Bash", json.dumps(u["annotations"]["last_failure"]))

    def test_classifier_ignores_ordinary_auth_words_in_unit_output(self):
        text = "tests failed: expected 401 Unauthorized from the GitHub adapter"
        self.assertIsNone(R.classify_runner_failure(text, 1))
        self.assertIsNone(R.classify_runner_failure("Not logged in", 0))
        self.assertEqual(R.classify_runner_failure("error: unknown option '--permission-prompts'", 1)[0],
                         "runner_missing")


if __name__ == "__main__":
    unittest.main()
