import copy
import json
from pathlib import Path
import time

from openclaw_ecc_orchestrator.runs.plan_lint import lint_plan


ROOT = Path(__file__).parents[1]


def fixture():
    return json.loads((ROOT / "fixtures/plans/multi-unit.json").read_text())


def test_representative_plan_is_clean_and_digest_stable():
    plan = fixture()
    policy = {"generated_file_couplings": [{"source": "src/models.py", "generated": "schemas/model.json"}]}
    paths = ["schemas/work_unit.schema.json", "docs/runtime.md", "src/models.py", "schemas/model.json"]
    first = lint_plan(plan, target_paths=reversed(paths), policy=policy)
    second = lint_plan(copy.deepcopy(plan), target_paths=paths, policy=policy)
    assert first == second
    assert first["ok"]
    assert len(first["finding_digest"]) == 64


def test_unordered_overlap_is_slash_sensitive_and_dag_aware():
    plan = fixture()
    plan["units"][1]["depends_on"] = []
    plan["units"][0]["scope"]["files"] = ["src/*"]
    plan["units"][1]["scope"]["files"] = ["src/nested/*"]
    assert not any(
        finding["code"] == "scope.unordered_overlap"
        for finding in lint_plan(plan, target_paths=["src/nested/x"])["findings"]
    )
    plan["units"][0]["scope"]["files"] = ["src/**"]
    assert any(
        finding["code"] == "scope.unordered_overlap"
        for finding in lint_plan(plan, target_paths=["src/nested/x"])["findings"]
    )


def test_unordered_globs_overlap_before_files_exist():
    plan = fixture()
    plan["units"][1]["depends_on"] = []
    plan["units"][0]["scope"]["files"] = ["new/**/*.py"]
    plan["units"][1]["scope"]["files"] = ["new/**/*.py"]
    findings = lint_plan(plan, target_paths=[])["findings"]
    assert any(finding["code"] == "scope.unordered_overlap" for finding in findings)

    plan["units"][0]["scope"]["files"] = ["src/*"]
    plan["units"][1]["scope"]["files"] = ["src/nested/*"]
    findings = lint_plan(plan, target_paths=[])["findings"]
    assert not any(finding["code"] == "scope.unordered_overlap" for finding in findings)


def test_target_acceptance_generated_coupling_and_transitive_requirements():
    plan = fixture()
    plan["units"][0]["scope"]["files"] = ["src/models.py"]
    plan["units"][1]["acceptance"]["paths"] = ["missing/**"]
    plan["units"][1]["requires"] = ["missing-token"]
    result = lint_plan(
        plan,
        target_paths=["schemas/work_unit.schema.json"],
        policy={"generated_file_couplings": [{"source": "src/models.py", "generated": "schemas/model.json"}]},
    )
    assert {finding["code"] for finding in result["findings"]} == {
        "acceptance.path_missing",
        "generated.coupling",
        "requirements.unsatisfied",
    }


def test_exemption_requires_reason_and_suppresses_only_matching_unit():
    plan = fixture()
    plan["units"][1]["requires"] = ["absent"]
    plan["units"][1]["lint_exemptions"] = [
        {"code": "requirements.unsatisfied", "reason": "reviewed legacy boundary"}
    ]
    result = lint_plan(plan, target_paths=["schemas/work_unit.schema.json", "docs/runtime.md"])
    assert result["ok"]
    assert result["exemptions"][0]["reasons"] == ["reviewed legacy boundary"]
    plan["units"][1]["lint_exemptions"][0]["reason"] = ""
    assert any(
        finding["code"] == "exemption.reason_required"
        for finding in lint_plan(plan)["findings"]
    )


def test_acceptance_command_paths_use_scope_and_pinned_target_tree():
    plan = fixture()
    unit = plan["units"][0]
    unit["acceptance"]["commands"] = [
        "python -m pytest tests/test_cli.py::test_help docs/planned.md",
    ]
    unit["scope"]["files"].append("docs/planned.md")
    result = lint_plan(
        plan,
        target_paths=["tests/test_cli.py", "schemas/work_unit.schema.json", "docs/runtime.md"],
    )
    assert not any(
        finding["code"].startswith("acceptance.command_path")
        for finding in result["findings"]
    )


def test_acceptance_command_ignores_executable_and_inline_code():
    plan = fixture()
    plan["units"][0]["acceptance"]["commands"] = [
        "/opt/python/bin/python3 -c \"import pathlib; pathlib.Path('/tmp/result').exists()\"",
        "PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v",
        "npm test -- tests/test_cli.py",
        "go test ./...",
    ]
    result = lint_plan(
        plan,
        target_paths=["tests/test_cli.py", "schemas/work_unit.schema.json", "docs/runtime.md"],
    )
    assert not any(
        finding["code"].startswith("acceptance.command")
        for finding in result["findings"]
    )


