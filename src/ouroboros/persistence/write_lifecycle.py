"""Shared lifecycle guards for EventStore writes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from ouroboros.core.errors import PersistenceError


async def run_with_write_lifecycle[T](
    coro: Coroutine[Any, Any, T],
    *,
    registry: set[asyncio.Future[Any]],
    refuse_when: Callable[[], bool],
    operation: str,
) -> T:
    """Keep a complete logical write registered across retries and backoff.

    The registry entry is a completion token for this write, not the owning
    task.  A caller commonly continues with another store operation after the
    write returns; making its whole task the drain target lets initialize() or
    close() wait on code that is itself waiting for that lifecycle operation.
    The token settles in this wrapper's ``finally`` block, exactly at the
    logical-write boundary, so drains cover retries without inheriting the
    caller's unrelated lifetime.
    """
    if refuse_when():
        coro.close()
        raise PersistenceError(
            "EventStore is closing; write refused.",
            operation=operation,
        )
    completion = asyncio.get_running_loop().create_future()
    registry.add(completion)
    try:
        return await coro
    finally:
        if not completion.done():
            completion.set_result(None)
        registry.discard(completion)
