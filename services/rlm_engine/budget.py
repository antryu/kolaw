"""
RLM Token / Recursion Budget — Phase 3.

Caps token consumption, recursion depth, and sub-LLM call count for a
single trajectory so a misbehaving root LLM cannot fork-bomb the system.
"""

from __future__ import annotations

from dataclasses import dataclass


class BudgetExceeded(Exception):
    """Raised when a token cap or sub-call cap is exceeded."""


class RecursionDepthExceeded(Exception):
    """Raised when sub-LLM recursion goes deeper than max_depth."""


@dataclass
class TokenBudget:
    total_in_cap: int = 200_000
    total_out_cap: int = 20_000
    per_call_out_cap: int = 1024
    max_depth: int = 3
    max_sub_calls: int = 8
    _used_in: int = 0
    _used_out: int = 0
    _sub_calls: int = 0

    def reserve(self, tokens_in_est: int, tokens_out_est: int, depth: int) -> bool:
        """
        Check whether a request fits remaining budgets.

        Returns True if the request can proceed; False otherwise.
        Does NOT consume the budget — caller must call consume() after the
        actual usage is known.
        """
        if depth > self.max_depth:
            return False
        if tokens_out_est > self.per_call_out_cap:
            return False
        if self._used_in + tokens_in_est > self.total_in_cap:
            return False
        if self._used_out + tokens_out_est > self.total_out_cap:
            return False
        return True

    def consume(self, tokens_in: int, tokens_out: int) -> None:
        """
        Record actual usage. Raises BudgetExceeded if either total cap is
        crossed by the consumption (post-hoc enforcement so callers can
        detect overage even when reserve() under-estimated).
        """
        self._used_in += max(0, int(tokens_in))
        self._used_out += max(0, int(tokens_out))
        if self._used_in > self.total_in_cap:
            raise BudgetExceeded(
                f"total_in_cap exceeded: {self._used_in} > {self.total_in_cap}"
            )
        if self._used_out > self.total_out_cap:
            raise BudgetExceeded(
                f"total_out_cap exceeded: {self._used_out} > {self.total_out_cap}"
            )

    def remaining(self) -> dict[str, int]:
        return {
            "in": max(0, self.total_in_cap - self._used_in),
            "out": max(0, self.total_out_cap - self._used_out),
            "sub_calls": max(0, self.max_sub_calls - self._sub_calls),
            "depth": self.max_depth,
        }

    def assert_depth(self, depth: int) -> None:
        """Raise RecursionDepthExceeded if depth exceeds max_depth."""
        if depth > self.max_depth:
            raise RecursionDepthExceeded(
                f"depth {depth} exceeds max_depth {self.max_depth}"
            )

    def increment_sub_call(self) -> None:
        """
        Bump the sub-LLM call counter. Raises BudgetExceeded if the cap
        is reached.
        """
        if self._sub_calls + 1 > self.max_sub_calls:
            raise BudgetExceeded(
                f"max_sub_calls exceeded: {self._sub_calls + 1} > {self.max_sub_calls}"
            )
        self._sub_calls += 1
