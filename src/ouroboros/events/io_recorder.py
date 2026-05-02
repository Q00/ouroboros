"""Async context-manager helper that records I/O Journal events.

Issue #517 — slice 2 of #517. Adapters and tool dispatchers wrap their
LLM/tool calls in :class:`IOJournalRecorder` so the journal entry
emission becomes a 4-line addition instead of duplicating the
factory + hashing + privacy + timing boilerplate at every call site.

Design choices baked in:

* The recorder is **always opt-in**. ``event_store=None`` produces a
  no-op recorder that returns the same context-manager shape but
  emits nothing. Adapters that have not yet adopted the journal pass
  ``None`` and continue to work unchanged.
* The recorder **owns** the ``call_id``, the timing, the hashing, and
  the privacy switch. Callers only provide payload text and metadata.
* The recorder pairs a ``*.started`` / ``*.requested`` event with a
  ``*.returned`` / ``*.returned`` event using a shared ``call_id``.
  On exception inside the context block the recorder still emits the
  paired ``returned`` event with ``is_error=True`` so projections see
  the failure rather than a half-open call.
* The recorder is **async** so it can ``await event_store.append``;
  inside the context block the caller can use ordinary ``await``.

The recorder does not import from :mod:`ouroboros.persistence` — it
takes a duck-typed ``event_store`` with an ``append(event)`` coroutine.
That keeps it cheaply testable with a ``list``-backed fake.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import logging
import time
from typing import Any, Protocol

from ouroboros.events.base import BaseEvent
from ouroboros.events.io import (
    PREVIEW_DEFAULT_CHARS,
    PREVIEW_HARD_CAP_CHARS_LLM,
    PREVIEW_HARD_CAP_CHARS_TOOL,
    PrivacyMode,
    content_hash,
    create_llm_call_requested_event,
    create_llm_call_returned_event,
    create_tool_call_returned_event,
    create_tool_call_started_event,
    new_call_id,
)

logger = logging.getLogger(__name__)

_CURRENT_IO_JOURNAL_RECORDER: ContextVar[IOJournalRecorder | None] = ContextVar(
    "ouroboros_current_io_journal_recorder",
    default=None,
)


class _AppendableEventStore(Protocol):
    """Structural type for the recorder's ``event_store`` argument."""

    def append(self, event: BaseEvent) -> Awaitable[None]: ...


def get_current_io_journal_recorder() -> IOJournalRecorder | None:
    """Return the recorder scoped to the current async task, if any."""
    return _CURRENT_IO_JOURNAL_RECORDER.get()


@contextmanager
def use_io_journal_recorder(recorder: IOJournalRecorder | None) -> Iterator[None]:
    """Temporarily scope *recorder* to adapter calls in the current task.

    This lets a shared LLM adapter resolve the right per-call
    ``target_type`` / ``target_id`` without storing that target on the
    adapter itself.
    """
    token = _CURRENT_IO_JOURNAL_RECORDER.set(recorder)
    try:
        yield
    finally:
        _CURRENT_IO_JOURNAL_RECORDER.reset(token)


@dataclass(slots=True)
class LLMCallRecord:
    """Mutable handle returned to the caller inside a recorded LLM call.

    The caller fills this in *before* the context exits so the recorder
    can emit a fully-populated ``llm.call.returned`` event. Any field
    left at its default reflects "not provided"; the recorder omits
    ``None`` fields from the persisted payload (matching the policy in
    ``events/control.py`` and ``events/io.py``).
    """

    completion_text: str | None = None
    finish_reason: str | None = None
    token_count_in: int | None = None
    token_count_out: int | None = None
    is_error: bool = False
    error_kind: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def record_completion(
        self,
        *,
        completion_text: str | None = None,
        finish_reason: str | None = None,
        token_count_in: int | None = None,
        token_count_out: int | None = None,
        is_error: bool = False,
        error_kind: str | None = None,
        **extra: Any,
    ) -> None:
        """Fluent setter that mirrors the keyword shape of the factory."""
        if completion_text is not None:
            self.completion_text = completion_text
        if finish_reason is not None:
            self.finish_reason = finish_reason
        if token_count_in is not None:
            self.token_count_in = token_count_in
        if token_count_out is not None:
            self.token_count_out = token_count_out
        if is_error:
            self.is_error = True
        if error_kind is not None:
            self.error_kind = error_kind
            self.is_error = True
        if extra:
            self.extra.update(extra)

    def record_error(self, *, error_kind: str, completion_text: str | None = None) -> None:
        """Mark a handled provider-level failure without raising from the context."""
        self.is_error = True
        self.error_kind = error_kind
        if completion_text is not None:
            self.completion_text = completion_text