def test_acceptance_command_ignores_supported_inline_interpreter_programs():
    plan = fixture()
    plan["units"][0]["acceptance"]["commands"] = [
        "node -e \"require('./src/missing.js')\"",
        "node --eval=\"require('./src/missing.js')\"",
        "node -p \"require('./src/missing.js')\"",
        "node --print=\"require('./src/missing.js')\"",
        "ruby -e \"require './src/missing.rb'\"",
        "perl -E \"require './src/missing.pl'\"",
    ]
    result = lint_plan(plan, target_paths=["schemas/work_unit.schema.json", "docs/runtime.md"])
    assert not any(
        finding["code"].startswith("acceptance.command_path")
        for finding in result["findings"]
    )


def test_acceptance_command_env_wrapper_preserves_inline_program_handling():
    plan = fixture()
    plan["units"][0]["acceptance"]["commands"] = [
        "env node -e \"require('./src/check.js')\"",
        "env CHECK=1 ruby -e \"require './src/check.rb'\"",
        "/usr/bin/env -u UNUSED perl -E \"require './src/check.pl'\"",
    ]
    result = lint_plan(plan, target_paths=["schemas/work_unit.schema.json", "docs/runtime.md"])
    assert not any(
        finding["code"].startswith("acceptance.command_path")
        for finding in result["findings"]
    )


def test_acceptance_command_env_chdir_remains_a_validated_path():
    plan = fixture()
    plan["units"][0]["acceptance"]["commands"] = [
        "env --chdir missing/directory node -e \"require('./src/check.js')\"",
    ]
    result = lint_plan(plan, target_paths=["schemas/work_unit.schema.json", "docs/runtime.md"])
    assert any(
        finding["code"] == "acceptance.command_path_missing"
        and finding["paths"] == ["missing/directory"]
        for finding in result["findings"]
    )


def test_acceptance_command_recognizes_repository_root():
    plan = fixture()
    plan["units"][0]["acceptance"]["commands"] = ["pytest .", "pytest ./"]
    result = lint_plan(plan, target_paths=[])
    assert not any(
        finding["code"].startswith("acceptance.command_path")
        for finding in result["findings"]
    )


def test_acceptance_command_paths_report_missing_and_unsafe_tokens():
    plan = fixture()
    plan["units"][0]["acceptance"]["commands"] = [
        "pytest missing/test_plan.py::test_case ../outside.py::test_case",
    ]
    result = lint_plan(
        plan,
        target_paths=["schemas/work_unit.schema.json", "docs/runtime.md"],
    )
    command_findings = {
        (finding["code"], tuple(finding["paths"]))
        for finding in result["findings"]
        if finding["code"].startswith("acceptance.command_path")
    }
    assert command_findings == {
        ("acceptance.command_path_missing", ("missing/test_plan.py",)),
        ("acceptance.command_path_unsafe", ("../outside.py",)),
    }


def test_repository_path_requirement_can_be_satisfied_by_target_tree():
    plan = fixture()
    plan["units"][1]["requires"] = ["schemas/work_unit.schema.json"]
    result = lint_plan(
        plan,
        target_paths=["schemas/work_unit.schema.json", "docs/runtime.md"],
    )
    assert not any(
        finding["code"] == "requirements.unsatisfied"
        for finding in result["findings"]
    )


def test_unparseable_acceptance_command_is_a_stable_finding():
    plan = fixture()
    plan["units"][0]["acceptance"]["commands"] = ["pytest 'tests/test_plan.py"]
    first = lint_plan(plan, target_paths=["schemas/work_unit.schema.json", "docs/runtime.md"])
    second = lint_plan(copy.deepcopy(plan), target_paths=["docs/runtime.md", "schemas/work_unit.schema.json"])
    assert first == second
    assert any(
        finding["code"] == "acceptance.command_unparseable"
        for finding in first["findings"]
    )


def test_thirty_one_unit_plan_lints_in_seconds():
    template = fixture()["units"][0]
    units = []
    for index in range(31):
        unit = copy.deepcopy(template)
        unit["id"] = f"unit-{index:02d}"
        unit["depends_on"] = []
        unit["scope"]["files"] = [f"src/package_{index:02d}/**"]
        unit["acceptance"] = {"commands": []}
        units.append(unit)
    target_paths = [f"src/package_{index % 31:02d}/file_{index}.py" for index in range(10_000)]
    started = time.monotonic()
    result = lint_plan({"units": units}, target_paths=target_paths)
    assert result["ok"]
    assert time.monotonic() - started < 3.0
