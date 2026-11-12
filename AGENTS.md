# Agent guide

This repository is the OpenClaw ECC orchestrator: a Python runtime and
operator CLI (`ecc-orchestrator`) that plans, routes, runs, verifies, reviews
and merges work units in Git worktrees, plus an OpenClaw plugin that shows
its events and relays approval decisions. Start with `README.md` and the
files in `docs/`.

## Layout

- `src/openclaw_ecc_orchestrator/`: the runtime. `cli/` is the operator CLI
  (`cli/main.py` builds the argument parser), `runs/` holds run state and
  the conductor, `routing/`, `runners/`, `worktrees/`, `gates/`,
  `merge_queue/`, `process/`, `handoffs/` and `plugin/` (event log, approval
  broker, decision inbox) hold the rest. `schemas.py` validates the
  versioned documents.
- `schemas/`: JSON Schema copies of the documents in `schemas.py`.
- `fixtures/`: valid and invalid sample documents used by the tests.
- `tests/`: Python tests, including `tests/adversarial/`.
- `integrations/openclaw-plugin/`: the OpenClaw plugin, plain ESM with no
  dependencies and its own `README.md` and Node tests.
- `.kimi-code/`: agent configuration installed by the ECC installer (see
  `.kimi-code/ecc-install-state.json`). It is not part of this project's code;
  do not edit it.

## Setup

Python 3.11 or later. The package has no runtime dependencies
(`dependencies = []` in `pyproject.toml`).

```sh
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
ecc-orchestrator --help
```

Without installing, `PYTHONPATH=src python3.11 -m openclaw_ecc_orchestrator --help`
runs the same CLI.

## Tests

```sh
PYTHONPATH=src python3.11 -m unittest discover -s tests -t .
```

The tests are `unittest.TestCase` classes. They create temporary
directories, set `HOME` to a temporary path where they run the CLI, and use
fake runner scripts in place of the real coding CLIs.
Process liveness checks call `ps` (`runs/liveness.py`); in a sandbox that
blocks `ps`, a few process tests in `tests/test_runs_manager.py` and
`tests/test_runners_cli_flags.py` fail for that reason.

pytest also runs the suite with the settings in `pyproject.toml` once the
optional `test` extra is installed (`python -m pip install -e ".[test]"`).

Plugin tests, from `integrations/openclaw-plugin/` with Node 22 or later:

```sh
npm test
npm run test:contract
```

`npm run test:integration` starts a disposable OpenClaw gateway and needs
`openclaw` installed; run it only when asked.

Before opening a pull request, run `scripts/check.sh`, the one command that
runs the Python tests, the plugin unit tests and the secret scan.

## Conventions

- Standard library only, in both the runtime and the tests.
- Commands return a result envelope built by `runs/envelope.py` (`ok`,
  `operation`, `changed`, `checks`, `warnings`, `required_user_actions`,
  `rollback_checkpoint`). Keep new commands on that shape.
- When a document in `schemas.py` changes, update the matching file in
  `schemas/`; `tests/test_schemas_json.py` checks that they agree.
- `docs/cli.md` writes option names without their leading hyphens and uses
  no double hyphen, em dash or en dash; `tests/test_cli.py` (`DocsTest`)
  enforces this.
- Add a regression test with each bug fix.
- Claude Code hooks (`.claude/settings.json` calling `.claude/hooks/check_edit.py`)
  block writes to `.env*` files, warn on direct `schemas/*.json` edits, and after
  an edit run only the matching `tests/test_<module>*.py`, `DocsTest` for
  `docs/cli.md`, or `npm test` for the plugin; `tests/test_claude_hooks.py` tests them.

## Safety

- Never point the CLI, tests or plugin at real state: `~/.local/state`,
  `~/.local/share`, `~/.openclaw` or a production OpenClaw config. The code
  refuses state inside `.openclaw` or `.git` directories and inside the
  repository (`cli/config.py`).
