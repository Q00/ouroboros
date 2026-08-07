"""Provider-entry ordering shared by concurrent batch tasks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token

import anyio

_Gate = Callable[[], Awaitable[None]]
_gate: ContextVar[_Gate | None] = ContextVar("ouroboros_provider_admission_gate", default=None)


class AdmissionSequence:
    """Order provider entrances without serializing task preparation or execution."""

    def __init__(self, size: int) -> None:
        self._ready = [anyio.Event() for _ in range(size)]

    def bind(self, index: int) -> Token[_Gate | None]:
        """Bind this task behind its immediate predecessor."""
        return _gate.set(None if index == 0 else self._ready[index - 1].wait)

    def entered(self, index: int) -> None:
        """Release the next task once this task enters a provider effect."""
        self._ready[index].set()

    def finished(self, index: int, token: Token[_Gate | None]) -> None:
        """Release the next task on a pre-entry exit and restore context."""
        self._ready[index].set()
        _gate.reset(token)


async def wait() -> None:
    """Wait for the preceding batch task to enter or finish, if any."""
    gate = _gate.get()
    if gate is not None:
        await gate()
