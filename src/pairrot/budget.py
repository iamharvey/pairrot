"""Wall clock, token cost, and the per-perspective tool-call ceiling."""

from __future__ import annotations

import threading
import time
from typing import Callable

from pairrot.llm import Completion
from pairrot.models import Budget, TokenUsage


class Clock:
    def __init__(self, elapsed_fn: Callable[[], float] | None = None) -> None:
        self._t0 = time.monotonic()
        self._elapsed_fn = elapsed_fn

    def elapsed(self) -> float:
        if self._elapsed_fn is not None:
            return float(self._elapsed_fn())
        return time.monotonic() - self._t0


class BudgetTracker:
    def __init__(self, budget: Budget, clock: Clock | None = None) -> None:
        self.budget = budget
        self.clock = clock or Clock()
        self.tokens = TokenUsage()
        self.cost = 0.0
        self.exhausted = False
        self.reasons: list[str] = []
        self.error: str | None = None
        self.produced = False
        self._lock = threading.RLock()

    def set_error(self, message: str) -> None:
        with self._lock:
            if not self.error:
                self.error = message

    def mark(self, reason: str) -> None:
        with self._lock:
            self.exhausted = True
            if reason not in self.reasons:
                self.reasons.append(reason)

    def allow_model_call(self) -> bool:
        with self._lock:
            if "费用达到上限" in self.reasons:
                return False
            if self.clock.elapsed() >= self.budget.wall_clock_seconds:
                self.exhausted = True
                if "墙钟达到上限" not in self.reasons:
                    self.reasons.append("墙钟达到上限")
                return False
            return True

    def out_of_time(self) -> bool:
        """For git-only work between model calls. Spending the wall clock there marks the run partial too."""
        with self._lock:
            if self.clock.elapsed() < self.budget.wall_clock_seconds:
                return False
            self.mark("墙钟达到上限")
            return True

    def add_completion(self, completion: Completion) -> bool:
        """Record usage from a call that already happened. False means stop."""
        with self._lock:
            self.produced = True
            self.tokens.prompt_tokens += completion.prompt_tokens
            self.tokens.completion_tokens += completion.completion_tokens
            self.tokens.total = self.tokens.prompt_tokens + self.tokens.completion_tokens
            if completion.cost:
                self.cost += completion.cost
            else:
                used = completion.prompt_tokens + completion.completion_tokens
                self.cost += used / 1000 * self.budget.cost_per_1k_tokens
            cap = self.budget.cost_cap
            if cap is not None and self.cost >= cap:
                self.exhausted = True
                if "费用达到上限" not in self.reasons:
                    self.reasons.append("费用达到上限")
                return False
            if self.clock.elapsed() >= self.budget.wall_clock_seconds:
                self.exhausted = True
                if "墙钟达到上限" not in self.reasons:
                    self.reasons.append("墙钟达到上限")
                return False
            return True
