import os
import shlex
import sys
import tempfile
import unittest

from openclaw_ecc_orchestrator.gates.runner import is_protected, parse_command, run_gates
from openclaw_ecc_orchestrator.process.supervisor import Supervisor

PY = shlex.quote(sys.executable)


def unit(cmds, uid="u1"):
    return {"id": uid, "depends_on": [], "scope": {"files": ["src/x.py"]}, "acceptance": {"commands": list(cmds)},
            "risk": "low"}


class GateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.wt = self._tmp.name
        self.sup = Supervisor(base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent"},
                              grace=0.3)

    def tearDown(self):
        self._tmp.cleanup()

    def gates(self, cmds, repo_checks=None, **kw):
        return run_gates(unit(cmds), self.wt, repo_checks=repo_checks, supervisor=self.sup, **kw)

    def test_all_pass(self):
        res = self.gates([f"{PY} -c 'print(1)'"],
                         repo_checks={"required": ["unit"], "commands": {"unit": f"{PY} -c 'print(2)'"}})
        self.assertTrue(res["passed"], res)
        self.assertEqual(res["schema_version"], "1.0")
        self.assertEqual([g["source"] for g in res["gates"]], ["acceptance", "required_check"])
        for g in res["gates"]:
            self.assertIsInstance(g["argv"], list)
            self.assertEqual(g["exit_code"], 0)
            self.assertIn("duration_s", g)
            self.assertEqual(g["status"], "passed")

    def test_one_failure_fails_overall_and_all_run(self):
        res = self.gates([f"{PY} -c 'import sys; sys.exit(2)'", f"{PY} -c 'print(3)'"])
        self.assertFalse(res["passed"])
        self.assertEqual([g["status"] for g in res["gates"]], ["failed", "passed"])
        self.assertEqual(res["gates"][0]["exit_code"], 2)

    def test_missing_required_check_fails(self):
        res = self.gates([f"{PY} -c pass"], repo_checks={"required": ["lint"], "commands": {}})
        self.assertFalse(res["passed"])
        self.assertEqual(res["missing_required"], ["lint"])
        self.assertEqual(res["gates"][-1]["status"], "missing")

    def test_protected_command_refused_before_execution(self):
        marker = os.path.join(self.wt, "ran")
        res = self.gates([f"{PY} -c \"open('ran','w')\""], protected_globs=["*open(*"])
        self.assertFalse(res["passed"])
        self.assertEqual(res["gates"][0]["status"], "refused")
        self.assertFalse(os.path.exists(marker))

    def test_default_protected_commands(self):
        for cmd in ["git push origin main", "/usr/bin/git push", "git reset --hard HEAD~1", "rm -rf /", "sudo ls",
                    "git push --force"]:
            self.assertTrue(is_protected(parse_command(cmd)), cmd)
        self.assertFalse(is_protected(parse_command("python3 -m unittest")))

    def test_no_shell_expansion(self):
        out = self.gates([f"{PY} -c 'import sys; print(sys.argv[1:])' 'a b' '$HOME'"])
        self.assertTrue(out["passed"], out)
        self.assertIn("['a b', '$HOME']", out["gates"][0]["output_tail"])

    def test_shell_operators_rejected(self):
        marker = os.path.join(self.wt, "x")
        res = self.gates([f"{PY} -c pass && touch {marker}"])
        self.assertFalse(res["passed"])
        self.assertEqual(res["gates"][0]["status"], "invalid")
        self.assertFalse(os.path.exists(marker))

    def test_unparsable_command(self):
        res = self.gates([f"{PY} -c 'unterminated"])
        self.assertFalse(res["passed"])
        self.assertEqual(res["gates"][0]["status"], "invalid")

    def test_output_tail_truncated_and_redacted(self):
        code = "print('x' * 10000); print('token=abcdef123456')"  # gitleaks:allow
        res = self.gates([f"{PY} -c \"{code}\""], tail_chars=500)
        tail = res["gates"][0]["output_tail"]
        self.assertLessEqual(len(tail), 500)
        self.assertNotIn("abcdef123456", tail)
        self.assertIn("[REDACTED]", tail)

    def test_warnings_alone_do_not_pass(self):
        res = self.gates([f"{PY} -c \"print('WARNING: minor'); raise SystemExit(1)\""])
        self.assertFalse(res["passed"])

    def test_empty_acceptance_does_not_pass(self):
        res = self.gates([])
        self.assertFalse(res["passed"])

    def test_timeout_fails(self):
        res = self.gates([f"{PY} -c 'import time; time.sleep(30)'"], timeout=0.5)
        self.assertFalse(res["passed"])
        self.assertTrue(res["gates"][0]["timed_out"])
        self.assertEqual(res["gates"][0]["status"], "timeout")

    def test_log_dir(self):
        logs = os.path.join(self.wt, "..", os.path.basename(self.wt) + "-logs")
        try:
            res = self.gates([f"{PY} -c 'print(5)'"], log_dir=logs)
            self.assertTrue(os.path.isfile(res["gates"][0]["log_path"]))
        finally:
            import shutil
            shutil.rmtree(logs, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
