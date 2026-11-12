---
name: new-command
description: Add a new gated (mutating) subcommand to the ecc-orchestrator CLI with plan, execute, optional authorize, docs and tests. Use when adding or changing a CLI command that changes state.
---

# New gated command

Gated commands (the `GATED` table in `cli/commands.py`, for example
`create-run`, `dispatch` and `merge`) go through one shared gate, `run_gated`
in `src/openclaw_ecc_orchestrator/cli/main.py`. A few operator commands such
as `review-plan`, `approvals process-inbox` and `catalog-refresh` have their own
handlers; follow this guide when the new command should be gated. It handles dry
run, reviewed-plan confirmation, destructive authorization, the run lease
and the stale-plan check. A new command only supplies a plan and an
executor; do not add a second confirmation path.

The CLI entrypoint is `src/openclaw_ecc_orchestrator/__main__.py`, which
calls `openclaw_ecc_orchestrator.cli.main`.

## Steps

1. **Plan.** In `src/openclaw_ecc_orchestrator/cli/commands.py` write
   `plan_<name>(ctx)`. It is read only and returns a `Plan` whose `doc` is a
   deterministic JSON document (no timestamps, no random ids) describing
   exactly what will happen. Its digest is the plan hash that reviews and
   approvals bind to, and `run_gated` rebuilds the plan before executing and
   refuses if the hash changed. Model: `plan_create_run`.
2. **Execute.** Write `execute_<name>(ctx, plan)`. It performs the plan
   through the runtime's public APIs and returns a result envelope built with
   `envelope` and `check` from `src/openclaw_ecc_orchestrator/runs/envelope.py`
   (`ok`, `operation`, `changed`, `checks`, `warnings`,
   `required_user_actions`, `rollback_checkpoint`). Model:
   `execute_create_run`.
3. **Authorize (destructive plans only).** When the plan is destructive, add
   `authorize_<name>(ctx, plan, digest)`. It admits the plan through the
   ApprovalBroker or an interactive confirmation and never through the yes
   flag; it returns `(checks, refusal)`. Model: `plan_merge`,
   `authorize_merge` and `execute_merge`.
4. **Register.** Add a `Spec(plan_<name>, execute_<name>)` entry to `GATED`
   in `cli/commands.py`, passing `authorize=` for destructive plans and
   `lease=True` when the command touches a run that the conductor
   serializes (the plan doc must then carry `run_id`).
5. **Parser.** In `build_parser` in `cli/main.py`, add the subparser and call
   `_common(p)` and `_mutating(p)` on it so it gets the configuration,
   output and safety options. The dispatcher sends every `GATED` operation to
   `run_gated`; no extra wiring is needed.
6. **Docs.** Add the command to `docs/cli.md`. That file writes option names
   without their leading hyphens and uses no double hyphen, em dash or en
   dash; `DocsTest` in `tests/test_cli.py` enforces this.
7. **Fixtures.** Put sample inputs under `fixtures/` (for example
   `fixtures/plans/` or `fixtures/work_units/`) rather than inline in many
   tests.
8. **Tests.** Build on the harness in `tests/test_cli_support.py`
   (`CliTestBase`). Cover, next to `GateTest` and `DryRunTest` in
   `tests/test_cli.py`: the dry run changes nothing, a non TTY run without a
   review is refused, a reviewed plan runs, a plan that changed after review
   is refused as stale, and for destructive plans that the yes flag alone is
   refused. Add hostile inputs to `tests/test_cli_adversarial.py`.
9. **Schemas.** If the command adds or changes a versioned document, change
   `schemas.py`, then regenerate with `scripts/export_schemas.py`:

   ```sh
   PYTHONPATH=src python3.11 scripts/export_schemas.py
   ```

## Check

```sh
PYTHONPATH=src python3.11 -m openclaw_ecc_orchestrator --help
PYTHONPATH=src python3.11 -m unittest tests.test_cli tests.test_cli_adversarial
```

The new subcommand must appear in the help output and both suites must pass.
