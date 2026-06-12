"""Stream and subprocess management helpers for CLI provider adapters.

This module provides low-level async utilities for reading subprocess
output streams and performing graceful process termination.  They are
extracted from :mod:`ouroboros.providers.codex_cli_adapter` to keep
that module focused on the LLM adapter logic, and are backend-neutral:
each caller threads its own ``provider`` tag so the emitted
:class:`~ouroboros.core.errors.ProviderError` instances carry the
correct backend label (codex, copilot, opencode, gemini, ...).
"""

from __future__ import annotations

import asyncio
import codecs
from collections.abc import AsyncIterator, Awaitable, Callable
import contextlib
from typing import Any, Protocol

from ouroboros.core.errors import ProviderError

_MAX_STREAM_LINE_BUFFER_BYTES = 50 * 1024 * 1024
_MAX_STREAM_CAPTURE_BYTES = 50 * 1024 * 1024
# Orchestrator runtimes parse JSONL stdout; cap the line buffer so newline-free
# output (or a stuck stream) cannot grow memory without bound.  Matches the
# value the codex/opencode/pi runtimes already enforced inline.
_MAX_RUNTIME_LINE_BUFFER_BYTES = 50 * 1024 * 1024


class _StructLogger(Protocol):
    """Minimal structured-logger surface used by the runtime helpers."""

    def warning(self, event: str, **kwargs: Any) -> Any: ...

    def error(self, event: str, **kwargs: Any) -> Any: ...


