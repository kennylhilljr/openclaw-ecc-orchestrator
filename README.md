# OpenClaw ECC Orchestrator

Thin, provider-aware orchestration plugin for ECC workflows running through
OpenClaw.

## Run the CLI locally

Python 3.11 or later is required. The package has no runtime Python
dependencies.

```sh
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
ecc-orchestrator --help
```

See the [CLI operator guide](docs/cli.md) for subcommands, configuration
precedence, exit codes, and approval gates. `python3 -m openclaw_ecc_orchestrator`
runs the same CLI without the console script.

By default the CLI keeps its state under `$XDG_STATE_HOME` or
`~/.local/state/openclaw-ecc-orchestrator` and its worktrees under
`$XDG_DATA_HOME` or `~/.local/share/openclaw-ecc-orchestrator/worktrees`.

## Run the tests

The Python tests use the standard library `unittest` module, so they need no
extra packages:

```sh
PYTHONPATH=src python3.11 -m unittest discover -s tests -t .
```

The OpenClaw plugin has its own Node test suites; see
[integrations/openclaw-plugin/README.md](integrations/openclaw-plugin/README.md#tests).

## Documentation

- [docs/cli.md](docs/cli.md): the operator CLI.
- [docs/runtime.md](docs/runtime.md): run state, worktrees, runner
  supervision, gates, conflict prediction and the merge queue.
- [docs/routing.md](docs/routing.md): document validation, tier, runner and
  reviewer selection, escalation.
- [docs/runners.md](docs/runners.md): CLI and API runners, model discovery,
  certification probes.
- [docs/openclaw-events.md](docs/openclaw-events.md): the event log and
  approval decision contract with OpenClaw.
- [integrations/openclaw-plugin/README.md](integrations/openclaw-plugin/README.md):
  the OpenClaw plugin that tails events and records decisions.

## Responsibilities

Each of these has code under `src/openclaw_ecc_orchestrator/`:

- validate ECC-style task DAGs;
- select the lowest-cost adequate healthy model automatically;
- isolate implementation units in Git worktrees;
- launch and supervise native coding runners;
- enforce budgets, tests, review gates, and approvals;
- capture structured handoffs;
- resume interrupted runs;
- predict conflicts and manage a sequential merge queue.

Resuming, retrying and reassigning units is available through the Python API
only; the CLI has no subcommand for it yet (see the limitations in
[docs/cli.md](docs/cli.md)).

This repository intentionally does not implement chat, identity, session
storage, provider billing, or an independent dashboard. OpenClaw owns those
surfaces, ECC owns workflow doctrine, and Git owns code integration.

The implementation specification and migration ledger live in the private
`agent-control-plane-infra` repository.

