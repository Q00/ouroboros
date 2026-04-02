"""Tests for InterviewEngine — async file I/O via asyncio.to_thread.

Regression coverage for the bug where save_state/load_state used
synchronous file I/O (write_text/read_text + FileLock) inside async
handlers, blocking the asyncio event loop.

See: https://github.com/Q00/ouroboros/issues/284
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.bigbang.interview import (
    InterviewEngine,
    InterviewRound,
    InterviewState,
    InterviewStatus,
)
from ouroboros.providers.base import CompletionResponse, UsageInfo


def _make_engine(tmp_path) -> InterviewEngine:
    """Create an InterviewEngine with a real state_dir."""
    adapter = MagicMock()
    adapter.complete = AsyncMock(
        return_value=CompletionResponse(
            content="Next question?",
            model="test",
            usage=UsageInfo(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            finish_reason="stop",
        )
    )
    return InterviewEngine(
        llm_adapter=adapter,
        state_dir=tmp_path,
        model="test-model",
    )


def _make_state(interview_id: str = "test-async-io") -> InterviewState:
    """Create a minimal InterviewState."""
    return InterviewState(
        interview_id=interview_id,
        initial_context="Build an app",
        rounds=[
            InterviewRound(round_number=1, question="Q1?", user_response="A1"),
        ],
        status=InterviewStatus.IN_PROGRESS,
    )


class TestSaveStateUsesThread:
    """save_state must offload blocking I/O to a thread."""

    @pytest.mark.asyncio
    async def test_save_state_calls_to_thread(self, tmp_path) -> None:
        """save_state uses asyncio.to_thread for file write."""
        engine = _make_engine(tmp_path)
        state = _make_state()

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            # Make to_thread actually write so the result is ok
            async def _run_sync(fn, *args, **kwargs):
                return fn(*args, **kwargs)

            mock_to_thread.side_effect = _run_sync
            result = await engine.save_state(state)

        assert result.is_ok
        mock_to_thread.assert_called_once()
        # The saved file should exist on disk
        saved_path = result.value
        assert saved_path.exists()

    @pytest.mark.asyncio
    async def test_save_state_roundtrip(self, tmp_path) -> None:
        """save_state writes valid JSON that load_state can read back."""
        engine = _make_engine(tmp_path)
        state = _make_state()

        save_result = await engine.save_state(state)
        assert save_result.is_ok

        load_result = await engine.load_state(state.interview_id)
        assert load_result.is_ok

        loaded = load_result.value
        assert loaded.interview_id == state.interview_id
        assert len(loaded.rounds) == 1
        assert loaded.rounds[0].user_response == "A1"


class TestLoadStateUsesThread:
    """load_state must offload blocking I/O to a thread."""

    @pytest.mark.asyncio
    async def test_load_state_calls_to_thread(self, tmp_path) -> None:
        """load_state uses asyncio.to_thread for file read."""
        engine = _make_engine(tmp_path)
        state = _make_state()

        # First save so there's something to load
        await engine.save_state(state)

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:

            async def _run_sync(fn, *args, **kwargs):
                return fn(*args, **kwargs)

            mock_to_thread.side_effect = _run_sync
            result = await engine.load_state(state.interview_id)

        assert result.is_ok
        mock_to_thread.assert_called_once()

    @pytest.mark.asyncio
    async def test_load_state_not_found(self, tmp_path) -> None:
        """load_state returns error for nonexistent interview."""
        engine = _make_engine(tmp_path)

        result = await engine.load_state("nonexistent-id")

        assert result.is_err
        assert "not found" in str(result.error).lower()


class TestEventLoopNotBlocked:
    """Verify the event loop is not blocked during I/O operations."""

    @pytest.mark.asyncio
    async def test_concurrent_save_load(self, tmp_path) -> None:
        """Multiple save/load operations can run concurrently."""
        import asyncio

        engine = _make_engine(tmp_path)

        states = [_make_state(f"concurrent-{i}") for i in range(3)]

        # Save all concurrently
        save_results = await asyncio.gather(*[engine.save_state(s) for s in states])
        assert all(r.is_ok for r in save_results)

        # Load all concurrently
        load_results = await asyncio.gather(*[engine.load_state(s.interview_id) for s in states])
        assert all(r.is_ok for r in load_results)
        assert {r.value.interview_id for r in load_results} == {s.interview_id for s in states}
