"""Shared lifecycle guards for EventStore writes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from ouroboros.core.errors import PersistenceError


async def run_with_write_lifecycle[T](
    coro: Coroutine[Any, Any, T],
    *,
    registry: set[asyncio.Task[Any]],
    refuse_when: Callable[[], bool],
    operation: str,
) -> T:
    """Keep a complete logical write registered across retries and backoff."""
    if refuse_when():
        coro.close()
        raise PersistenceError(
            "EventStore is closing; write refused.",
            operation=operation,
        )
    task = asyncio.current_task()
    if task is None:  # pragma: no cover - async functions always have a task here.
        coro.close()
        raise PersistenceError(
            "EventStore write has no owning task.",
            operation=operation,
        )
    already_registered = task in registry
    registry.add(task)
    try:
        return await coro
    finally:
        if not already_registered:
            registry.discard(task)
