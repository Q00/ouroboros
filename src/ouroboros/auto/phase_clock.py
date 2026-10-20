"""An interview phase clock that stops while auto reads the repository.

The pipeline bounds the interview phase with a timeout. Time the investigating
self-answer (``auto/investigator.py``) spends reading the repository must not
count against that timeout or against the pipeline deadline: the interview
waits for each investigation, and an investigation never pushes the interview
into the deadline path.

``wait_for_pausable`` is ``asyncio.wait_for`` (Python 3.12 runs both through
``asyncio.timeout`` in the calling task) that also publishes its timeout handle
in a context variable. ``phase_clock_paused`` turns that timeout off for the
duration of its block, then re-arms it with the remaining time it had, and
moves the pipeline deadline (``AutoPipelineState.deadline_at`` and
``deadline_at_epoch``) later by the time the block took.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ouroboros.auto.state import AutoPipelineState

_PHASE_TIMEOUT: ContextVar[asyncio.Timeout | None] = ContextVar(
    "ouroboros_auto_phase_timeout", default=None
)


async def wait_for_pausable[T](coro: Coroutine[Any, Any, T], timeout: float) -> T:
    """Await ``coro`` under ``timeout`` seconds; raise ``TimeoutError`` past it."""
    async with asyncio.timeout(timeout) as handle:
        token = _PHASE_TIMEOUT.set(handle)
        try:
            return await coro
        finally:
            _PHASE_TIMEOUT.reset(token)


@asynccontextmanager
async def phase_clock_paused(state: AutoPipelineState) -> AsyncIterator[None]:
    """Stop the phase timeout and the pipeline deadline while the block runs."""
    loop = asyncio.get_running_loop()
    handle = _PHASE_TIMEOUT.get()
    when = handle.when() if handle is not None else None
    if handle is not None and when is not None:
        handle.reschedule(None)
    started = loop.time()
    try:
        yield
    finally:
        elapsed = loop.time() - started
        if handle is not None and when is not None:
            handle.reschedule(when + elapsed)
        if state.deadline_at is not None:
            state.deadline_at += elapsed
        if state.deadline_at_epoch is not None:
            state.deadline_at_epoch += elapsed


__all__ = ["phase_clock_paused", "wait_for_pausable"]
