"""Tests for the internal async retry helper."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.core.retry import retry_async


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