async def iter_stream_lines(
    stream: asyncio.StreamReader | None,
    *,
    chunk_size: int = 16384,
    max_buffer_bytes: int = _MAX_STREAM_LINE_BUFFER_BYTES,
    provider: str = "codex_cli",
) -> AsyncIterator[str]:
    """Yield decoded lines from an asyncio stream without readline().

    The function reads raw bytes in *chunk_size* chunks, feeds them
    through an incremental UTF-8 decoder, and splits on newline
    boundaries.  Trailing ``\\r`` characters are stripped.

    *provider* tags any :class:`ProviderError` raised on buffer overflow
    with the calling backend so the error is attributable.
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
                message=(f"CLI stream line buffer exceeded {max_buffer_bytes} bytes"),
                provider=provider,
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
    provider: str = "codex_cli",
) -> list[str]:
    """Drain a subprocess stream into a list of non-empty lines.

    The collector enforces a cumulative byte cap so stderr/stdout capture cannot
    grow without bound under noisy or malicious subprocess output.  *provider*
    tags any :class:`ProviderError` raised on overflow with the calling backend.
    """
    if stream is None:
        return []

    lines: list[str] = []
    total_bytes = 0
    async for line in iter_stream_lines(stream, provider=provider):
        if not line:
            continue

        total_bytes += len(line.encode("utf-8", errors="replace")) + 1
        if total_bytes > max_total_bytes:
            raise ProviderError(
                message=(f"CLI stream capture exceeded {max_total_bytes} bytes"),
                provider=provider,
                details={
                    "capture_limit_bytes": max_total_bytes,
                    "overflow_stage": "stream_capture",
                },
            )
        lines.append(line)
    return lines


async def iter_runtime_stream_lines(
    stream: asyncio.StreamReader | None,
    *,
    display_name: str,
    chunk_size: int = 16384,
    first_chunk_timeout_seconds: float | None = None,
    chunk_timeout_seconds: float | None = None,
    max_buffer_bytes: int = _MAX_RUNTIME_LINE_BUFFER_BYTES,
    logger: _StructLogger | None = None,
    log_namespace: str | None = None,
) -> AsyncIterator[str]:
    """Yield decoded lines from a runtime subprocess stdout/stderr stream.

    This is the orchestrator-runtime superset of :func:`iter_stream_lines`:
    in addition to the incremental UTF-8 decode and the line-buffer cap, it
    enforces per-chunk timeouts so a runtime that produces no output during a
    startup or idle window fails fast.

    Args:
        stream: Async stream reader, or ``None`` (returns immediately).
        display_name: Human-readable backend label used in the timeout message.
        chunk_size: Bytes to read per iteration.
        first_chunk_timeout_seconds: Maximum wait for the first chunk (startup
            guard).  ``None`` disables the guard.
        chunk_timeout_seconds: Maximum wait between subsequent chunks (idle
            guard).  ``None`` disables the guard.
        max_buffer_bytes: Line-buffer ceiling.  Prevents unbounded memory
            growth on newline-free output.
        logger: Optional structured logger; when supplied an overflow logs
            ``{log_namespace}.line_buffer_overflow`` before raising.
        log_namespace: Namespace prefix for the overflow log event.

    Yields:
        Newline-delimited strings with trailing ``\\r`` stripped.

    Raises:
        TimeoutError: If a startup/idle timeout is exceeded.
        ProviderError: If the line buffer overflows ``max_buffer_bytes``.
    """
    if stream is None:
        return

    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""
    buffer_byte_estimate = 0
    saw_chunk = False

    while True:
        timeout_seconds: float | None = None
        if not saw_chunk:
            timeout_seconds = first_chunk_timeout_seconds
        elif chunk_timeout_seconds is not None:
            timeout_seconds = chunk_timeout_seconds

        try:
            if timeout_seconds is None:
                chunk = await stream.read(chunk_size)
            else:
                chunk = await asyncio.wait_for(
                    stream.read(chunk_size),
                    timeout=timeout_seconds,
                )
        except TimeoutError as exc:
            phase = "startup" if not saw_chunk else "idle"
            raise TimeoutError(
                f"{display_name} produced no stdout during {phase} window ({timeout_seconds:.0f}s)"
            ) from exc
        if not chunk:
            break

        saw_chunk = True
        decoded = decoder.decode(chunk)
        buffer += decoded
        # Track byte size incrementally: worst-case 4 bytes per char (UTF-8).
        buffer_byte_estimate += len(decoded) * 4
        if buffer_byte_estimate > max_buffer_bytes:
            if logger is not None:
                logger.error(
                    f"{log_namespace}.line_buffer_overflow",
                    buffer_size=len(buffer),
                    limit=max_buffer_bytes,
                )
            raise ProviderError(f"JSONL line buffer exceeded {max_buffer_bytes} bytes")
        while True:
            newline_index = buffer.find("\n")
            if newline_index < 0:
                break

            line = buffer[:newline_index]
            buffer = buffer[newline_index + 1 :]
            # Recalculate estimate after draining consumed lines.
            buffer_byte_estimate = len(buffer) * 4
            yield line.rstrip("\r")

    buffer += decoder.decode(b"", final=True)
    if buffer:
        yield buffer.rstrip("\r")


async def terminate_runtime_process(
    process: Any,
    *,
    shutdown_timeout: float = 5.0,
    logger: _StructLogger | None = None,
    log_namespace: str | None = None,
    close_stdin: Callable[[Any], Awaitable[None]] | None = None,
) -> None:
    """Best-effort runtime subprocess shutdown (SIGTERM then SIGKILL).

    Orchestrator-runtime superset of :func:`terminate_process`.  In addition to
    the duck-typed terminate/kill escalation it optionally closes a writable
    stdin pipe first (for runtimes that feed prompts via stdin, e.g. Codex) and
    logs per-step failures under *log_namespace* when a *logger* is supplied.

    Args:
        process: Subprocess object exposing ``terminate``/``kill``/``wait``.
        shutdown_timeout: Grace period before escalating to SIGKILL.
        logger: Optional structured logger for per-step failure events.
        log_namespace: Namespace prefix for failure log events.
        close_stdin: Optional coroutine to close the process stdin before
            terminating (best-effort).
    """
    if getattr(process, "returncode", None) is not None:
        return

    if close_stdin is not None:
        await close_stdin(process)

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
    except Exception as exc:
        if logger is not None:
            logger.warning(
                f"{log_namespace}.process_terminate_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
        return

    try:
        await asyncio.wait_for(process.wait(), timeout=shutdown_timeout)
        return
    except (TimeoutError, ProcessLookupError):
        pass
    except Exception as exc:
        if logger is not None:
            logger.warning(
                f"{log_namespace}.process_wait_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
        return

    if not callable(kill_fn):
        return

    try:
        kill_fn()
    except ProcessLookupError:
        return
    except Exception as exc:
        if logger is not None:
            logger.warning(
                f"{log_namespace}.process_kill_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
        return

    with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError, Exception):
        await asyncio.wait_for(process.wait(), timeout=shutdown_timeout)


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
    "iter_runtime_stream_lines",
    "iter_stream_lines",
    "terminate_process",
    "terminate_runtime_process",
]
