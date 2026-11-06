"""Regression tests for acceptance command path extraction in plan lint."""

import unittest

from openclaw_ecc_orchestrator.runs.plan_lint import _command_paths, lint_plan


def plan(commands, scope_files):
    return {
        "units": [
            {
                "id": "unit-a",
                "scope": {"files": scope_files},
                "acceptance": {"commands": commands},
            }
        ]
    }


def command_findings(result):
    return [
        (finding["code"], finding["paths"])
        for finding in result["findings"]
        if finding["code"].startswith("acceptance.command_path")
    ]


class TestPatternOptionsAreNotPaths(unittest.TestCase):
    def test_unittest_discover_pattern_is_not_a_path(self):
        # Blocked plans reported "acceptance command path 'test_iwb.py' is
        # neither in scope nor in the target tree" for this command.
        self.assertEqual(
            _command_paths("python3 -m unittest discover -s tests -p test_iwb.py"),
            ("tests",),
        )

    def test_unittest_pattern_forms_and_top_level_dir(self):
        self.assertEqual(
            _command_paths(
                "PYTHONPATH=src python3.11 -m unittest discover -s tests -t . --pattern=test_cli.py"
            ),
            ("tests", "."),
        )
        self.assertEqual(
            _command_paths("python3 -m unittest discover -s tests -k test_claude_hooks.py"),
            ("tests",),
        )

    def test_pytest_keyword_is_not_a_path(self):
        self.assertEqual(
            _command_paths("python3 -m pytest -k test_guard_docs.py tests"),
            ("tests",),
        )
        self.assertEqual(
            _command_paths("pytest -k test_cli.py tests/test_cli.py"),
            ("tests/test_cli.py",),
        )

    def test_blocked_unittest_commands_lint_clean(self):
        commands = [
            f"python3 -m unittest discover -s tests -p {name}"
            for name in ("test_iwb.py", "test_cli.py", "test_claude_hooks.py", "test_guard_docs.py")
        ]
        result = lint_plan(
            plan(commands, ["tools/iwb.py", "tests/test_iwb.py"]),
            target_paths=["tests/test_cli.py", "tools/iwb.py"],
        )
        self.assertEqual(command_findings(result), [])

    def test_other_modules_keep_their_path_arguments(self):
        self.assertEqual(_command_paths("python -m json.tool -p x.json"), ("x.json",))
        self.assertEqual(
            _command_paths("python3 -m unittest discover -s missing/tests -p test_x.py"),
            ("missing/tests",),
        )


class TestScopeParentDirectoriesArePresent(unittest.TestCase):
    def test_parent_directory_of_scope_file_is_present(self):
        # Blocked plans reported ".claude" and "tools/project-hooks/" missing
        # although the unit's scope adds files below both directories.
        result = lint_plan(
            plan(
                ["ls .claude", "test -d tools/project-hooks/", "ls ./.claude/hooks"],
                [".claude/hooks/check_edit.py", "tools/project-hooks/after-edit.mjs"],
            ),
            target_paths=["README.md"],
        )
        self.assertEqual(command_findings(result), [])

    def test_parent_directory_of_scope_glob_is_present(self):
        result = lint_plan(
            plan(["test -d tools/project-hooks"], ["tools/project-hooks/**/*.mjs"]),
            target_paths=["README.md"],
        )
        self.assertEqual(command_findings(result), [])

    def test_unrelated_and_unsafe_paths_are_still_reported(self):
        result = lint_plan(
            plan(
                ["ls tools/other", "ls .claude/hooks/check", "test -d tools/project", "ls ../.claude"],
                [".claude/hooks/check_edit.py", "tools/project-hooks/after-edit.mjs"],
            ),
            target_paths=["README.md"],
        )
        self.assertEqual(
            sorted(command_findings(result)),
            [
                ("acceptance.command_path_missing", [".claude/hooks/check"]),
                ("acceptance.command_path_missing", ["tools/other"]),
                ("acceptance.command_path_missing", ["tools/project"]),
                ("acceptance.command_path_unsafe", ["../.claude"]),
            ],
        )


if __name__ == "__main__":
    unittest.main()
