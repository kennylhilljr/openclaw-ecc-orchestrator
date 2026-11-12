---
name: plan-lint
description: Lint a work-unit plan JSON file against this repository's committed HEAD and explain each finding. Use before creating a run or when a plan fails lint-plan.
---

# Plan lint

Runs the same linter as the `lint-plan` CLI command
(`runs.plan_lint.lint_plan`) on a plan file, without creating a run or any
runtime state. The helper never executes the plan's acceptance commands.

## Usage

`$ARGUMENTS` is the path to a plan JSON file: either a list of work units or
an object with a `units` list.

```sh
PYTHONPATH=src python3.11 scripts/plan_lint.py "$ARGUMENTS"
```

The script adds `src/` to its import path itself, so plain
`python3 scripts/plan_lint.py PLAN.json` also works from the repository root
without `PYTHONPATH`; from elsewhere, pass the script's full path.

Every unit must be a valid work unit; a schema error, malformed JSON,
duplicate key or non-finite number is an input error (exit 2).

Optional flags:

- `--repo PATH`: the repository the plan targets (default: the current
  directory). Any path inside the repository works; its Git root is used.
- `--policy FILE`: the repository policy to apply. Without it the helper
  loads `<repo root>/.orchestration/config.yaml` when that file exists and
  lints without a policy otherwise. The policy must be JSON compatible YAML.

## Target paths

Target paths come from the committed tree at HEAD (`git ls-tree -r` on the
pinned commit), matching what `lint_plan` requires. Untracked files,
staged-only files and uncommitted deletions do not count. Commit first if a
finding names a path you just created.

## Reading the result

The helper prints the complete `lint_plan` result as JSON:

- `ok`: true when no active findings remain.
- `findings`: sorted list of `{code, message, unit_ids, paths}`. Codes are
  grouped by prefix, for example `dag.*` (dependency graph errors),
  `scope.unordered_overlap` (two units without an ordering edit the same
  files), `acceptance.path_missing`, `generated.coupling` (a unit edits a
  coupling source from the policy's `generated_file_couplings` without its
  generated file) and `requirements.unsatisfied`.
- `finding_digest`: SHA-256 over the findings; it is stable for the same
  plan, tree and policy, so it identifies an exact lint outcome.
- `exemptions`: findings suppressed by a unit's `lint_exemptions`, with the
  recorded reasons.

Exit codes: 0 no findings, 1 findings, 2 input, policy or Git error (the
message goes to stderr and nothing is printed to stdout).

Fix findings in the plan rather than adding exemptions; an exemption needs a
reviewed reason.