- Do not run `dispatch` without `--runner-command-json` and
  `--allow-test-runner` unless the user asks: it launches real coding
  runners.
- `merge` and archiving `cleanup` are destructive and need an approval
  through the approval broker; do not work around the confirmation gate.
- Never commit secrets. `.env` files are ignored and `.gitleaks.toml`
  configures secret scanning.

## Review rules

### Approvals

- For CLI plans marked destructive, `Spec.execute` is reached only through
  `cli.main.run_gated` (`src/openclaw_ecc_orchestrator/cli/main.py`), which
  calls `Spec.authorize` before `Spec.execute`. The destructive-capable CLI
  Specs in `GATED` today are `merge` (`authorize=authorize_merge`) and
  `cleanup` (`authorize=authorize_cleanup`) in
  `src/openclaw_ecc_orchestrator/cli/commands.py`.
- Other executors also reach merges: `team/review.py` enqueues and processes
  merges and `team/drive.py` processes the merge queue. For those paths the
  approval is enforced by `MergeQueue.enqueue()`
  (`src/openclaw_ecc_orchestrator/merge_queue/queue.py`), which consumes a
  bound broker approval via `ApprovalBroker.consume()` before the merge runs.
- `ApprovalBroker.usable()` (`src/openclaw_ecc_orchestrator/plugin/approvals.py`)
  only validates a request and never consumes it. Consumption happens through
  `ApprovalBroker.consume()`, which `authorize_cleanup` calls directly and
  which `MergeQueue.enqueue()` calls on the merge path reached from
  `execute_merge`'s enqueue.
- `authorize_cleanup` in `cli/commands.py` admits a destructive cleanup
  through one of two paths: `ApprovalBroker.consume()` with `plan_sha256`
  equal to the plan digest `run_gated` computed over `Plan.doc`, or an
  interactive operator confirmation on a TTY. `authorize_merge` admits a
  fresh merge by calling `ApprovalBroker.usable()` with `run_id`, `unit_id`,
  `action='merge'`, `plan_sha256=doc['run_plan_sha256']`, `session_id` and
  `head_sha`; a unit already in `QUEUED_FOR_MERGE` skips the fresh check
  because its approval was consumed at enqueue time.
- `ApprovalBroker.usable()` and `ApprovalBroker.consume()` validate the
  binding keyword arguments the caller supplies (via `_usable_checks`)
  against the stored approval request, so an approval scoped to one
  `run_id`, `unit_id`, `action` and `session_id` is not admitted for another.
  `DecisionInbox` (`src/openclaw_ecc_orchestrator/plugin/inbox.py`) reads a
  shared inbox directory and delegates binding validation of each decision
  file to `ApprovalBroker._decision_checks`.
- Worktree and state paths are checked by `cli.config.checked_path`,
  `cli.config.forbidden_location` and `cli.config.check_state_outside_repo`
  (`src/openclaw_ecc_orchestrator/cli/config.py`), and the worktree root
  additionally by `worktrees.manager.validate_root`
  (`src/openclaw_ecc_orchestrator/worktrees/manager.py`). These refuse paths
  inside `.openclaw`, `.git` or the repository.

### Envelopes

- Every mutating command result is built with `runs.envelope.envelope()`
  (`src/openclaw_ecc_orchestrator/runs/envelope.py`).
- `envelope()` sets these top-level fields on every result: `ok`,
  `operation`, `changed`, `checks`, `warnings`, `required_user_actions`,
  `rollback_checkpoint`. An operation specific payload lives under an
  optional `data` key.
- `checks`, `warnings` and `required_user_actions` are always lists.
- A successful destructive merge returns a `rollback_checkpoint` (set by
  `merge_queue/queue.py` and passed through by `execute_merge` in
  `cli/commands.py`). A successful destructive cleanup returns a
  `rollback_checkpoint` (set by `WorktreeManager.cleanup()` in
  `src/openclaw_ecc_orchestrator/worktrees/manager.py`, which
  `execute_cleanup` delegates to).
