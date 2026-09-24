"""Escalation state machine.

Rules:

* Start at ``classification.chosen_tier`` (the lowest allowed tier).
* Per tier: one implementation attempt plus one repair (``budget.attempts``
  per tier, capped at 2).
* Escalate one tier only after an objective failure (a failed gate or test),
  never on a model's opinion. A non-objective failure may be repaired but
  never escalates.
* Projected cost rule: when another attempt at the current tier would bring
  that tier's spend above the cost of one attempt at the next tier, and the
  last failure was objective, escalate instead of retrying cheaply.
* Stop at ``maximum_tier``, when the cost or time budget is exhausted, or
  when the next attempt's projected cost does not fit the remaining budget.
* Every attempt and escalation is recorded with reason, attempts, elapsed
  time, spend, usage and the target runner/model.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

MAX_ATTEMPTS_PER_TIER = 2
_TERMINAL = ("done", "stop")


@dataclass
class AttemptOutcome:
    passed: bool
    objective_failure: bool = False
    cost_usd: float = 0.0
    elapsed_seconds: float | None = None
    runner: str | None = None
    provider: str | None = None
    model: str | None = None
    usage: Mapping[str, Any] | None = None
    reason: str = ""


class EscalationController:
    def __init__(self, unit: Mapping[str, Any], classification, *,
                 tier_costs: Mapping[int, float | None] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 policy: Mapping[str, Any] | None = None, breaker=None):
        if classification is None or not classification.ok or classification.chosen_tier is None:
            raise ValueError("escalation requires a valid classification")
        budget = unit["budget"]
        ceilings = (policy or {}).get("budget") or {}
        self.unit_id = unit.get("id")
        self.start_tier = classification.chosen_tier
        self.maximum_tier = classification.maximum_tier
        self.tier = self.start_tier
        self.attempts_per_tier = max(1, min(int(budget["attempts"]), MAX_ATTEMPTS_PER_TIER))
        self.max_cost = float(budget["maximum_cost_usd"])
        if isinstance(ceilings.get("max_cost_usd_per_unit"), (int, float)):
            self.max_cost = min(self.max_cost, float(ceilings["max_cost_usd_per_unit"]))
        self.max_seconds = float(budget["minutes"]) * 60.0
        if isinstance(ceilings.get("max_minutes_per_unit"), (int, float)):
            self.max_seconds = min(self.max_seconds, float(ceilings["max_minutes_per_unit"]) * 60)
        self.tier_costs = dict(tier_costs or {})
        self.clock = clock
        self.breaker = breaker
        self.started_at = clock()
        self._attempt_started = self.started_at
        self.attempts_at_tier = 0
        self.spent = 0.0
        self.spent_at_tier = 0.0
        self.history: list[dict] = []
        self.escalations: list[dict] = []
        self._pending: dict = self._initial_action()

    # ------------------------------------------------------------ helpers
    def _est(self, tier: int) -> float | None:
        value = self.tier_costs.get(tier)
        return float(value) if isinstance(value, (int, float)) else None

    def _remaining(self) -> float:
        return self.max_cost - self.spent

    def _elapsed(self) -> float:
        return self.clock() - self.started_at

    def _fits(self, tier: int) -> bool:
        est = self._est(tier)
        return est is None or est <= self._remaining() + 1e-12

    def _action(self, action: str, reason: str, kind: str | None = None) -> dict:
        return {"action": action, "tier": self.tier, "kind": kind, "reason": reason,
                "attempt": self.attempts_at_tier + (1 if action in ("attempt", "repair",
                                                                    "escalate") else 0)}

    def _initial_action(self) -> dict:
        if not self._fits(self.tier):
            return self._action("stop", "projected cost of a tier %d attempt exceeds the "
                                        "cost budget" % self.tier)
        return self._action("attempt", "start at lowest allowed tier %d" % self.tier,
                            "implementation")

    # ------------------------------------------------------------ public
    @property
    def finished(self) -> bool:
        return self._pending["action"] in _TERMINAL

    def next_action(self) -> dict:
        return dict(self._pending)

    def record(self, outcome: AttemptOutcome) -> dict:
        """Record the attempt that followed the pending action; return the next action."""
        if self.finished:
            raise RuntimeError("escalation already finished: %s" % self._pending["action"])
        if not isinstance(outcome.cost_usd, (int, float)) or outcome.cost_usd < 0:
            raise ValueError("cost_usd must be a non-negative number")
        now = self.clock()
        elapsed = outcome.elapsed_seconds if outcome.elapsed_seconds is not None \
            else now - self._attempt_started
        self._attempt_started = now
        kind = self._pending.get("kind") or "implementation"
        self.attempts_at_tier += 1
        self.spent += float(outcome.cost_usd)
        self.spent_at_tier += float(outcome.cost_usd)
        self.history.append({
            "tier": self.tier, "attempt": self.attempts_at_tier, "kind": kind,
            "runner": outcome.runner, "model": outcome.model, "passed": outcome.passed,
            "objective_failure": outcome.objective_failure, "reason": outcome.reason,
            "cost_usd": float(outcome.cost_usd), "elapsed_seconds": elapsed,
            "usage": dict(outcome.usage) if outcome.usage else None,
        })
        if self.escalations and self.escalations[-1]["target_runner"] is None \
                and self.escalations[-1]["to_tier"] == self.tier:
            self.escalations[-1]["target_runner"] = outcome.runner
            self.escalations[-1]["target_model"] = outcome.model
        provider = outcome.provider or outcome.runner
        if self.breaker is not None and provider:
            if outcome.passed:
                self.breaker.record_success(provider)
            else:
                self.breaker.record_failure(provider)
        self._pending = self._decide(outcome)
        return dict(self._pending)

    def _decide(self, outcome: AttemptOutcome) -> dict:
        if outcome.passed:
            return self._action("done", "attempt passed verification")
        if self._elapsed() >= self.max_seconds:
            return self._action("stop", "time budget exhausted after %.0fs" % self._elapsed())
        if self.spent >= self.max_cost - 1e-12:
            return self._action("stop", "cost budget exhausted (spent %.4f of %.4f USD)"
                                % (self.spent, self.max_cost))
        can_repair = self.attempts_at_tier < self.attempts_per_tier
        can_escalate = outcome.objective_failure and self.tier < self.maximum_tier
        if can_repair:
            here, above = self._est(self.tier), self._est(self.tier + 1)
            if can_escalate and here is not None and above is not None \
                    and self.spent_at_tier + here > above:
                return self._escalate(
                    "projected cost of another tier %d attempt (%.4f total) exceeds one tier "
                    "%d attempt (%.4f)" % (self.tier, self.spent_at_tier + here,
                                           self.tier + 1, above))
            if not self._fits(self.tier):
                return self._action("stop", "projected cost of a repair exceeds the remaining "
                                            "budget")
            return self._action("repair", "repair at tier %d using verification output"
                                % self.tier, "repair")
        if can_escalate:
            return self._escalate("objective failure after %d attempt(s) at tier %d"
                                  % (self.attempts_at_tier, self.tier))
        if outcome.objective_failure:
            return self._action("stop", "maximum tier %d reached without passing"
                                % self.maximum_tier)
        return self._action("stop", "attempts exhausted without an objective failure; "
                                    "escalation not permitted")

    def _escalate(self, reason: str) -> dict:
        target = self.tier + 1
        if not self._fits(target):
            return self._action("stop", "projected cost of a tier %d attempt exceeds the "
                                        "remaining budget" % target)
        usage: dict[str, float] = {}
        for entry in self.history:
            if entry["tier"] == self.tier and entry["usage"]:
                for key, value in entry["usage"].items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        usage[key] = usage.get(key, 0) + value
        self.escalations.append({
            "from_tier": self.tier, "to_tier": target, "reason": reason,
            "attempts": self.attempts_at_tier, "elapsed_seconds": self._elapsed(),
            "spent_usd": round(self.spent, 8), "usage": usage,
            "target_runner": None, "target_model": None,
        })
        self.tier = target
        self.attempts_at_tier = 0
        self.spent_at_tier = 0.0
        return self._action("escalate", reason, "implementation")

    def to_record(self) -> dict:
        return {
            "unit_id": self.unit_id, "start_tier": self.start_tier,
            "maximum_tier": self.maximum_tier, "current_tier": self.tier,
            "attempts_per_tier": self.attempts_per_tier, "spent_usd": round(self.spent, 8),
            "max_cost_usd": self.max_cost, "elapsed_seconds": self._elapsed(),
            "attempts": [dict(h) for h in self.history],
            "escalations": [dict(e) for e in self.escalations],
            "final": dict(self._pending) if self.finished else None,
        }
