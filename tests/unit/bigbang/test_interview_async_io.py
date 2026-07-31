"""Tests for InterviewEngine — async file I/O via asyncio.to_thread.

Regression coverage for the bug where save_state/load_state used
synchronous file I/O (write_text/read_text + FileLock) inside async
handlers, blocking the asyncio event loop.

See: https://github.com/Q00/ouroboros/issues/284
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
from typing import IO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.bigbang.interview import (
    InterviewEngine,
    InterviewRound,
    InterviewState,
    InterviewStatus,
)
from ouroboros.providers.base import CompletionResponse, UsageInfo


class _PartialWriteHandle:
    def __init__(self, handle: IO[str]) -> None:
        self._handle = handle

    def __enter__(self) -> _PartialWriteHandle:
        return self

    def __exit__(self, *args) -> None:
        self._handle.close()

    def write(self, content: str) -> int:
        _ = self._handle.write(content[:1])
        raise RuntimeError("interrupted write")

    def flush(self) -> None:
        self._handle.flush()

    def fileno(self) -> int:
        return self._handle.fileno()


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
    async def test_save_state_preserves_existing_file_on_replace_failure(self, tmp_path) -> None:
        engine = _make_engine(tmp_path)
        state = _make_state()
        saved_path = engine._state_file_path(state.interview_id)
        saved_path.write_text("original\n", encoding="utf-8")

        with (
            patch("os.open", wraps=os.open) as mock_open,
            patch("os.fsync") as mock_fsync,
            patch("os.replace", side_effect=OSError("boom")),
        ):
            result = await engine.save_state(state)

        assert result.is_err
        assert saved_path.read_text(encoding="utf-8") == "original\n"
        temp_creations = [call for call in mock_open.call_args_list if call.args[1] & os.O_EXCL]
        assert len(temp_creations) == 1
        assert Path(temp_creations[0].args[0]).parent == saved_path.parent
        mock_fsync.assert_called_once()
        assert {path.name for path in saved_path.parent.iterdir()} == {
            saved_path.name,
            f"{saved_path.name}.lock",
        }

    @pytest.mark.asyncio
    async def test_save_state_closes_raw_fd_when_fdopen_fails(self, tmp_path) -> None:
        engine = _make_engine(tmp_path)
        state = _make_state()
        created_fds: list[int] = []
        created_paths: list[Path] = []
        real_open = os.open

        def _recording_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if flags & os.O_EXCL:
                created_fds.append(fd)
                created_paths.append(Path(path))
            return fd

        with (
            patch("os.open", side_effect=_recording_open),
            patch("os.fdopen", side_effect=RuntimeError("fdopen failed")),
            pytest.raises(RuntimeError, match="fdopen failed"),
        ):
            await engine.save_state(state)

        with pytest.raises(OSError):
            os.fstat(created_fds[0])
        assert not created_paths[0].exists()

    @pytest.mark.asyncio
    async def test_save_state_removes_partial_temp_on_non_oserror_write(self, tmp_path) -> None:
        engine = _make_engine(tmp_path)
        state = _make_state()
        saved_path = engine._state_file_path(state.interview_id)
        saved_path.write_text("original\n", encoding="utf-8")
        real_fdopen = os.fdopen

        def _interrupting_fdopen(fd, *args, **kwargs):
            return _PartialWriteHandle(real_fdopen(fd, *args, **kwargs))

        with (
            patch("os.fdopen", side_effect=_interrupting_fdopen),
            pytest.raises(RuntimeError, match="interrupted write"),
        ):
            await engine.save_state(state)

        assert saved_path.read_text(encoding="utf-8") == "original\n"
        assert {path.name for path in saved_path.parent.iterdir()} == {
            saved_path.name,
            f"{saved_path.name}.lock",
        }

    @pytest.mark.asyncio
    async def test_save_state_narrows_an_existing_readable_mode(self, tmp_path) -> None:
        """A transcript left readable by an older version is narrowed, not kept.

        The mode is deliberately NOT preserved here. Preserving it is the usual
        contract for an atomic write, and it is exactly what would carry a
        group- or world-readable transcript forward forever. The transcript
        holds confirmed data answers and lives indefinitely.
        """
        engine = _make_engine(tmp_path)
        state = _make_state()
        saved_path = engine._state_file_path(state.interview_id)
        saved_path.write_text("original\n", encoding="utf-8")
        saved_path.chmod(0o640)

        result = await engine.save_state(state)

        assert result.is_ok
        assert stat.S_IMODE(saved_path.stat().st_mode) == 0o600

    @pytest.mark.skipif(os.name != "posix", reason="directory fsync is POSIX-only")
    @pytest.mark.asyncio
    async def test_save_state_fsyncs_file_and_parent_directory(self, tmp_path) -> None:
        engine = _make_engine(tmp_path)
        state = _make_state()

        with patch("os.fsync", wraps=os.fsync) as mock_fsync:
            result = await engine.save_state(state)

        assert result.is_ok
        assert mock_fsync.call_count == 2

    @pytest.mark.skipif(os.name != "posix", reason="directory fsync is POSIX-only")
    @pytest.mark.asyncio
    async def test_save_state_allows_unsupported_directory_fsync(self, tmp_path) -> None:
        engine = _make_engine(tmp_path)
        state = _make_state()

        with patch(
            "os.fsync",
            side_effect=(None, OSError(errno.EINVAL, "directory fsync unsupported")),
        ) as mock_fsync:
            result = await engine.save_state(state)

        assert result.is_ok
        assert mock_fsync.call_count == 2

    @pytest.mark.skipif(os.name != "posix", reason="directory fsync is POSIX-only")
    @pytest.mark.asyncio
    async def test_save_state_reports_success_when_post_replace_fsync_fails(self, tmp_path) -> None:
        engine = _make_engine(tmp_path)
        state = _make_state()

        with patch(
            "os.fsync",
            side_effect=(None, OSError(errno.EIO, "directory fsync failed")),
        ) as mock_fsync:
            result = await engine.save_state(state)

        assert result.is_ok
        assert mock_fsync.call_count == 2
        load_result = await engine.load_state(state.interview_id)
        assert load_result.is_ok
        assert load_result.value.interview_id == state.interview_id

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
