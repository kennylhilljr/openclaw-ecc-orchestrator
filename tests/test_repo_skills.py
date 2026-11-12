"""The repository skills in .claude/skills/ stay accurate as the code changes."""

import importlib
import pathlib
import re
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

commands = importlib.import_module("openclaw_ecc_orchestrator.cli.commands")
cli_main = importlib.import_module("openclaw_ecc_orchestrator.cli.main")

SKILLS_DIR = REPO_ROOT / ".claude" / "skills"

# skill name -> (files it must reference, exact commands it must document)
EXPECTED = {
    "plan-lint": (
        ["scripts/plan_lint.py"],
        ['PYTHONPATH=src python3.11 scripts/plan_lint.py "$ARGUMENTS"'],
    ),
    "routing-trace": (
        ["scripts/routing_trace.py", "src/openclaw_ecc_orchestrator/routing/classify.py"],
        ['PYTHONPATH=src python3.11 scripts/routing_trace.py "$ARGUMENTS"'],
    ),
    "sync-schemas": (
        ["scripts/export_schemas.py", "src/openclaw_ecc_orchestrator/schemas.py"],
        ["PYTHONPATH=src python3.11 scripts/export_schemas.py",
         "PYTHONPATH=src python3.11 scripts/export_schemas.py --check",
         "PYTHONPATH=src python3.11 -m unittest tests.test_schemas_json"],
    ),
    "new-command": (
        ["src/openclaw_ecc_orchestrator/__main__.py", "src/openclaw_ecc_orchestrator/cli/main.py",
         "src/openclaw_ecc_orchestrator/cli/commands.py",
         "src/openclaw_ecc_orchestrator/runs/envelope.py", "docs/cli.md",
         "tests/test_cli_support.py", "tests/test_cli.py", "tests/test_cli_adversarial.py",
         "scripts/export_schemas.py"],
        ["PYTHONPATH=src python3.11 -m openclaw_ecc_orchestrator --help",
         "PYTHONPATH=src python3.11 -m unittest tests.test_cli tests.test_cli_adversarial",
         "PYTHONPATH=src python3.11 scripts/export_schemas.py"],
    ),
}

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)
# Backticked repository paths such as `scripts/plan_lint.py` or `docs/cli.md`.
_PATH_RE = re.compile(r"`((?:src|scripts|tests|docs|fixtures|schemas)/[A-Za-z0-9_./-]*)`")


def read_skill(name):
    return (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")


def frontmatter(text):
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None
    fields = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def commands_in(text):
    """Lines inside fenced code blocks, stripped."""
    lines, inside = [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            inside = not inside
            continue
        if inside:
            lines.append(line.strip())
    return lines


class RepoSkillsTest(unittest.TestCase):
    def test_exactly_the_expected_skills_exist(self):
        found = {p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md")}
        self.assertEqual(found, set(EXPECTED))

    def test_frontmatter_names_match_directories(self):
        for name in EXPECTED:
            with self.subTest(skill=name):
                fields = frontmatter(read_skill(name))
                self.assertIsNotNone(fields, "missing frontmatter")
                self.assertEqual(fields.get("name"), name)
                self.assertTrue(fields.get("description"), "missing description")

    def test_required_references_and_commands(self):
        for name, (paths, cmds) in EXPECTED.items():
            text = read_skill(name)
            code = commands_in(text)
            for path in paths:
                with self.subTest(skill=name, path=path):
                    self.assertIn(path, text)
            for cmd in cmds:
                with self.subTest(skill=name, command=cmd):
                    self.assertIn(cmd, code)

    def test_referenced_repository_files_exist(self):
        for name in EXPECTED:
            text = read_skill(name)
            refs = set(_PATH_RE.findall(text))
            self.assertTrue(refs, name)
            for ref in sorted(refs):
                with self.subTest(skill=name, path=ref):
                    self.assertTrue((REPO_ROOT / ref.rstrip("/")).exists(), ref)

    def test_documented_scripts_and_test_modules_exist(self):
        for name in EXPECTED:
            for line in commands_in(read_skill(name)):
                for script in re.findall(r"scripts/[A-Za-z0-9_]+\.py", line):
                    with self.subTest(skill=name, script=script):
                        self.assertTrue((REPO_ROOT / script).is_file())
                for module in re.findall(r"\btests\.[A-Za-z0-9_.]+", line):
                    with self.subTest(skill=name, module=module):
                        top = module.split(".")[:2]
                        self.assertTrue((REPO_ROOT / "/".join(top)).with_suffix(".py").is_file(),
                                        module)

    def test_new_command_names_real_functions(self):
        text = read_skill("new-command")
        for function in ("plan_create_run", "execute_create_run", "plan_merge",
                         "authorize_merge", "execute_merge"):
            with self.subTest(function=function):
                self.assertIn(function, text)
                self.assertTrue(callable(getattr(commands, function, None)), function)
        for symbol in ("GATED", "Spec"):
            self.assertIn(symbol, text)
            self.assertTrue(hasattr(commands, symbol), symbol)
        for function in ("run_gated", "_common", "_mutating", "build_parser"):
            with self.subTest(function=function):
                self.assertIn(function, text)
                self.assertTrue(callable(getattr(cli_main, function, None)), function)
        for test_class in ("GateTest", "DryRunTest", "DocsTest", "CliTestBase"):
            self.assertIn(test_class, text)
        cli_tests = (REPO_ROOT / "tests" / "test_cli.py").read_text(encoding="utf-8")
        support = (REPO_ROOT / "tests" / "test_cli_support.py").read_text(encoding="utf-8")
        for test_class in ("GateTest", "DryRunTest", "DocsTest"):
            self.assertIn("class %s(" % test_class, cli_tests)
        self.assertIn("class CliTestBase(", support)


if __name__ == "__main__":
    unittest.main()
