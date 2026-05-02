"""Unit tests for :class:`IOJournalRecorder` (slice 2 of #517).

Coverage:
- ``record_llm_call`` emits a paired ``llm.call.requested`` /
  ``llm.call.returned`` with a shared ``call_id``, the prompt hash,
  and the privacy-aware preview.
- The completion fields the caller fills on the yielded record (text,
  finish_reason, token counts) appear on the returned event.
- An exception inside the context block is re-raised but the recorder
  still emits a returned event with ``is_error=True`` and the
  exception type name in ``error_kind``.
- ``record_tool_call`` mirrors the above shape for tool dispatch.
- ``event_store=None`` produces a no-op recorder that never emits.
- ``duration_ms`` is non-negative and roughly tracks the wall-clock
  spent inside the context block.
- An EventStore that raises during ``append`` does not propagate the
  failure (observational-first stance).
"""

from __future__ import annotations

import asyncio

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.events.io import (
    PRIVACY_ENV_VAR,
    REDACTION_MARKER_TEMPLATE,
    PrivacyMode,
    content_hash,
)
from ouroboros.events.io_recorder import (
    IOJournalRecorder,
    get_current_io_journal_recorder,
    use_io_journal_recorder,
)


class _FakeEventStore:
    def __init__(self) -> None:
        self.appended: list[BaseEvent] = []

    async def append(self, event: BaseEvent) -> None:
        self.appended.append(event)


class _BrokenEventStore:
    async def append(self, event: BaseEvent) -> None:
        raise RuntimeError("simulated EventStore outage")


def test_scoped_recorder_context_resets_after_exit() -> None:
    recorder = IOJournalRecorder(
        event_store=_FakeEventStore(),
        target_type="execution",
        target_id="exec_ctx",
    )

    assert get_current_io_journal_recorder() is None
    with use_io_journal_recorder(recorder):
        assert get_current_io_journal_recorder() is recorder
    assert get_current_io_journal_recorder() is None


@pytest.mark.asyncio
async def test_record_llm_call_emits_started_and_returned() -> None:
    store = _FakeEventStore()
    recorder = IOJournalRecorder(
        event_store=store,
        target_type="execution",
        target_id="exec_123",
        execution_id="exec_123",
    )

    async with recorder.record_llm_call(
        model_id="claude-opus-4",
        prompt_text="hello",
        caller="anthropic_adapter",
        max_tokens=2048,
    ) as call:
        call.record_completion(
            completion_text="hi there",
            finish_reason="stop",
            token_count_in=10,
            token_count_out=4,
        )

    assert [e.type for e in store.appended] == [
        "llm.call.requested",
        "llm.call.returned",
    ]

    started, returned = store.appended
    assert started.data["call_id"] == returned.data["call_id"]
    assert started.data["model_id"] == "claude-opus-4"
    assert started.data["prompt_hash"] == content_hash("hello")
    assert started.data["caller"] == "anthropic_adapter"
    assert started.data["max_tokens"] == 2048

    assert returned.data["finish_reason"] == "stop"
    assert returned.data["token_count_in"] == 10
    assert returned.data["token_count_out"] == 4
    assert returned.data["completion_hash"] == content_hash("hi there")
    assert returned.data["is_error"] is False
    assert returned.data["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_record_llm_call_records_exception_and_re_raises() -> None:
    store = _FakeEventStore()
    recorder = IOJournalRecorder(
        event_store=store,
        target_type="execution",
        target_id="exec_err",
    )

    class _SimulatedFailure(RuntimeError):
        pass

    with pytest.raises(_SimulatedFailure):
        async with recorder.record_llm_call(
            model_id="m",
            prompt_text="p",
        ) as _call:
            raise _SimulatedFailure("network blew up")

    assert len(store.appended) == 2
    returned = store.appended[1]
    assert returned.type == "llm.call.returned"
    assert returned.data["is_error"] is True
    assert returned.data["error_kind"] == "_SimulatedFailure"


@pytest.mark.asyncio
async def test_record_tool_call_emits_started_and_returned() -> None:
    store = _FakeEventStore()
    recorder = IOJournalRecorder(
        event_store=store,
        target_type="execution",
        target_id="exec_tool",
    )

    async with recorder.record_tool_call(
        tool_name="filesystem.read",
        args_text='{"path": "/etc/hosts"}',
        caller="evolver",
        mcp_server="filesystem",
    ) as call:
        call.record_result(result_text="ok bytes")

    assert [e.type for e in store.appended] == [
        "tool.call.started",
        "tool.call.returned",
    ]
    started, returned = store.appended
    assert started.data["tool_name"] == "filesystem.read"
    assert started.data["args_hash"] == content_hash('{"path": "/etc/hosts"}')
    assert started.data["mcp_server"] == "filesystem"
    assert returned.data["result_hash"] == content_hash("ok bytes")
    assert returned.data["is_error"] is False
    assert returned.data["call_id"] == started.data["call_id"]


@pytest.mark.asyncio
async def test_no_op_when_event_store_is_none() -> None:
    recorder = IOJournalRecorder(
        event_store=None,
        target_type="execution",
        target_id="exec_none",
    )
    assert recorder.is_active is False

    async with recorder.record_llm_call(
        model_id="m",
        prompt_text="p",
    ) as call:
        call.record_completion(completion_text="anything", finish_reason="stop")
    # No appendable store, nothing to assert beyond "no error".


@pytest.mark.asyncio
async def test_privacy_mode_redacted_replaces_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PRIVACY_ENV_VAR, "redacted")
    store = _FakeEventStore()
    recorder = IOJournalRecorder(
        event_store=store,
        target_type="execution",
        target_id="exec_priv",
    )

    async with recorder.record_llm_call(
        model_id="m",
        prompt_text="secret prompt",
    ) as call:
        call.record_completion(completion_text="secret reply", finish_reason="stop")

    started, returned = store.appended
    assert started.data["prompt_preview"] == REDACTION_MARKER_TEMPLATE.format(
        length=len("secret prompt")
    )
    assert returned.data["completion_preview"] == REDACTION_MARKER_TEMPLATE.format(
        length=len("secret reply")
    )


