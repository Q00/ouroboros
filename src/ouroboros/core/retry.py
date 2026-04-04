"""Small async retry helper used in place of a third-party retry package."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import wraps
import random
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


def retry_async(
    *,
    on: tuple[type[BaseException], ...],
    attempts: int,
    wait_initial: float,
    wait_max: float,
    wait_jitter: float = 0.0,
) -> Callable[[Callable[P, Any]], Callable[P, Any]]:
    """Retry an async callable with exponential backoff."""

    if attempts <= 0:
        msg = "attempts must be > 0"
        raise ValueError(msg)

    def decorator(func: Callable[P, Any]) -> Callable[P, Any]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            delay = max(wait_initial, 0.0)
            for attempt in range(1, attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except on:
                    if attempt >= attempts:
                        raise

                    sleep_for = min(delay, wait_max)
                    if wait_jitter > 0:
                        sleep_for += random.uniform(0.0, wait_jitter)
                    await asyncio.sleep(sleep_for)
                    delay = min(max(delay * 2, wait_initial), wait_max)

            msg = "retry_async exhausted without returning or raising"
            raise RuntimeError(msg)

        return wrapper

    return decorator
