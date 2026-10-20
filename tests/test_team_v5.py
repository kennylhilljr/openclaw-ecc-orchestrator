"""TEAM-DELIVERY v5: branches stay current, the drive heals before it stops,
and every stop says exactly what to run next (live findings, rmcp-m0f)."""

import json
import os
import unittest

try:
    from ._integration import git
    from .test_team_drive import DriveBase
except ImportError:  # discovered as a top level module
    from _integration import git
    from test_team_drive import DriveBase

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


if __name__ == "__main__":
    unittest.main()
