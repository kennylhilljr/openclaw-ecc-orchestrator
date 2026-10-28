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
precedence, exit codes, and approval gates.

Planned responsibilities:

- validate ECC-style task DAGs;
- select the lowest-cost adequate healthy model automatically;
- isolate implementation units in Git worktrees;
- launch and supervise native coding runners;
- enforce budgets, tests, review gates, and approvals;
- capture structured handoffs;
- resume interrupted runs;
- predict conflicts and manage a sequential merge queue.

This repository intentionally does not implement chat, identity, session
storage, provider billing, or an independent dashboard. OpenClaw owns those
surfaces, ECC owns workflow doctrine, and Git owns code integration.

The implementation specification and migration ledger live in the private
`agent-control-plane-infra` repository.

