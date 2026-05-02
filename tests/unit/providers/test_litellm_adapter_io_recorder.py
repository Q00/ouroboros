"""LiteLLM adapter wires the I/O Journal recorder (slice 4 of #517).

Same shape as the Anthropic-adapter slice (#535): legacy constructor
remains valid, ``io_recorder=None`` is byte-for-byte the previous
behaviour, and per-attempt emission lands a paired
``llm.call.requested`` / ``llm.call.returned`` for every retry attempt.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.security import MAX_LLM_RESPONSE_LENGTH
from ouroboros.events.base import BaseEvent
from ouroboros.events.io import content_hash
from ouroboros.events.io_recorder import (
    IOJournalRecorder,
    LLMCallRecord,
    use_io_journal_recorder,
)
from ouroboros.providers.base import (
    CompletionConfig,
    CompletionResponse,
    Message,
    MessageRole,
    UsageInfo,
)
from ouroboros.providers.litellm_adapter import (
    LiteLLMAdapter,
    _record_litellm_completion,
    _serialise_messages_for_hash,
)


class _FakeEventStore:
    def __init__(self) -> None:
        self.appended: list[BaseEvent] = []

    async def append(self, event: BaseEvent) -> None:
        self.appended.append(event)


def _stub_response(
    *,
    content: str = "hi",
    finish_reason: str = "stop",
    prompt_tokens: int = 10,
    completion_tokens: int = 4,
    model: str = "openrouter/openai/gpt-4",
) -> Any:
    """Build a MagicMock that mirrors a litellm ModelResponse."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    response.choices[0].finish_reason = finish_reason
    response.usage = MagicMock(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
    response.model = model
    response.model_dump = lambda: {"stub": True}
    return response


class TestSerialiseMessagesForHash:
    def test_deterministic_for_same_input(self) -> None:
        a = _serialise_messages_for_hash([Message(role=MessageRole.USER, content="hi")])
        b = _serialise_messages_for_hash([Message(role=MessageRole.USER, content="hi")])
        assert a == b

    def test_different_for_different_input(self) -> None:
        a = _serialise_messages_for_hash([Message(role=MessageRole.USER, content="a")])
        b = _serialise_messages_for_hash([Message(role=MessageRole.USER, content="b")])
        assert a != b


class TestRecordLitellmCompletion:
    def test_populates_record_from_parsed_response(self) -> None:
        record = LLMCallRecord()
        parsed = CompletionResponse(
            content="hi",
            model="openrouter/openai/gpt-4",
            usage=UsageInfo(prompt_tokens=10, completion_tokens=4, total_tokens=14),
            finish_reason="stop",
        )
        _record_litellm_completion(record, parsed)
        assert record.completion_text == "hi"
        assert record.finish_reason == "stop"
        assert record.token_count_in == 10
        assert record.token_count_out == 4

    def test_handles_missing_usage_gracefully(self) -> None:
        parsed = CompletionResponse(
            content="hi",
            model="openrouter/openai/gpt-4",
            usage=None,
            finish_reason="stop",
        )
        record = LLMCallRecord()
        _record_litellm_completion(record, parsed)
        assert record.completion_text == "hi"
        assert record.token_count_in is None
        assert record.token_count_out is None


class TestAdapterConstructor:
    def test_accepts_io_recorder_kwarg(self) -> None:
        recorder = IOJournalRecorder(
            event_store=_FakeEventStore(),
            target_type="execution",
            target_id="exec_test",
        )
        adapter = LiteLLMAdapter(api_key="dummy", io_recorder=recorder)
        assert adapter._io_recorder is recorder

    def test_legacy_constructor_unchanged(self) -> None:
        adapter = LiteLLMAdapter(api_key="dummy")
        assert adapter._io_recorder is None


def test_prompt_hash_serialisation_includes_request_options() -> None:
    base = _serialise_messages_for_hash(
        [Message(role=MessageRole.USER, content="hi")],
        {"top_p": 0.9, "stop": ["STOP"]},
    )
    changed = _serialise_messages_for_hash(
        [Message(role=MessageRole.USER, content="hi")],
        {"top_p": 0.8, "stop": ["STOP"]},
    )
    assert base != changed


@pytest.mark.asyncio
async def test_complete_records_request_options_in_journal_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeEventStore()
    recorder = IOJournalRecorder(
        event_store=store,
        target_type="execution",
        target_id="exec_options",
    )
    adapter = LiteLLMAdapter(api_key="dummy", io_recorder=recorder)

    response = _stub_response()
    monkeypatch.setattr(adapter, "_raw_complete", AsyncMock(return_value=response))

    result = await adapter.complete(
        messages=[Message(role=MessageRole.USER, content="hello")],
        config=CompletionConfig(
            model="openrouter/openai/gpt-4",
            max_tokens=64,
            top_p=0.7,
            stop=["STOP"],
            response_format={"type": "json_object"},
        ),
    )
    assert result.is_ok

    assert store.appended[0].data["extra"] == {
        "top_p": 0.7,
        "stop": ["STOP"],
        "response_format": {"type": "json_object"},
    }


@pytest.mark.asyncio
async def test_complete_records_truncated_completion_seen_by_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeEventStore()
    recorder = IOJournalRecorder(
        event_store=store,
        target_type="execution",
        target_id="exec_truncated",
    )
    adapter = LiteLLMAdapter(api_key="dummy", io_recorder=recorder)

    too_long = "x" * (MAX_LLM_RESPONSE_LENGTH + 1)
    response = _stub_response(content=too_long)
    monkeypatch.setattr(adapter, "_raw_complete", AsyncMock(return_value=response))

    result = await adapter.complete(
        messages=[Message(role=MessageRole.USER, content="hello")],
        config=CompletionConfig(model="openrouter/openai/gpt-4", max_tokens=64),
    )
    assert result.is_ok
    assert len(result.value.content) == MAX_LLM_RESPONSE_LENGTH

    returned = store.appended[1]
    assert returned.data["completion_hash"] == content_hash(result.value.content)
    assert returned.data["completion_preview"].startswith("x")
    assert len(returned.data["completion_preview"]) < len(too_long)


@pytest.mark.asyncio
async def test_complete_emits_paired_events_when_recorder_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeEventStore()
    recorder = IOJournalRecorder(
        event_store=store,
        target_type="execution",
        target_id="exec_test",
    )
    adapter = LiteLLMAdapter(api_key="dummy", io_recorder=recorder)

    response = _stub_response()
    monkeypatch.setattr(adapter, "_raw_complete", AsyncMock(return_value=response))

    result = await adapter.complete(
        messages=[Message(role=MessageRole.USER, content="hello")],
        config=CompletionConfig(model="openrouter/openai/gpt-4", max_tokens=64),
    )
    assert result.is_ok

    assert [e.type for e in store.appended] == [
        "llm.call.requested",
        "llm.call.returned",
    ]
    started, returned = store.appended
    assert started.data["call_id"] == returned.data["call_id"]
    assert started.data["caller"] == "litellm_adapter"
    assert returned.data["finish_reason"] == "stop"
    assert returned.data["token_count_in"] == 10
    assert returned.data["is_error"] is False


@pytest.mark.asyncio
async def test_complete_uses_scoped_recorder_for_shared_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_a = _FakeEventStore()
    store_b = _FakeEventStore()
    recorder_a = IOJournalRecorder(
        event_store=store_a,
        target_type="execution",
        target_id="exec_a",
        execution_id="exec_a",
    )
    recorder_b = IOJournalRecorder(
        event_store=store_b,
        target_type="lineage",
        target_id="lin_b",
        lineage_id="lin_b",
    )
    adapter = LiteLLMAdapter(api_key="dummy")

    monkeypatch.setattr(
        adapter,
        "_raw_complete",
        AsyncMock(side_effect=[_stub_response(content="a"), _stub_response(content="b")]),
    )

    with use_io_journal_recorder(recorder_a):
        result_a = await adapter.complete(
            messages=[Message(role=MessageRole.USER, content="hello a")],
            config=CompletionConfig(model="openrouter/openai/gpt-4", max_tokens=64),
        )
    with use_io_journal_recorder(recorder_b):
        result_b = await adapter.complete(
            messages=[Message(role=MessageRole.USER, content="hello b")],
            config=CompletionConfig(model="openrouter/openai/gpt-4", max_tokens=64),
        )

    assert result_a.is_ok
    assert result_b.is_ok
    assert store_a.appended[0].data["target_id"] == "exec_a"
    assert store_a.appended[1].data["execution_id"] == "exec_a"
    assert store_b.appended[0].data["target_id"] == "lin_b"
    assert store_b.appended[1].data["lineage_id"] == "lin_b"


@pytest.mark.asyncio
async def test_complete_does_not_emit_when_recorder_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = LiteLLMAdapter(api_key="dummy")  # no recorder

    response = _stub_response()
    monkeypatch.setattr(adapter, "_raw_complete", AsyncMock(return_value=response))

    result = await adapter.complete(
        messages=[Message(role=MessageRole.USER, content="hello")],
        config=CompletionConfig(model="openrouter/openai/gpt-4", max_tokens=8),
    )
    assert result.is_ok


@pytest.mark.asyncio
async def test_complete_emits_returned_with_is_error_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeEventStore()
    recorder = IOJournalRecorder(
        event_store=store,
        target_type="execution",
        target_id="exec_err",
    )
    adapter = LiteLLMAdapter(
        api_key="dummy",
        io_recorder=recorder,
        max_retries=1,
    )

    monkeypatch.setattr(
        adapter,
        "_raw_complete",
        AsyncMock(side_effect=RuntimeError("simulated provider failure")),
    )

    result = await adapter.complete(
        messages=[Message(role=MessageRole.USER, content="hello")],
        config=CompletionConfig(model="openrouter/openai/gpt-4", max_tokens=8),
    )
    assert result.is_err

    # The recorder still emitted a paired returned event for each
    # attempt — at least one attempt should be recorded.
    assert any(e.type == "llm.call.returned" for e in store.appended)
    failure_events = [
        e
        for e in store.appended
        if e.type == "llm.call.returned" and e.data.get("is_error") is True
    ]
    assert failure_events
    assert failure_events[0].data["error_kind"] == "RuntimeError"