@pytest.mark.asyncio
async def test_explicit_privacy_arg_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PRIVACY_ENV_VAR, "redacted")
    store = _FakeEventStore()
    recorder = IOJournalRecorder(
        event_store=store,
        target_type="execution",
        target_id="exec_priv",
        privacy=PrivacyMode.OFF,
    )

    async with recorder.record_llm_call(
        model_id="m",
        prompt_text="secret prompt",
    ) as call:
        call.record_completion(completion_text="secret reply", finish_reason="stop")

    started, returned = store.appended
    assert "prompt_preview" not in started.data
    assert "completion_preview" not in returned.data


@pytest.mark.asyncio
async def test_duration_tracks_wall_clock_inside_block() -> None:
    store = _FakeEventStore()
    recorder = IOJournalRecorder(
        event_store=store,
        target_type="execution",
        target_id="exec_time",
    )

    async with recorder.record_llm_call(
        model_id="m",
        prompt_text="p",
    ) as call:
        await asyncio.sleep(0.02)
        call.record_completion(completion_text="x", finish_reason="stop")

    returned = store.appended[1]
    assert returned.data["duration_ms"] >= 15  # generous floor for CI jitter


@pytest.mark.asyncio
async def test_event_store_failure_does_not_propagate() -> None:
    """Observational-first: a broken EventStore must never break the LLM call."""
    store = _BrokenEventStore()
    recorder = IOJournalRecorder(
        event_store=store,
        target_type="execution",
        target_id="exec_broken",
    )

    # No exception is raised even though every store.append() call fails.
    async with recorder.record_llm_call(
        model_id="m",
        prompt_text="p",
    ) as call:
        call.record_completion(completion_text="ok", finish_reason="stop")


class _FailFirstEventStore:
    def __init__(self) -> None:
        self.appended: list[BaseEvent] = []
        self.calls = 0

    async def append(self, event: BaseEvent) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("start append failed")
        self.appended.append(event)


@pytest.mark.asyncio
async def test_started_append_failure_suppresses_orphaned_returned_event(caplog) -> None:
    store = _FailFirstEventStore()
    recorder = IOJournalRecorder(
        event_store=store,
        target_type="execution",
        target_id="exec_partial",
    )

    async with recorder.record_llm_call(model_id="m", prompt_text="p") as call:
        call.record_completion(completion_text="ok", finish_reason="stop")

    assert store.appended == []
    assert store.calls == 1
    assert "io_journal.append_failed" in caplog.text


@pytest.mark.asyncio
async def test_recorder_privacy_override_and_preview_cap_are_honored(monkeypatch) -> None:
    monkeypatch.setenv(PRIVACY_ENV_VAR, "off")
    store = _FakeEventStore()
    recorder = IOJournalRecorder(
        event_store=store,
        target_type="execution",
        target_id="exec_override",
        privacy=PrivacyMode.ON,
    )

    async with recorder.record_llm_call(
        model_id="m",
        prompt_text="abcdef",
        preview_cap=3,
    ) as call:
        call.record_completion(completion_text="ghijkl", finish_reason="stop")

    started, returned = store.appended
    assert started.data["prompt_preview"].startswith("abc")
    assert "truncated len=3" in started.data["prompt_preview"]
    assert returned.data["completion_preview"].startswith("ghi")
    assert "truncated len=3" in returned.data["completion_preview"]


@pytest.mark.asyncio
async def test_record_llm_call_allows_handled_error_marking() -> None:
    store = _FakeEventStore()
    recorder = IOJournalRecorder(
        event_store=store, target_type="execution", target_id="exec_handled"
    )

    async with recorder.record_llm_call(model_id="m", prompt_text="p") as call:
        call.record_error(error_kind="ProviderError", completion_text="error payload")

    returned = store.appended[1]
    assert returned.data["is_error"] is True
    assert returned.data["error_kind"] == "ProviderError"
    assert returned.data["completion_hash"] == content_hash("error payload")


@pytest.mark.asyncio
async def test_record_tool_call_allows_handled_error_marking() -> None:
    store = _FakeEventStore()
    recorder = IOJournalRecorder(
        event_store=store, target_type="execution", target_id="exec_tool_err"
    )

    async with recorder.record_tool_call(tool_name="tool", args_text="{}") as call:
        call.record_error(error_kind="ToolError", result_text="bad result")

    returned = store.appended[1]
    assert returned.data["is_error"] is True
    assert returned.data["error_kind"] == "ToolError"
    assert returned.data["result_hash"] == content_hash("bad result")
