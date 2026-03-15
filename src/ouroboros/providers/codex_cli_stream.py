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


async def iter_stream_lines(
    stream: asyncio.StreamReader | None,
    *,
    chunk_size: int = 16384,
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

    while True:
        chunk = await stream.read(chunk_size)
        if not chunk:
            break

        buffer += decoder.decode(chunk)
        while True:
            newline_index = buffer.find("\n")
            if newline_index < 0:
                break

            line = buffer[:newline_index]
            buffer = buffer[newline_index + 1 :]
            yield line.rstrip("\r")

    buffer += decoder.decode(b"", final=True)
    if buffer:
        yield buffer.rstrip("\r")


async def collect_stream_lines(
    stream: asyncio.StreamReader | None,
) -> list[str]:
    """Drain a subprocess stream into a list of non-empty lines."""
    if stream is None:
        return []

    lines: list[str] = []
    async for line in iter_stream_lines(stream):
        if line:
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
