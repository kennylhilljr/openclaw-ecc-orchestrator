---
name: routing-trace
description: Explain which tier the router picks for a work unit or plan and why, signal by signal. Use when a unit routes to an unexpected tier or before tuning risk and tiers.
---

# Routing trace

Runs `routing.classify.classify` on each unit and prints the full
classification, so you can see exactly which signals fired and which floor or
cap decided the tier. The script only reports; all scoring rules live in
`src/openclaw_ecc_orchestrator/routing/classify.py`.

## Usage

`$ARGUMENTS` is a JSON file holding one work unit, a list of units, or an
object with a `units` list.

```sh
PYTHONPATH=src python3.11 scripts/routing_trace.py "$ARGUMENTS"
```

The script bootstraps `src/` itself, so `python3 scripts/routing_trace.py
FILE` works from the repository root without `PYTHONPATH`; from elsewhere,
pass the script's full path.

Policy options:

- `--repo PATH`: repository whose policy applies (default: the current
  directory; its Git root is used).
- `--policy FILE`: explicit repository policy. Without it the script loads
  `<repo root>/.orchestration/config.yaml` when present and classifies
  without a policy otherwise. A policy's `high_risk_paths` can force a unit's
  risk to high.

## Declared scope only

The trace classifies the unit as declared: `scope.files`, traits, risk,
capabilities and routing. It has no diff statistics, so signals that depend
on a real diff (`large_diff`, and file counts taken from changed files) do
not fire here even though they may during a run. Treat the trace as the
starting tier, not the final one after escalation.

## Reading the trace

Output: `{"ok": ..., "classifications": [...]}`, one entry per unit, each a
complete `Classification.to_dict()` plus the declared `initial_tier`:

- `signals`: every triggered signal with `name`, `points` and `reason`.
- `score` and `score_tier`: the summed points and the tier they map to
  (score <= 0 is tier 0, 1..5 tier 1, >= 6 tier 2).
- `declared_risk`, `risk` and `risk_minimum_tier`: the risk in the unit, the
  effective risk after `high_risk_paths`, and the floor it sets.
- `minimum_tier`: the larger of the risk floor and `initial_tier`.
- `maximum_tier`: the unit's cap; `reasons` says when the score was capped.
- `chosen_tier` and `review_tier`: the starting tier and the review tier.
- `errors`: why a classification is invalid, for example a schema error or a
  risk floor above `maximum_tier`.

To lower a tier, change the unit (scope, traits, risk), not the classifier.

Exit codes: 0 every classification valid, 1 any invalid classification, 2
malformed input, unsupported container, unreadable file or policy error.
