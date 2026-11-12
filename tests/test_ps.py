"""The cached `ps` probe and the tests it guards."""

import ast
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

try:
    from . import _ps
except ImportError:  # discovered as a top level module
    import _ps

TESTS = Path(__file__).resolve().parent
REASON = "ps is not permitted in this sandbox"
GUARDED = {
    "test_process_health": {("SignalSourceTests", "test_real_group_snapshot_sees_self_group"),
                            ("RealProcessTests", "test_sleeping_silent_child_becomes_hung")},
    "test_conductor_health": {("ConductorHealthTests", "test_hung_gate_fails_verification_with_events"),
                              ("ConductorHealthTests", "test_hung_runner_is_cancelled_and_failed_with_reason_hung")},
    "test_gates": {("GateTests", "test_hung_gate_is_cancelled_by_health_checks")},
    "test_runners_cli_flags": {("DefaultCancelRunnerTests", "test_kills_grandchildren_and_skips_non_event_lines")},
    "test_runs_manager": {("ResumeTests", "test_resume_marks_dead_process_interrupted_and_keeps_live")},
}


def is_ps_guard(node):
    """True when a decorator is unittest.skipUnless(ps_permitted(), REASON)."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "skipUnless"
            and isinstance(func.value, ast.Name) and func.value.id == "unittest"):
        return False
    if len(node.args) != 2 or node.keywords:
        return False
    cond, reason = node.args
    return (isinstance(cond, ast.Call) and isinstance(cond.func, ast.Name) and cond.func.id == "ps_permitted"
            and not cond.args and not cond.keywords
            and isinstance(reason, ast.Constant) and reason.value == REASON)


def mentions_ps_permitted(node):
    return any(isinstance(n, ast.Name) and n.id == "ps_permitted" for n in ast.walk(node))


class ProbeTests(unittest.TestCase):
    def setUp(self):
        _ps.ps_permitted.cache_clear()
        self.addCleanup(_ps.ps_permitted.cache_clear)

    def probe(self, **kw):
        with mock.patch.object(_ps.subprocess, "run", **kw) as run:
            return _ps.ps_permitted(), run

    def test_probe_runs_ps_for_this_process(self):
        ok, run = self.probe(return_value=subprocess.CompletedProcess([], 0, b"", b""))
        self.assertTrue(ok)
        run.assert_called_once_with(["ps", "-o", "pid=", "-p", str(os.getpid())],
                                    capture_output=True, check=False)

    def test_nonzero_exit_is_not_permitted(self):
        ok, _ = self.probe(return_value=subprocess.CompletedProcess([], 1, b"", b"denied"))
        self.assertFalse(ok)

    def test_permission_error_is_not_permitted(self):
        ok, _ = self.probe(side_effect=PermissionError("Operation not permitted"))
        self.assertFalse(ok)

    def test_other_os_error_is_not_permitted(self):
        ok, _ = self.probe(side_effect=OSError("no such file"))
        self.assertFalse(ok)

    def test_success_is_cached(self):
        with mock.patch.object(_ps.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, b"", b"")) as run:
            self.assertTrue(_ps.ps_permitted())
            self.assertTrue(_ps.ps_permitted())
        self.assertEqual(run.call_count, 1)

    def test_failure_is_cached(self):
        with mock.patch.object(_ps.subprocess, "run", side_effect=PermissionError("denied")) as run:
            self.assertFalse(_ps.ps_permitted())
            self.assertFalse(_ps.ps_permitted())
        self.assertEqual(run.call_count, 1)


class GuardPlacementTests(unittest.TestCase):
    def test_exactly_the_process_tests_are_guarded(self):
        for module, expected in GUARDED.items():
            tree = ast.parse((TESTS / f"{module}.py").read_text(), filename=f"{module}.py")
            guarded = set()
            for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
                self.assertFalse(any(mentions_ps_permitted(d) for d in cls.decorator_list),
                                 f"{module}.{cls.name} has a class level ps guard")
                for fn in cls.body:
                    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    if any(is_ps_guard(d) for d in fn.decorator_list):
                        guarded.add((cls.name, fn.name))
                    else:
                        self.assertFalse(any(mentions_ps_permitted(d) for d in fn.decorator_list),
                                         f"{module}.{cls.name}.{fn.name} has a malformed ps guard")
            self.assertEqual(guarded, expected, module)


if __name__ == "__main__":
    unittest.main()
