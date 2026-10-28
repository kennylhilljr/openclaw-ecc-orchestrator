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

Plugin tests, from `integrations/openclaw-plugin/` with Node 22 or later:

```sh
npm test
npm run test:contract
```

`npm run test:integration` starts a disposable OpenClaw gateway and needs
`openclaw` installed; run it only when asked.

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
