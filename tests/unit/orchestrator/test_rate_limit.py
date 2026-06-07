"""Unit tests for shared orchestrator rate-limit coordination."""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.rate_limit import (
    RateLimitBackoff,
    RateLimitGate,
    SharedRateLimitBucket,
    build_rate_limit_gate,
    estimate_runtime_request_tokens,
)


@pytest.mark.asyncio
async def test_shared_rate_limit_bucket_waits_when_request_budget_is_exhausted() -> None:
    """The shared bucket should defer new reservations once RPM is exhausted."""
    clock = {"now": 0.0}
    bucket = SharedRateLimitBucket(
        runtime_backend="claude",
        request_limit=1,
        token_limit=None,
        time_provider=lambda: clock["now"],
    )

    wait_seconds, _ = await bucket.acquire(estimated_tokens=100)
    assert wait_seconds == 0.0

    wait_seconds, snapshot = await bucket.acquire(estimated_tokens=100)
    assert wait_seconds == 60.0
    assert snapshot.requests_in_window == 1
    assert snapshot.request_limit == 1

    clock["now"] = 60.0
    wait_seconds, snapshot = await bucket.acquire(estimated_tokens=100)
    assert wait_seconds == 0.0
    assert snapshot.requests_in_window == 1


@pytest.mark.asyncio
async def test_shared_rate_limit_bucket_waits_when_token_budget_is_exhausted() -> None:
    """The shared bucket should defer reservations once TPM is exhausted."""
    clock = {"now": 0.0}
    bucket = SharedRateLimitBucket(
        runtime_backend="claude",
        request_limit=None,
        token_limit=200,
        time_provider=lambda: clock["now"],
    )

    wait_seconds, _ = await bucket.acquire(estimated_tokens=150)
    assert wait_seconds == 0.0

    wait_seconds, snapshot = await bucket.acquire(estimated_tokens=100)
    assert wait_seconds == 60.0
    assert snapshot.tokens_in_window == 150
    assert snapshot.token_limit == 200


def test_estimate_runtime_request_tokens_adds_completion_cushion() -> None:
    """Runtime token estimates should always include a non-zero completion cushion."""
    estimate = estimate_runtime_request_tokens("abcd" * 100, system_prompt="system")

    assert estimate > 100


@pytest.mark.asyncio
async def test_force_reserve_appends_unconditionally() -> None:
    """force_reserve must append to _reservations regardless of current budget."""
    bucket = SharedRateLimitBucket(
        runtime_backend="claude",
        request_limit=2,
        token_limit=None,
    )
    # Fill the bucket beyond capacity
    for _ in range(5):
        await bucket.force_reserve(1000)
    # All 5 reservations should exist — force_reserve bypasses the budget check.
    assert len(bucket._reservations) == 5


@pytest.mark.asyncio
async def test_force_reserve_reports_snapshot_reflecting_new_reservation() -> None:
    """force_reserve must surface a snapshot that includes the new reservation."""
    bucket = SharedRateLimitBucket(
        runtime_backend="claude",
        request_limit=1,
        token_limit=4_096,
    )

    # Saturate the request budget first via the normal path.
    wait_seconds, _ = await bucket.acquire(estimated_tokens=500)
    assert wait_seconds == 0.0

    # force_reserve should proceed even though acquire() would now block.
    snapshot = await bucket.force_reserve(estimated_tokens=750)

    assert snapshot.requests_in_window == 2
    assert snapshot.tokens_in_window == 1_250
    assert snapshot.request_limit == 1
    assert snapshot.token_limit == 4_096


class TestRateLimitGate:
    """The reusable dispatch gate around a shared bucket."""

    @pytest.mark.asyncio
    async def test_dormant_gate_acquires_immediately(self) -> None:
        # No limits → dormant: acquire must return without sleeping.
        slept: list[float] = []
        gate = build_rate_limit_gate(
            "hermes_cli",
            request_limit=None,
            token_limit=None,
            sleep=lambda seconds: slept.append(seconds),
        )

        assert gate.enabled is False
        await gate.acquire(estimated_tokens=10_000)
        assert slept == []

    @pytest.mark.asyncio
    async def test_gate_paces_dispatch_when_request_budget_exhausted(self) -> None:
        clock = {"now": 0.0}
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            clock["now"] += seconds  # advance the window so the next acquire frees

        bucket = SharedRateLimitBucket(
            runtime_backend="hermes_cli",
            request_limit=1,
            token_limit=None,
            time_provider=lambda: clock["now"],
        )
        # Heartbeat larger than the wait so the 60s wait is a single sleep chunk.
        gate = RateLimitGate(bucket, heartbeat_seconds=120.0, sleep=fake_sleep)

        backoffs: list[RateLimitBackoff] = []
        await gate.acquire(estimated_tokens=100, on_backoff=backoffs.append)  # first: free
        await gate.acquire(estimated_tokens=100, on_backoff=backoffs.append)  # second: waits 60s

        assert slept == [60.0]
        assert len(backoffs) == 1
        assert backoffs[0].forced is False
        assert backoffs[0].wait_seconds == 60.0

    @pytest.mark.asyncio
    async def test_gate_force_reserves_after_max_wait(self) -> None:
        clock = {"now": 0.0}
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            # Do NOT advance the clock: the budget never frees, forcing timeout.

        bucket = SharedRateLimitBucket(
            runtime_backend="hermes_cli",
            request_limit=1,
            token_limit=None,
            time_provider=lambda: clock["now"],
        )
        # Small wait budget + heartbeat so the loop force-reserves quickly.
        gate = RateLimitGate(bucket, max_wait_seconds=60.0, heartbeat_seconds=30.0, sleep=fake_sleep)

        await gate.acquire(estimated_tokens=100)  # consume the only slot

        backoffs: list[RateLimitBackoff] = []
        await gate.acquire(estimated_tokens=100, on_backoff=backoffs.append)

        # Two 30s heartbeats reach the 60s ceiling, then a forced reservation.
        assert slept == [30.0, 30.0]
        assert backoffs[-1].forced is True
        assert len(bucket._reservations) == 2  # force_reserve appended despite saturation
