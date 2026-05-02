"""Anthropic adapter wires the I/O Journal recorder (slice 3 of #517).

The migration is intentionally additive: the legacy constructor shape
remains valid, and ``io_recorder=None`` is byte-for-byte the previous
behaviour. This module pins both branches plus the helpers the adapter
introduces for prompt hashing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.events.io_recorder import IOJournalRecorder, use_io_journal_recorder
from ouroboros.providers.anthropic_adapter import (
    AnthropicAdapter,
    _record_completion,
    _serialise_prompt_for_hash,
)
from ouroboros.providers.base import (
    CompletionConfig,
    CompletionResponse,
    Message,
    MessageRole,
    UsageInfo,
)


class _FakeEventStore:
    def __init__(self) -> None:
        self.appended: list[BaseEvent] = []

    async def append(self, event: BaseEvent) -> None:
        self.appended.append(event)


@dataclass
class _StubAnthropicResponse:
    """Minimal stand-in for the Anthropic SDK response object."""

    content: list[Any]
    model: str
    stop_reason: str
    usage: Any


class TestSerialisePromptForHash:
    def test_deterministic_for_same_input(self) -> None:
        a = _serialise_prompt_for_hash(
            [{"role": "user", "content": "hi"}],
            ["system 1"],
        )
        b = _serialise_prompt_for_hash(
            [{"role": "user", "content": "hi"}],
            ["system 1"],
        )
        assert a == b

    def test_different_for_different_input(self) -> None:
        a = _serialise_prompt_for_hash([{"role": "user", "content": "a"}], [])
        b = _serialise_prompt_for_hash([{"role": "user", "content": "b"}], [])
        assert a != b


class TestRecordCompletionHelper:
    def test_populates_record_from_parsed_response(self) -> None:
        from ouroboros.events.io_recorder import LLMCallRecord

        record = LLMCallRecord()
        parsed = CompletionResponse(
            content="hi there",
            model="claude-sonnet-4-6",
            usage=UsageInfo(prompt_tokens=10, completion_tokens=4, total_tokens=14),
            finish_reason="end_turn",
        )
        _record_completion(record, parsed)
        assert record.completion_text == "hi there"
        assert record.finish_reason == "end_turn"
        assert record.token_count_in == 10
        assert record.token_count_out == 4


class TestAdapterConstructor:
    def test_accepts_io_recorder_kwarg(self) -> None:
        recorder = IOJournalRecorder(
            event_store=_FakeEventStore(),
            target_type="execution",
            target_id="exec_test",
        )
        adapter = AnthropicAdapter(api_key="dummy", io_recorder=recorder)
        assert adapter._io_recorder is recorder

    def test_legacy_constructor_unchanged(self) -> None:
        # No io_recorder kwarg — must still construct.
        adapter = AnthropicAdapter(api_key="dummy")
        assert adapter._io_recorder is None


@pytest.mark.asyncio
async def test_complete_emits_paired_events_when_recorder_present() -> None:
    store = _FakeEventStore()
    recorder = IOJournalRecorder(
        event_store=store,
        target_type="execution",
        target_id="exec_test",
    )
    adapter = AnthropicAdapter(api_key="dummy", io_recorder=recorder)

    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "hi there"
    stub_response = _StubAnthropicResponse(
        content=[text_block],
        model="claude-sonnet-4-6",
        stop_reason="end_turn",
        usage=MagicMock(input_tokens=10, output_tokens=4),
    )

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=stub_response)
    adapter._client = fake_client

    result = await adapter.complete(
        messages=[Message(role=MessageRole.USER, content="hello")],
        config=CompletionConfig(model="claude-sonnet-4-6", max_tokens=128),
    )

    assert result.is_ok
    parsed = result.value
    assert parsed.content == "hi there"

    assert [e.type for e in store.appended] == [
        "llm.call.requested",
        "llm.call.returned",
    ]
    started, returned = store.appended
    assert started.data["call_id"] == returned.data["call_id"]
    assert started.data["caller"] == "anthropic_adapter"
    assert returned.data["finish_reason"] == "end_turn"
    assert returned.data["token_count_in"] == 10
    assert returned.data["token_count_out"] == 4
    assert returned.data["is_error"] is False


@pytest.mark.asyncio
async def test_complete_uses_scoped_recorder_for_shared_adapter() -> None:
    store = _FakeEventStore()
    recorder = IOJournalRecorder(
        event_store=store,
        target_type="execution",
        target_id="exec_scoped",
        execution_id="exec_scoped",
    )
    adapter = AnthropicAdapter(api_key="dummy")

    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "hi scoped"
    stub_response = _StubAnthropicResponse(
        content=[text_block],
        model="claude-sonnet-4-6",
        stop_reason="end_turn",
        usage=MagicMock(input_tokens=10, output_tokens=4),
    )

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=stub_response)
    adapter._client = fake_client

    with use_io_journal_recorder(recorder):
        result = await adapter.complete(
            messages=[Message(role=MessageRole.USER, content="hello")],
            config=CompletionConfig(model="claude-sonnet-4-6", max_tokens=128),
        )

    assert result.is_ok
    assert store.appended[0].data["target_id"] == "exec_scoped"
    assert store.appended[1].data["execution_id"] == "exec_scoped"


@pytest.mark.asyncio
async def test_complete_does_not_emit_when_recorder_absent() -> None:
    """When io_recorder is None the adapter behaves exactly like before."""
    adapter = AnthropicAdapter(api_key="dummy")  # no recorder

    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "hi"
    stub_response = _StubAnthropicResponse(
        content=[text_block],
        model="claude-sonnet-4-6",
        stop_reason="end_turn",
        usage=MagicMock(input_tokens=2, output_tokens=1),
    )
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=stub_response)
    adapter._client = fake_client

    result = await adapter.complete(
        messages=[Message(role=MessageRole.USER, content="hello")],
        config=CompletionConfig(model="claude-sonnet-4-6", max_tokens=8),
    )
    assert result.is_ok


@pytest.mark.asyncio
async def test_complete_emits_returned_with_is_error_on_exception() -> None:
    store = _FakeEventStore()
    recorder = IOJournalRecorder(
        event_store=store,
        target_type="execution",
        target_id="exec_err",
    )
    adapter = AnthropicAdapter(api_key="dummy", io_recorder=recorder)

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(side_effect=RuntimeError("simulated provider failure"))
    adapter._client = fake_client

    result = await adapter.complete(
        messages=[Message(role=MessageRole.USER, content="hello")],
        config=CompletionConfig(model="claude-sonnet-4-6", max_tokens=8),
    )

    # The adapter swallows the exception via its existing _handle_error
    # path and returns a Result.err. Inspecting the journal still shows
    # the failure rather than a half-open call.
    assert result.is_err

    assert [e.type for e in store.appended] == [
        "llm.call.requested",
        "llm.call.returned",
    ]
    returned = store.appended[1]
    assert returned.data["is_error"] is True
    assert returned.data["error_kind"] == "RuntimeError"


def test_prompt_hash_serialisation_matches_wire_system_join() -> None:
    split = _serialise_prompt_for_hash(
        [{"role": "user", "content": "hi"}],
        ["a", "b"],
    )
    joined = _serialise_prompt_for_hash(
        [{"role": "user", "content": "hi"}],
        ["a\n\nb"],
    )
    assert split == joined


def test_prompt_hash_serialisation_includes_request_options() -> None:
    base = _serialise_prompt_for_hash(
        [{"role": "user", "content": "hi"}],
        [],
        {"top_p": 0.9, "stop_sequences": ["STOP"]},
    )
    changed = _serialise_prompt_for_hash(
        [{"role": "user", "content": "hi"}],
        [],
        {"top_p": 0.8, "stop_sequences": ["STOP"]},
    )
    assert base != changed


@pytest.mark.asyncio
async def test_complete_records_top_p_and_stop_sequences_in_journal_extra() -> None:
    store = _FakeEventStore()
    recorder = IOJournalRecorder(
        event_store=store,
        target_type="execution",
        target_id="exec_options",
    )
    adapter = AnthropicAdapter(api_key="dummy", io_recorder=recorder)

    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "hi"
    stub_response = _StubAnthropicResponse(
        content=[text_block],
        model="claude-sonnet-4-6",
        stop_reason="end_turn",
        usage=MagicMock(input_tokens=2, output_tokens=1),
    )
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=stub_response)
    adapter._client = fake_client

    result = await adapter.complete(
        messages=[
            Message(role=MessageRole.SYSTEM, content="sys a"),
            Message(role=MessageRole.SYSTEM, content="sys b"),
            Message(role=MessageRole.USER, content="hello"),
        ],
        config=CompletionConfig(
            model="claude-sonnet-4-6",
            max_tokens=8,
            top_p=0.7,
            stop=["STOP"],
        ),
    )

    assert result.is_ok
    started = store.appended[0]
    assert started.data["extra"] == {"top_p": 0.7, "stop_sequences": ["STOP"]}
    fake_client.messages.create.assert_awaited_once()
    kwargs = fake_client.messages.create.await_args.kwargs
    assert kwargs["system"] == "sys a\n\nsys b"
    assert kwargs["top_p"] == 0.7
    assert kwargs["stop_sequences"] == ["STOP"]
