"""Allowlist first gate command policy (review finding 4) and gate environment
(review finding 7: HOME plus a per unit TMPDIR)."""

import json
import os
import shlex
import sys
import tempfile
import unittest

from openclaw_ecc_orchestrator.gates.runner import check_command_policy, is_protected, parse_command, run_gates
from openclaw_ecc_orchestrator.process.supervisor import Supervisor

PY = shlex.quote(sys.executable)

REFUSED = [
    # shells and wrappers
    'sh -c "cd / && rm -rf x"',
    'bash -c "git push origin main"',
    "/bin/bash script.sh",
    "zsh -c true",
    "dash -c true",
    "fish -c true",
    "env git push",
    "env -i FOO=1 git status",
    "xargs rm",
    "eval git push",
    "exec git status",
    "sudo ls",
    "nohup make",
    "time make test",
    "nice -n 5 make",
    "command git push",
    "busybox rm -rf x",
    "/usr/bin/env python3 -c pass",
    # case insensitive filesystems (macOS APFS default) resolve BASH to bash
    "BASH -c true",
    "Env git push",
    "SUDO ls",
    "RM -rf x",
    "GIT push origin main",
    # macOS developer tool wrappers
    "xcrun git push",
    "sandbox-exec -n no-network git push",
    # recursive rm in every spelling
    "rm -Rf ~/x",
    "rm -rf x",
    "rm -fr x",
    "rm -r x",
    "rm -R x",
    "rm -fR x",
    "rm -vrf x",
    "rm --recursive x",
    "rm --rec x",
    "rm -f -r x",
    "/bin/rm -rf x",
    # git
    "git -c alias.p=push p",
    "git -c core.sshCommand=evil fetch",
    "git push origin main",
    "git --no-pager push",
    "git -C /tmp push",
    "git remote add x y",
    "git config core.hooksPath /tmp",
    "git update-ref refs/heads/main HEAD",
    "git credential fill",
    "git credential-store get",
    "git p",
    "git reset --hard HEAD~1",
    "git clean -fdx",
    "git branch -D main",
    "git worktree remove x",
    "git fetch origin",
    "git submodule update",
    # metacharacters inside an argument
    "make 'x; rm -rf /'",
    "make 'a && b'",
    "make 'a || b'",
    "make 'a | b'",
    "make '$(whoami)'",
    "make '`whoami`'",
    "make 'x > /etc/passwd'",
    "make 'x < in'",
    "make '${HOME}'",
    # other guard rails
    "curl https://example.invalid",
    "npm publish",
    "find . -delete",
    "find . -exec rm {} +",
]

ALLOWED = [
    "python3 -m unittest",
    f"{PY} -c pass",
    f"{PY} -c \"import os,sys; sys.exit(0 if os.path.exists('a') else 1)\"",
    "pytest -q -k 'not slow'",
    "make test",
    "git status --porcelain",
    "git diff --stat HEAD~1",
    "git -C sub log --oneline",
    "git --no-pager log -1",
    "rm -f build.log",
    "rm build.log",
    "npm test",
    "echo '$HOME'",
    "pytest -q tests/x.py",
    "ruff check .",
    "python3 -m pytest -q",
    "go test ./...",
    "cargo test",
]


class PolicyTableTests(unittest.TestCase):
    def test_refused(self):
        for cmd in REFUSED:
            with self.subTest(cmd=cmd):
                try:
                    argv = parse_command(cmd)
                except ValueError:
                    continue  # refused at parse time is also a refusal
                self.assertTrue(check_command_policy(argv), cmd)
                self.assertTrue(is_protected(argv), cmd)

    def test_allowed(self):
        for cmd in ALLOWED:
            with self.subTest(cmd=cmd):
                self.assertIsNone(check_command_policy(parse_command(cmd)), cmd)

    def test_wrapper_allowlist_still_checks_inner_command(self):
        self.assertIsNone(check_command_policy(parse_command("bash scripts/check.sh"), allow_wrappers=["bash"]))
        self.assertTrue(check_command_policy(parse_command('bash -c "a && b"'), allow_wrappers=["bash"]))
        self.assertIsNone(check_command_policy(parse_command("env LANG=C make test"), allow_wrappers=["env"]))
        self.assertTrue(check_command_policy(parse_command("env LANG=C git push"), allow_wrappers=["env"]))
        self.assertTrue(check_command_policy(parse_command("env rm -rf x"), allow_wrappers=["env"]))
        self.assertTrue(check_command_policy(parse_command("sudo make"), allow_wrappers=["env"]))

    def test_reason_is_a_string(self):
        reason = check_command_policy(parse_command('sh -c "x"'))
        self.assertIsInstance(reason, str)
        self.assertIn("sh", reason)


class RunGatesPolicyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.wt = self._tmp.name
        self.sup = Supervisor(base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/home/gate-user",
                                        "UNRELATED_TOKEN": "zzzzzzzz"}, grace=0.3)

    def tearDown(self):
        self._tmp.cleanup()

    def unit(self, cmds):
        return {"id": "u1", "depends_on": [], "scope": {"files": ["x"]}, "acceptance": {"commands": cmds},
                "risk": "low"}

    def test_bypasses_never_execute(self):
        marker = os.path.join(self.wt, "ran")
        cmds = [f'sh -c "touch {marker}"', f"env touch {marker}", f"bash -c 'touch {marker}'"]
        res = run_gates(self.unit(cmds), self.wt, supervisor=self.sup)
        self.assertFalse(res["passed"])
        self.assertEqual({g["status"] for g in res["gates"]}, {"refused"})
        self.assertEqual(len(res["refused"]), 3)
        self.assertFalse(os.path.exists(marker))

    def test_repo_policy_can_allowlist_a_wrapper(self):
        script = os.path.join(self.wt, "check.sh")
        with open(script, "w") as fh:
            fh.write("exit 0\n")
        res = run_gates(self.unit([f"sh {script}"]), self.wt, supervisor=self.sup,
                        repo_checks={"allow_wrappers": ["sh"]})
        self.assertTrue(res["passed"], res)

    def test_gate_env_has_home_and_per_unit_tmpdir(self):
        code = ("import json, os; print(json.dumps({'home': os.environ.get('HOME'), 'tmp': os.environ.get('TMPDIR'),"
                " 'unrelated': 'UNRELATED_TOKEN' in os.environ}))")
        res = run_gates(self.unit([f"{PY} -c {shlex.quote(code)}"]), self.wt, supervisor=self.sup)
        self.assertTrue(res["passed"], res)
        env = json.loads(res["gates"][0]["output_tail"].splitlines()[-1])
        self.assertEqual(env["home"], "/home/gate-user")
        self.assertFalse(env["unrelated"])
        self.assertTrue(env["tmp"])
        self.assertIn("u1", os.path.basename(env["tmp"]))
        self.assertFalse(os.path.exists(env["tmp"]))  # removed after the gates ran
        self.assertNotIn(REDACTED_MARK, res["gates"][0]["output_tail"])

    def test_tmpdir_root_is_configurable(self):
        root = os.path.join(self.wt, "tmproot")
        os.makedirs(root)
        code = "import os; print(os.environ['TMPDIR'])"
        res = run_gates(self.unit([f"{PY} -c {shlex.quote(code)}"]), self.wt, supervisor=self.sup, tmp_root=root)
        self.assertTrue(res["gates"][0]["output_tail"].strip().startswith(root))


REDACTED_MARK = "[REDACTED]"


if __name__ == "__main__":
    unittest.main()
