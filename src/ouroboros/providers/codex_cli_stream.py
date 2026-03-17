"""Stream and subprocess management helpers for the Codex CLI adapter.

This module provides low-level async utilities for reading subprocess
output streams and performing graceful process termination.  They are
extracted from :mod:`ouroboros.providers.codex_cli_adapter` to keep
that module focused on the LLM adapter logic.
"""

from __future__ import annotations

import asyncio
import codecs
from collections.abc import AsyncIterator
import contextlib
from typing import Any

from ouroboros.core.errors import ProviderError

_MAX_STREAM_LINE_BUFFER_BYTES = 50 * 1024 * 1024
_MAX_STREAM_CAPTURE_BYTES = 50 * 1024 * 1024


async def iter_stream_lines(
    stream: asyncio.StreamReader | None,
    *,
    chunk_size: int = 16384,
    max_buffer_bytes: int = _MAX_STREAM_LINE_BUFFER_BYTES,
) -> AsyncIterator[str]:
    """Yield decoded lines from an asyncio stream without readline().

    The function reads raw bytes in *chunk_size* chunks, feeds them
    through an incremental UTF-8 decoder, and splits on newline
    boundaries.  Trailing ``\\r`` characters are stripped.
    """
    if stream is None:
        return

    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""
    buffer_byte_estimate = 0

    while True:
        chunk = await stream.read(chunk_size)
        if not chunk:
            break

        decoded = decoder.decode(chunk)
        buffer += decoded
        buffer_byte_estimate += len(decoded) * 4
        if buffer_byte_estimate > max_buffer_bytes:
            raise ProviderError(
                message=(
                    "Codex CLI stream line buffer exceeded "
                    f"{max_buffer_bytes} bytes"
                ),
                provider="codex_cli",
                details={
                    "buffer_limit_bytes": max_buffer_bytes,
                    "overflow_stage": "line_buffer",
                },
            )

        while True:
            newline_index = buffer.find("\n")
            if newline_index < 0:
                break

            line = buffer[:newline_index]
            buffer = buffer[newline_index + 1 :]
            buffer_byte_estimate = len(buffer) * 4
            yield line.rstrip("\r")

    buffer += decoder.decode(b"", final=True)
    if buffer:
        yield buffer.rstrip("\r")


async def collect_stream_lines(
    stream: asyncio.StreamReader | None,
    *,
    max_total_bytes: int = _MAX_STREAM_CAPTURE_BYTES,
) -> list[str]:
    """Drain a subprocess stream into a list of non-empty lines.

    The collector enforces a cumulative byte cap so stderr/stdout capture cannot
    grow without bound under noisy or malicious subprocess output.
    """
    if stream is None:
        return []

    lines: list[str] = []
    total_bytes = 0
    async for line in iter_stream_lines(stream):
        if not line:
            continue

        total_bytes += len(line.encode("utf-8", errors="replace")) + 1
        if total_bytes > max_total_bytes:
            raise ProviderError(
                message=(
                    "Codex CLI stream capture exceeded "
                    f"{max_total_bytes} bytes"
                ),
                provider="codex_cli",
                details={
                    "capture_limit_bytes": max_total_bytes,
                    "overflow_stage": "stream_capture",
                },
            )
        lines.append(line)
    return lines


async def terminate_process(
    process: Any,
    *,
    shutdown_timeout: float = 5.0,
) -> None:
    """Best-effort subprocess shutdown for timeouts and cancellation.

    Attempts SIGTERM first, then escalates to SIGKILL if the process
    does not exit within *shutdown_timeout* seconds.
    """
    if getattr(process, "returncode", None) is not None:
        return

    terminate_fn = getattr(process, "terminate", None)
    kill_fn = getattr(process, "kill", None)

    try:
        if callable(terminate_fn):
            terminate_fn()
        elif callable(kill_fn):
            kill_fn()
        else:
            return
    except ProcessLookupError:
        return
    except Exception:
        return

    try:
        await asyncio.wait_for(process.wait(), timeout=shutdown_timeout)
        return
    except (TimeoutError, ProcessLookupError):
        pass
    except Exception:
        return

    if not callable(kill_fn):
        return

    with contextlib.suppress(ProcessLookupError, Exception):
        kill_fn()

    with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError, Exception):
        await asyncio.wait_for(process.wait(), timeout=shutdown_timeout)


__all__ = [
    "collect_stream_lines",
    "iter_stream_lines",
    "terminate_process",
]
