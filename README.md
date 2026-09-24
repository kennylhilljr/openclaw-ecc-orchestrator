# OpenClaw ECC Orchestrator

Thin, provider-aware orchestration plugin for ECC workflows running through
OpenClaw.

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

