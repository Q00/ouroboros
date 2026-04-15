"""Unit tests for shared orchestrator rate-limit coordination."""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.rate_limit import SharedRateLimitBucket, estimate_runtime_request_tokens


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