@dataclass(slots=True)
class ToolCallRecord:
    """Mutable handle returned to the caller inside a recorded tool call."""

    result_text: str | None = None
    is_error: bool = False
    error_kind: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def record_result(
        self,
        *,
        result_text: str | None = None,
        error_kind: str | None = None,
        **extra: Any,
    ) -> None:
        if result_text is not None:
            self.result_text = result_text
        if error_kind is not None:
            self.error_kind = error_kind
            self.is_error = True
        if extra:
            self.extra.update(extra)

    def record_error(self, *, error_kind: str, result_text: str | None = None) -> None:
        """Mark a handled tool failure without raising from the context."""
        self.is_error = True
        self.error_kind = error_kind
        if result_text is not None:
            self.result_text = result_text


@dataclass(frozen=True, slots=True)
class IOJournalRecorder:
    """Adapter-side helper that wraps LLM and tool calls in journal events.

    Construction:
        recorder = IOJournalRecorder(
            event_store=event_store,         # or None for no-op
            target_type="execution",
            target_id="exec_123",
            session_id="sess_x",             # optional correlation
            execution_id="exec_123",
            lineage_id=None,
            generation_number=None,
            phase=None,
        )

    Usage::

        async with recorder.record_llm_call(
            model_id="claude-opus-4",
            prompt_text=prompt_str,
            caller="anthropic_adapter",
        ) as call:
            response = await client.messages.create(**kwargs)
            call.record_completion(
                completion_text=text,
                finish_reason="stop",
                token_count_in=120,
                token_count_out=80,
            )
    """

    event_store: _AppendableEventStore | None
    target_type: str
    target_id: str
    session_id: str | None = None
    execution_id: str | None = None
    lineage_id: str | None = None
    generation_number: int | None = None
    phase: str | None = None
    privacy: PrivacyMode | None = None

    @property
    def is_active(self) -> bool:
        """``True`` when the recorder has somewhere to append events."""
        return self.event_store is not None

    @asynccontextmanager
    async def record_llm_call(
        self,
        *,
        model_id: str,
        prompt_text: str,
        caller: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        tool_choice: str | None = None,
        preview_cap: int = PREVIEW_DEFAULT_CHARS,
        preview_hard_cap: int = PREVIEW_HARD_CAP_CHARS_LLM,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[LLMCallRecord]:
        """Wrap an outbound LLM call and emit started/returned events.

        The recorder owns the ``call_id``, the timing, the prompt hash,
        and the privacy-aware preview shaping. The caller's job is to
        fill in completion details on the yielded :class:`LLMCallRecord`
        before the context exits.
        """
        record = LLMCallRecord()
        if not self.is_active:
            yield record
            return

        call_id = new_call_id()
        prompt_hash_value = content_hash(prompt_text)
        started = create_llm_call_requested_event(
            target_type=self.target_type,
            target_id=self.target_id,
            call_id=call_id,
            model_id=model_id,
            prompt_hash=prompt_hash_value,
            prompt_preview=prompt_text,
            preview_cap=preview_cap,
            preview_hard_cap=preview_hard_cap,
            privacy=self.privacy,
            caller=caller,
            max_tokens=max_tokens,
            temperature=temperature,
            tool_choice=tool_choice,
            session_id=self.session_id,
            execution_id=self.execution_id,
            lineage_id=self.lineage_id,
            generation_number=self.generation_number,
            phase=self.phase,
            extra=extra,
        )
        started_persisted = await self._append(started)

        start_perf = time.perf_counter()
        is_error = False
        error_kind: str | None = None
        try:
            yield record
        except BaseException as exc:  # noqa: BLE001 — the journal must capture every failure path
            is_error = True
            error_kind = type(exc).__name__
            raise
        finally:
            duration_ms = int((time.perf_counter() - start_perf) * 1000)
            completion_hash_value = (
                content_hash(record.completion_text) if record.completion_text is not None else None
            )
            if started_persisted:
                returned = create_llm_call_returned_event(
                    target_type=self.target_type,
                    target_id=self.target_id,
                    call_id=call_id,
                    model_id=model_id,
                    prompt_hash=prompt_hash_value,
                    duration_ms=duration_ms,
                    is_error=is_error or record.is_error,
                    completion_preview=record.completion_text,
                    preview_cap=preview_cap,
                    preview_hard_cap=preview_hard_cap,
                    privacy=self.privacy,
                    completion_hash=completion_hash_value,
                    finish_reason=record.finish_reason,
                    token_count_in=record.token_count_in,
                    token_count_out=record.token_count_out,
                    error_kind=error_kind or record.error_kind,
                    session_id=self.session_id,
                    execution_id=self.execution_id,
                    lineage_id=self.lineage_id,
                    generation_number=self.generation_number,
                    phase=self.phase,
                    extra=record.extra or None,
                )
                await self._append(returned)

    @asynccontextmanager
    async def record_tool_call(
        self,
        *,
        tool_name: str,
        args_text: str,
        caller: str | None = None,
        mcp_server: str | None = None,
        preview_cap: int = PREVIEW_DEFAULT_CHARS,
        preview_hard_cap: int = PREVIEW_HARD_CAP_CHARS_TOOL,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[ToolCallRecord]:
        """Wrap a tool dispatch and emit started/returned events."""
        record = ToolCallRecord()
        if not self.is_active:
            yield record
            return

        call_id = new_call_id()
        args_hash_value = content_hash(args_text)
        started = create_tool_call_started_event(
            target_type=self.target_type,
            target_id=self.target_id,
            call_id=call_id,
            tool_name=tool_name,
            args_hash=args_hash_value,
            args_preview=args_text,
            preview_cap=preview_cap,
            preview_hard_cap=preview_hard_cap,
            privacy=self.privacy,
            caller=caller,
            mcp_server=mcp_server,
            session_id=self.session_id,
            execution_id=self.execution_id,
            lineage_id=self.lineage_id,
            generation_number=self.generation_number,
            phase=self.phase,
            extra=extra,
        )
        started_persisted = await self._append(started)

        start_perf = time.perf_counter()
        is_error = False
        error_kind: str | None = None
        try:
            yield record
        except BaseException as exc:  # noqa: BLE001 — capture every failure
            is_error = True
            error_kind = type(exc).__name__
            raise
        finally:
            duration_ms = int((time.perf_counter() - start_perf) * 1000)
            result_hash_value = (
                content_hash(record.result_text) if record.result_text is not None else None
            )
            if started_persisted:
                returned = create_tool_call_returned_event(
                    target_type=self.target_type,
                    target_id=self.target_id,
                    call_id=call_id,
                    tool_name=tool_name,
                    duration_ms=duration_ms,
                    is_error=is_error or record.is_error,
                    result_hash=result_hash_value,
                    result_preview=record.result_text,
                    preview_cap=preview_cap,
                    preview_hard_cap=preview_hard_cap,
                    privacy=self.privacy,
                    error_kind=error_kind or record.error_kind,
                    session_id=self.session_id,
                    execution_id=self.execution_id,
                    lineage_id=self.lineage_id,
                    generation_number=self.generation_number,
                    phase=self.phase,
                    extra=record.extra or None,
                )
                await self._append(returned)

    async def _append(self, event: BaseEvent) -> bool:
        """Best-effort append, returning whether the event was persisted.

        A failed start/request append suppresses the paired return event
        so the EventStore never contains orphaned ``*.returned`` rows.
        Failures remain observational: the underlying LLM/tool call is
        never failed because the journal could not record it, but the
        drop is logged here because generic EventStore failures are not
        guaranteed to log before raising.
        """
        store = self.event_store
        if store is None:
            return False
        try:
            await store.append(event)
        except Exception:  # noqa: BLE001 — observational-first
            logger.warning(
                "io_journal.append_failed",
                extra={
                    "event_type": event.type,
                    "target_type": event.aggregate_type,
                    "target_id": event.aggregate_id,
                },
                exc_info=True,
            )
            return False
        return True
