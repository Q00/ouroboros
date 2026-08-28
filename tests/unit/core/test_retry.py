"""Tests for the internal async retry helper."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.core import retry as retry_module
from ouroboros.core.retry import (
    DEFAULT_JITTER_RATIO,
    MIN_WAIT_SECONDS,
    _jittered_wait,
    retry_async,
)


@pytest.mark.asyncio
async def test_retry_async_retries_until_success() -> None:
    attempts = 0

    @retry_async(on=(ValueError,), attempts=3, wait_initial=0.1, wait_max=1.0)
    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ValueError("transient")
        return "ok"

    with patch("asyncio.sleep", new=AsyncMock()) as sleep_mock:
        result = await flaky()

    assert result == "ok"
    assert attempts == 3
    assert sleep_mock.await_count == 2


@pytest.mark.asyncio
async def test_retry_async_raises_after_exhaustion() -> None:
    attempts = 0

    @retry_async(on=(ValueError,), attempts=2, wait_initial=0.1, wait_max=1.0)
    async def always_fail() -> None:
        nonlocal attempts
        attempts += 1
        raise ValueError("still failing")

    with patch("asyncio.sleep", new=AsyncMock()) as sleep_mock:
        with pytest.raises(ValueError, match="still failing"):
            await always_fail()

    assert attempts == 2
    assert sleep_mock.await_count == 1


class TestJitteredWait:
    """Waits are jittered by default so co-scheduled retries do not stampede."""

    def test_default_jitter_spreads_waits_and_stays_positive(self) -> None:
        waits = {_jittered_wait(4.0, None) for _ in range(200)}

        assert len(waits) > 1  # a fixed wait would collapse to one value
        assert all(0.0 < w <= 4.0 for w in waits)
        # Equal jitter: never less than half the nominal wait.
        assert min(waits) >= 4.0 * (1.0 - DEFAULT_JITTER_RATIO) - 1e-9
        assert max(waits) > 4.0 * (1.0 - DEFAULT_JITTER_RATIO)

    def test_explicit_jitter_keeps_additive_semantics(self) -> None:
        waits = [_jittered_wait(2.0, 1.0) for _ in range(200)]

        assert all(2.0 <= w <= 3.0 for w in waits)
        assert len(set(waits)) > 1

    def test_zero_jitter_opts_out_but_never_sleeps_zero(self) -> None:
        assert _jittered_wait(2.0, 0.0) == 2.0
        assert _jittered_wait(0.0, 0.0) == MIN_WAIT_SECONDS
        assert _jittered_wait(-5.0, None) == MIN_WAIT_SECONDS

    def test_negative_jitter_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="wait_jitter"):
            retry_async(
                on=(ValueError,), attempts=2, wait_initial=1.0, wait_max=2.0, wait_jitter=-1.0
            )

    @pytest.mark.asyncio
    async def test_decorator_jitters_by_default(self) -> None:
        @retry_async(on=(ValueError,), attempts=2, wait_initial=4.0, wait_max=4.0)
        async def always_fail() -> None:
            raise ValueError("boom")

        with patch("asyncio.sleep", new=AsyncMock()) as sleep_mock:
            with pytest.raises(ValueError, match="boom"):
                await always_fail()

        slept = sleep_mock.await_args.args[0]
        assert 2.0 <= slept <= 4.0


@pytest.mark.asyncio
async def test_retry_async_logs_each_retry() -> None:
    """Operators need a structlog signal during a retry storm."""

    @retry_async(on=(ValueError,), attempts=3, wait_initial=0.1, wait_max=1.0)
    async def always_fail() -> None:
        raise ValueError("transient boom")

    with patch("asyncio.sleep", new=AsyncMock()), patch.object(retry_module.log, "warning") as warn:
        with pytest.raises(ValueError, match="transient boom"):
            await always_fail()

    assert warn.call_count == 2  # one per retry, none after exhaustion
    event, kwargs = warn.call_args_list[0].args[0], warn.call_args_list[0].kwargs
    assert event == "retry_async.retry"
    assert kwargs["attempt"] == 1
    assert kwargs["error_type"] == "ValueError"
    assert kwargs["error"] == "transient boom"
    assert kwargs["delay"] > 0
    assert warn.call_args_list[1].kwargs["attempt"] == 2
