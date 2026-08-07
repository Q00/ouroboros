"""Task-local provider-effect state for recoverable batch pauses."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass, field

import anyio

from ouroboros.orchestrator.provider_admission import AdmissionSequence

_Sink = Callable[[], None]
_entry_sink: ContextVar[_Sink | None] = ContextVar("ouroboros_provider_entry_sink", default=None)
_exit_sink: ContextVar[_Sink | None] = ContextVar("ouroboros_provider_exit_sink", default=None)
ProviderEffectTokens = tuple[Token[_Sink | None], Token[_Sink | None]]


def enter() -> None:
    """Mark the current task as crossing one provider-effect boundary."""
    sink = _entry_sink.get()
    if sink is not None:
        sink()


def complete() -> None:
    """Mark the current provider call complete after feedback observation."""
    sink = _exit_sink.get()
    if sink is not None:
        sink()


@dataclass(slots=True)
class BatchProviderEffects:
    """Distinguish never-entered, active, and completed batch provider calls."""

    pause_detected: anyio.Event
    admission_sequence: AdmissionSequence
    blocked_error: type[BaseException]
    _active: set[int] = field(default_factory=set, init=False)
    _entered: set[int] = field(default_factory=set, init=False)

    def bind(self, index: int) -> ProviderEffectTokens:
        """Install call-boundary callbacks for one batch task."""
        return (
            _entry_sink.set(lambda: self._enter(index)),
            _exit_sink.set(lambda: self._complete(index)),
        )

    def finish(self, index: int, tokens: ProviderEffectTokens) -> None:
        """Remove task-local callbacks and stale active state."""
        self._active.discard(index)
        _exit_sink.reset(tokens[1])
        _entry_sink.reset(tokens[0])

    def is_active(self, index: int) -> bool:
        """Return whether cancellation would cut through a live provider call."""
        return index in self._active

    def should_cancel(self, index: int) -> bool:
        """Cancel only pre-entry or currently uncertain calls, not completed ones."""
        return index not in self._entered or index in self._active

    def _enter(self, index: int) -> None:
        if self.pause_detected.is_set():
            raise self.blocked_error(
                "batch stopped before a new provider effect after a recoverable pause"
            )
        self._active.add(index)
        self._entered.add(index)
        self.admission_sequence.entered(index)

    def _complete(self, index: int) -> None:
        self._active.discard(index)


__all__ = ["BatchProviderEffects", "complete", "enter"]
