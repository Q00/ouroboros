"""Shared runtime rate-limit coordination for orchestrator workers."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
import time

RATE_LIMIT_WINDOW_SECONDS = 60.0
RATE_LIMIT_HEARTBEAT_SECONDS = 30.0
RATE_LIMIT_MAX_WAIT_SECONDS = 120.0
DEFAULT_ANTHROPIC_RPM_CEILING = 40
DEFAULT_ANTHROPIC_TPM_CEILING = 32_000
_TOKEN_ESTIMATE_DIVISOR = 4
_TOKEN_COMPLETION_CUSHION = 1024


@dataclass(frozen=True, slots=True)
class RateLimitSnapshot:
    """Current shared-budget usage for one runtime backend."""

    runtime_backend: str
    requests_in_window: int
    request_limit: int | None
    tokens_in_window: int
    token_limit: int | None


class SharedRateLimitBucket:
    """Sliding-window request/token budget shared by concurrent runtime workers."""

    def __init__(
        self,
        *,
        runtime_backend: str,
        request_limit: int | None,
        token_limit: int | None,
        window_seconds: float = RATE_LIMIT_WINDOW_SECONDS,
        time_provider: Callable[[], float] | None = None,
    ) -> None:
        self._runtime_backend = runtime_backend
        self._request_limit = request_limit if request_limit and request_limit > 0 else None
        self._token_limit = token_limit if token_limit and token_limit > 0 else None
        self._window_seconds = window_seconds
        self._time = time_provider or time.monotonic
        self._lock = asyncio.Lock()
        self._reservations: deque[tuple[float, int]] = deque()

    @property
    def enabled(self) -> bool:
        """Return True when either request or token budgets are active."""
        return self._request_limit is not None or self._token_limit is not None

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_seconds
        while self._reservations and self._reservations[0][0] <= cutoff:
            self._reservations.popleft()

    def _tokens_in_window(self) -> int:
        return sum(tokens for _, tokens in self._reservations)

    def _snapshot(self) -> RateLimitSnapshot:
        return RateLimitSnapshot(
            runtime_backend=self._runtime_backend,
            requests_in_window=len(self._reservations),
            request_limit=self._request_limit,
            tokens_in_window=self._tokens_in_window(),
            token_limit=self._token_limit,
        )

    def _request_wait_seconds(self, now: float) -> float:
        if self._request_limit is None or len(self._reservations) < self._request_limit:
            return 0.0
        oldest_timestamp, _ = self._reservations[0]
        return max(0.0, oldest_timestamp + self._window_seconds - now)

    def _token_wait_seconds(self, now: float, estimated_tokens: int) -> float:
        if self._token_limit is None:
            return 0.0

        current_tokens = self._tokens_in_window()
        if current_tokens + estimated_tokens <= self._token_limit:
            return 0.0

        remaining_tokens = current_tokens
        wait_seconds = 0.0
        for timestamp, reserved_tokens in self._reservations:
            remaining_tokens -= reserved_tokens
            wait_seconds = max(0.0, timestamp + self._window_seconds - now)
            if remaining_tokens + estimated_tokens <= self._token_limit:
                return wait_seconds

        if not self._reservations:
            return 0.0
        newest_timestamp, _ = self._reservations[-1]
        return max(0.0, newest_timestamp + self._window_seconds - now)

    async def acquire(self, estimated_tokens: int) -> tuple[float, RateLimitSnapshot]:
        """Reserve capacity immediately or return the wait time before retry."""
        normalized_tokens = max(1, estimated_tokens)
        async with self._lock:
            now = self._time()
            self._prune(now)
            wait_seconds = max(
                self._request_wait_seconds(now),
                self._token_wait_seconds(now, normalized_tokens),
            )
            if wait_seconds <= 0:
                self._reservations.append((now, normalized_tokens))
                return 0.0, self._snapshot()
            return wait_seconds, self._snapshot()

    async def force_reserve(self, estimated_tokens: int) -> RateLimitSnapshot:
        """Reserve capacity unconditionally (for timeout escape hatch).

        Used when the wait loop has exhausted its maximum wait budget and
        must proceed regardless. This preserves the budget accounting
        invariant — without this, N workers timing out simultaneously
        would each bypass the bucket, causing N× the intended RPM to
        hit the upstream API in lockstep.
        """
        normalized_tokens = max(1, estimated_tokens)
        async with self._lock:
            now = self._time()
            self._prune(now)
            self._reservations.append((now, normalized_tokens))
            return self._snapshot()


def estimate_runtime_request_tokens(
    prompt: str,
    *,
    system_prompt: str | None = None,
) -> int:
    """Estimate the cost of starting one runtime request."""
    prompt_chars = len(prompt)
    system_chars = len(system_prompt or "")
    prompt_tokens = (prompt_chars + system_chars) // _TOKEN_ESTIMATE_DIVISOR
    return max(1, prompt_tokens + _TOKEN_COMPLETION_CUSHION)


__all__ = [
    "DEFAULT_ANTHROPIC_RPM_CEILING",
    "DEFAULT_ANTHROPIC_TPM_CEILING",
    "RATE_LIMIT_HEARTBEAT_SECONDS",
    "RATE_LIMIT_MAX_WAIT_SECONDS",
    "RATE_LIMIT_WINDOW_SECONDS",
    "RateLimitSnapshot",
    "SharedRateLimitBucket",
    "estimate_runtime_request_tokens",
]
