"""TEAM-DELIVERY v5: branches stay current, the drive heals before it stops,
and every stop says exactly what to run next (live findings, rmcp-m0f)."""

import json
import os
import unittest

try:
    from ._integration import PY, git
    from ._units import work_unit
    from .test_team_drive import DriveBase
    from .test_team_review import CATALOG, CERTIFIED
except ImportError:  # discovered as a top level module
    from _integration import PY, git
    from _units import work_unit
    from test_team_drive import DriveBase
    from test_team_review import CATALOG, CERTIFIED

from openclaw_ecc_orchestrator.cli.output import render_human


class V5Tests(DriveBase):
    def make_modes(self, units, modes):
        """A drive whose dispatch uses the next runner mode for each attempt of a unit."""
        drive = self.make(units)
        queue = {uid: list(m) for uid, m in modes.items()}

        def dispatch(uid):
            self.dispatched.append(uid)
            files = next(u for u in units if u["id"] == uid)["scope"]["files"]
            res = self.c.assign_unit("r1", uid)
            if not res["ok"]:
                return res
            mode = queue[uid].pop(0) if len(queue.get(uid) or []) > 1 else (queue.get(uid) or ["write"])[0]
            self.c.start_unit("r1", uid, self.argv(mode, *[f"{f}={uid}" for f in files]))
            return self.c.wait_unit("r1", uid)

        drive.dispatch = dispatch
        return drive

    def test_blocked_handoff_is_retried_once_then_the_unit_proceeds(self):
        """A unit that reports blocked gets one delivery-lead retry with a brief instead of stopping the drive."""
        drive = self.make_modes(self.units2(), {"alpha": ["blocked", "write"]})
        res = drive.go()
        self.assertTrue(res["ok"], json.dumps(res, indent=1, default=str)[:3000])
        alpha = self.m.load("r1")["units"]["alpha"]
        self.assertEqual(alpha["state"], "merged")
        self.assertEqual(alpha["annotations"]["budget_grant"]["by"], "delivery_lead")
        self.assertEqual(self.dispatched.count("alpha"), 2)

    def test_repeated_block_stops_with_exact_next_steps(self):
        drive = self.make_modes(self.units2(), {"alpha": ["blocked"]})
        res = drive.go()
        self.assertFalse(res["ok"])
        self.assertEqual(res["data"]["stop"], "needs_owner")
        steps = res["data"]["next_steps"]
        self.assertIn("ecc resume --run-id r1 --unit alpha --extra-attempts 1", steps[0])
        self.assertEqual(steps[-1], "ecc drive --run-id r1")
        out = []

        class Stream:
            def write(self, text):
                out.append(text)

            def flush(self):
                pass

        render_human(res, Stream())
        self.assertIn("  next: ecc drive --run-id r1\n", "".join(out))

    def test_retry_merges_main_into_the_unit_branch(self):
        """Before a repair attempt ECC merges main's new commits into the unit's existing branch."""
        drive = self.make_modes(self.units2(), {"alpha": ["fail", "write"]})
        state = {"done": False}
        original = self.c.start_unit

        def start_unit(run_id, uid, argv, **kw):
            if uid == "alpha" and not state["done"] and self.m.load(run_id)["units"]["alpha"]["attempts"] == 2:
                with open(os.path.join(self.repo, "hotfix.txt"), "w") as fh:
                    fh.write("hotfix")
                git(self.repo, "add", "hotfix.txt")
                git(self.repo, "commit", "-qm", "hotfix on main")
                state["done"] = True
            return original(run_id, uid, argv, **kw)

        self.c.start_unit = start_unit
        res = drive.go()
        self.assertTrue(res["ok"], json.dumps(res, indent=1, default=str)[:3000])
        self.assertTrue(state["done"])
        self.assertEqual(git(self.repo, "show", "ecc/r1/alpha:hotfix.txt"), "hotfix")
        self.assertEqual(git(self.repo, "show", "main:a.txt"), "alpha")


    def test_skills_pass_runs_after_a_non_claude_author_and_is_verified(self):
        """TEAM-DELIVERY v6 4.5: Claude Code's skills run on another author's work before the gates."""
        script = os.path.join(self.tmp, "skills.py")
        seen = {}
        with open(script, "w") as fh:
            fh.write("import subprocess\n"
                     "open('a.txt', 'a').write('simplified\\n')\n"
                     "subprocess.run(['git', 'commit', '-qam', 'Skills pass: simplify'], check=True)\n")

        def argv(prompt, path):
            seen["prompt"] = prompt
            return [PY, script]

        c, m, _ = self.conductor("c1", certified_runners=CERTIFIED, catalog=CATALOG, auto_retry=True,
                                 policy=self.policy, skills_pass={"runner": "claude"}, skills_pass_argv=argv)
        self.ok(c.create_run({"units": [work_unit("alpha", files=["a.txt"], commands=[f"{PY} -c pass"])]},
                             run_id="r1"))
        self.ok(c.assign_unit("r1", "alpha"))
        author = m.load("r1")["units"]["alpha"]["routing"]["runner"]
        self.assertNotEqual(author, "claude")
        self.ok(c.start_unit("r1", "alpha", self.argv("write", "a.txt=alpha")))
        self.ok(c.wait_unit("r1", "alpha"))
        unit = m.load("r1")["units"]["alpha"]
        self.assertEqual(unit["state"], "reviewing")
        record = unit["annotations"]["skills_pass"]
        self.assertEqual((record["author"], record["commits"], record["status"]), (author, 1, "ran"))
        self.assertEqual(unit["annotations"]["verification"]["head"], record["head_after"])
        self.assertIn("/simplify, /verify and /code-review", seen["prompt"])
        self.assertIn("a.txt", seen["prompt"])

    def test_lease_is_kept_through_a_skills_pass_longer_than_the_lease(self):
        """Live finding rm-m0b M0-02: a six minute skills pass outlived the five minute lease and
        every later mutation was refused. The heartbeat keeps (or takes back) the lease."""
        script = os.path.join(self.tmp, "skills.py")
        with open(script, "w") as fh:
            fh.write("import subprocess\n"
                     "open('a.txt', 'a').write('simplified\\n')\n"
                     "subprocess.run(['git', 'commit', '-qam', 'Skills pass: simplify'], check=True)\n")

        def argv(prompt, path):
            self.clock.t += 600  # far past the 60 s test lease
            return [PY, script]

        c, m, _ = self.conductor("c1", certified_runners=CERTIFIED, catalog=CATALOG, auto_retry=True,
                                 policy=self.policy, skills_pass={"runner": "claude"}, skills_pass_argv=argv)
        self.ok(c.create_run({"units": [work_unit("alpha", files=["a.txt"], commands=[f"{PY} -c pass"])]},
                             run_id="r1"))
        self.ok(c.assign_unit("r1", "alpha"))
        self.ok(c.start_unit("r1", "alpha", self.argv("write", "a.txt=alpha")))
        self.ok(c.wait_unit("r1", "alpha"))
        unit = m.load("r1")["units"]["alpha"]
        self.assertEqual(unit["state"], "reviewing")
        self.assertEqual(unit["annotations"]["skills_pass"]["status"], "ran")

    def test_drive_retries_an_interrupted_unit(self):
        """A unit whose runner finished while no drive was processing it (process_gone) is retried,
        not left for the owner."""
        drive = self.make(self.units2())
        self.ok(self.c.assign_unit("r1", "alpha"))
        self.ok(self.m.transition("r1", "c1", "alpha", "interrupted", reason="process_gone"))
        self.clock.t += 600  # the old lease has lapsed too
        res = drive.go()
        self.assertTrue(res["ok"], json.dumps(res, indent=1, default=str)[:3000])
        self.assertEqual(self.m.load("r1")["units"]["alpha"]["state"], "merged")
        self.assertIn("alpha", self.dispatched)

    def test_no_skills_pass_when_claude_wrote_the_unit(self):
        c, m, _ = self.conductor("c1", certified_runners=["claude"], catalog=CATALOG, auto_retry=True,
                                 policy=self.policy, skills_pass={"runner": "claude"},
                                 skills_pass_argv=lambda prompt, path: self.fail("should not run"))
        self.ok(c.create_run({"units": [work_unit("alpha", files=["a.txt"], commands=[f"{PY} -c pass"],
                                                  routing={"initial_tier": 2, "maximum_tier": 2})]}, run_id="r1"))
        self.ok(c.assign_unit("r1", "alpha"))
        self.assertEqual(m.load("r1")["units"]["alpha"]["routing"]["runner"], "claude")
        self.ok(c.start_unit("r1", "alpha", self.argv("write", "a.txt=alpha")))
        self.ok(c.wait_unit("r1", "alpha"))
        self.assertNotIn("skills_pass", m.load("r1")["units"]["alpha"]["annotations"])


if __name__ == "__main__":
    unittest.main()
