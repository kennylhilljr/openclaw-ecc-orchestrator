"""Per provider circuit breaker with an injected clock.

``failure_threshold`` consecutive failures open the breaker for
``cooldown_seconds``. After the cooldown the breaker is half open: one
trial is allowed; a failure re-opens it immediately, a success closes it.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping

DEFAULT_THRESHOLD = 3
DEFAULT_COOLDOWN = 900.0


class CircuitBreaker:
    def __init__(self, failure_threshold: int = DEFAULT_THRESHOLD,
                 cooldown_seconds: float = DEFAULT_COOLDOWN,
                 clock: Callable[[], float] = time.monotonic):
        if not isinstance(failure_threshold, int) or failure_threshold < 1:
            raise ValueError("failure_threshold must be an integer >= 1")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be >= 0")
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.clock = clock
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}

    @classmethod
    def from_policy(cls, policy: Mapping[str, Any] | None,
                    clock: Callable[[], float] = time.monotonic) -> "CircuitBreaker":
        cfg = (policy or {}).get("circuit_breaker") or {}
        return cls(cfg.get("failure_threshold", DEFAULT_THRESHOLD),
                   cfg.get("cooldown_seconds", DEFAULT_COOLDOWN), clock)

    @staticmethod
    def _key(provider: str) -> str:
        return str(provider).strip().lower()

    def state(self, provider: str) -> str:
        provider = self._key(provider)
        opened = self._opened_at.get(provider)
        if opened is None:
            return "closed"
        if self.clock() - opened >= self.cooldown_seconds:
            return "half_open"
        return "open"

    def is_open(self, provider: str) -> bool:
        return self.state(provider) == "open"

    def record_failure(self, provider: str) -> None:
        provider = self._key(provider)
        if self.state(provider) == "half_open":
            self._opened_at[provider] = self.clock()
            return
        count = self._failures.get(provider, 0) + 1
        self._failures[provider] = count
        if count >= self.failure_threshold:
            self._opened_at[provider] = self.clock()

    def record_success(self, provider: str) -> None:
        provider = self._key(provider)
        self._failures[provider] = 0
        self._opened_at.pop(provider, None)

    def open_providers(self) -> list[str]:
        return sorted(p for p in self._opened_at if self.is_open(p))
