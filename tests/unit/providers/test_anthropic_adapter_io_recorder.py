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

from ouroboros.core.errors import ProviderError
from ouroboros.core.json_utils import extract_json_payload
from ouroboros.core.security import MAX_LLM_RESPONSE_LENGTH
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


def _parse_content(content: str, *, json_prefill: bool) -> str:
    text_block = MagicMock(type="text", text=content)
    response = _StubAnthropicResponse(
        content=[text_block],
        model="claude-sonnet-4-6",
        stop_reason="end_turn",
        usage=MagicMock(input_tokens=1, output_tokens=1),
    )
    return (
        AnthropicAdapter(api_key="dummy")
        ._parse_response(
            response,
            "claude-sonnet-4-6",
            json_prefill=json_prefill,
        )
        .content
    )


def _parse_prefill_content(content: str) -> str:
    return _parse_content(content, json_prefill=True)


def _oversized_prefill_suffix() -> str:
    prefix_start = '"payload": "'
    prefix_end = '"}'
    padding = "x" * (MAX_LLM_RESPONSE_LENGTH - len(prefix_start) - len(prefix_end))
    valid_at_limit = f"{prefix_start}{padding}{prefix_end}"
    assert len(valid_at_limit) == MAX_LLM_RESPONSE_LENGTH
    return valid_at_limit + "TRAILING"


class TestJsonPrefillResponseBoundary:
    def test_valid_object_continuation_restores_synthetic_opener(self) -> None:
        continuation = '"ok": true, "nested": {"value": 1}}'

        normalized = _parse_prefill_content(continuation)

        assert normalized == '{"ok": true, "nested": {"value": 1}}'

    def test_oversized_suffix_is_rejected_before_truncation_can_make_it_valid(self) -> None:
        with pytest.raises(ProviderError, match="safe length boundary") as exc_info:
            _parse_prefill_content(_oversized_prefill_suffix())

        assert exc_info.value.details["error_type"] == "invalid_json_prefill_response_length"
        assert exc_info.value.details["original_length"] > MAX_LLM_RESPONSE_LENGTH

    def test_oversized_response_without_prefill_keeps_legacy_clamp(self) -> None:
        content = "x" * (MAX_LLM_RESPONSE_LENGTH + 7)

        normalized = _parse_content(content, json_prefill=False)

        assert normalized == content[:MAX_LLM_RESPONSE_LENGTH]

    def test_malformed_quoted_key_continuation_keeps_opener_and_fails_closed(self) -> None:
        continuation = '  "draft": {"stale": true}'

        with pytest.raises(ProviderError, match="did not complete a valid object") as exc_info:
            _parse_prefill_content(continuation)

        assert exc_info.value.details["error_type"] == "invalid_json_prefill_response"

    @pytest.mark.parametrize(
        "continuation",
        [
            '} prose {"stale": true}',
            ', "draft": {"stale": true}',
            'draft: {"stale": true}',
        ],
        ids=["closing-brace", "comma", "unquoted-key"],
    )
    def test_malformed_object_continuation_markers_fail_closed(self, continuation: str) -> None:
        with pytest.raises(ProviderError, match="did not complete a valid object"):
            _parse_prefill_content(continuation)

    @pytest.mark.parametrize(
        "continuation",
        [
            '"draft":\n```json\n{"stale": true}\n```',
            'draft:\n```json\n{"stale": true}\n```',
            ', draft:\n```json\n{"stale": true}\n```',
        ],
        ids=["quoted-key-fence", "unquoted-key-fence", "comma-fence"],
    )
    def test_fenced_nested_payload_never_leaves_prefill_boundary(self, continuation: str) -> None:
        with pytest.raises(ProviderError, match="did not complete a valid object"):
            _parse_prefill_content(continuation)

    @pytest.mark.parametrize(
        "content",
        ['{"ok": true}', '[{"ok": true}]'],
        ids=["full-object", "full-array"],
    )
    def test_full_json_bytes_are_invalid_when_request_used_prefill(self, content: str) -> None:
        with pytest.raises(ProviderError, match="did not complete a valid object"):
            _parse_prefill_content(content)

    @pytest.mark.parametrize(
        "continuation",
        [
            '{"stale": true}}',
            '[{"stale": true}]]',
            'Let me explain.\n\n{"stale": true}',
        ],
        ids=["object-bytes", "array-bytes", "prose-json"],
    )
    def test_prefill_never_infers_ignored_assistant_prefix(self, continuation: str) -> None:
        with pytest.raises(ProviderError, match="did not complete a valid object"):
            _parse_prefill_content(continuation)

    @pytest.mark.parametrize(
        "content",
        ['{"ok": true}', '[{"ok": true}]'],
        ids=["full-object", "full-array"],
    )
    def test_full_json_answer_without_prefill_remains_raw(self, content: str) -> None:
        normalized = _parse_content(content, json_prefill=False)

        assert normalized == content
        assert extract_json_payload(normalized) == content

    def test_prose_then_json_is_rejected_when_request_used_prefill(self) -> None:
        payload = '{\r\n  "outer": {"value": 1},\r\n \t\r\n  "items": [1, 2]\r\n}'
        content = f"Let me inspect the response.\r\n  \r\n{payload}\r\n\t\r\n"

        with pytest.raises(ProviderError, match="did not complete a valid object"):
            _parse_prefill_content(content)

        normalized = _parse_content(content, json_prefill=False)

        assert normalized == content
        assert extract_json_payload(normalized) == payload

    def test_prose_with_two_json_answers_is_rejected_at_prefill_boundary(self) -> None:
        content = 'Example:\n\n{"a": 1}\nActual:\n\n{"b": 2}'

        with pytest.raises(ProviderError, match="did not complete a valid object"):
            _parse_prefill_content(content)

        normalized = _parse_content(content, json_prefill=False)
        assert extract_json_payload(normalized) is None

    def test_malformed_prose_answer_is_rejected_at_prefill_boundary(self) -> None:
        content = 'Let me explain.\n\n{"unfinished": true'

        with pytest.raises(ProviderError, match="did not complete a valid object"):
            _parse_prefill_content(content)

        normalized = _parse_content(content, json_prefill=False)
        assert extract_json_payload(normalized) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "consumer_name",
    ["wonder", "reflect", "semantic", "consensus", "judgment", "qa"],
)
@pytest.mark.parametrize(
    "continuation",
    [
        '"draft":\n```json\n{"stale": true}\n```',
        'draft:\n```json\n{"stale": true}\n```',
        ', draft:\n```json\n{"stale": true}\n```',
    ],
    ids=["quoted-key", "unquoted-key", "comma"],
)
async def test_invalid_prefill_response_cannot_reach_downstream_consumer(
    consumer_name: str,
    continuation: str,
) -> None:
    adapter = AnthropicAdapter(api_key="dummy")
    text_block = MagicMock(
        type="text",
        text=continuation,
    )
    response = _StubAnthropicResponse(
        content=[text_block],
        model="claude-sonnet-4-6",
        stop_reason="end_turn",
        usage=MagicMock(input_tokens=1, output_tokens=1),
    )
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=response)
    adapter._client = fake_client
    downstream_consumer = MagicMock(name=consumer_name)

    result = await adapter.complete(
        messages=[Message(role=MessageRole.USER, content="return JSON")],
        config=CompletionConfig(
            model="claude-sonnet-4-6",
            response_format={"type": "json_object"},
        ),
    )
    if result.is_ok:
        downstream_consumer(result.value.content)

    assert result.is_err
    assert result.error.details["error_type"] == "invalid_json_prefill_response"
    downstream_consumer.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_recorder", [False, True], ids=["plain", "recorder"])
async def test_complete_rejects_oversized_prefill_before_content_mutation(
    with_recorder: bool,
) -> None:
    store = _FakeEventStore()
    recorder = (
        IOJournalRecorder(
            event_store=store,
            target_type="execution",
            target_id="exec_oversized_prefill",
        )
        if with_recorder
        else None
    )
    adapter = AnthropicAdapter(api_key="dummy", io_recorder=recorder)
    text_block = MagicMock(type="text", text=_oversized_prefill_suffix())
    response = _StubAnthropicResponse(
        content=[text_block],
        model="claude-sonnet-4-6",
        stop_reason="end_turn",
        usage=MagicMock(input_tokens=1, output_tokens=1),
    )
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=response)
    adapter._client = fake_client
    downstream_consumer = MagicMock()

    result = await adapter.complete(
        messages=[Message(role=MessageRole.USER, content="return JSON")],
        config=CompletionConfig(
            model="claude-sonnet-4-6",
            response_format={"type": "json_object"},
        ),
    )
    if result.is_ok:
        downstream_consumer(result.value.content)

    assert result.is_err
    assert result.error.details["error_type"] == "invalid_json_prefill_response_length"
    downstream_consumer.assert_not_called()
    if with_recorder:
        assert [event.type for event in store.appended] == [
            "llm.call.requested",
            "llm.call.returned",
        ]
        returned = store.appended[1]
        assert returned.data["is_error"] is True
        assert returned.data["error_kind"] == "ProviderError"
        assert "completion_hash" not in returned.data
    else:
        assert store.appended == []


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
    assert started.data["extra"] == {
        "top_p": 0.7,
        "stop_sequences": ["STOP"],
        "reasoning_effort": None,
    }
    fake_client.messages.create.assert_awaited_once()
    kwargs = fake_client.messages.create.await_args.kwargs
    assert kwargs["system"] == "sys a\n\nsys b"
    assert kwargs["extra_body"]["top_p"] == 0.7
    assert kwargs["stop_sequences"] == ["STOP"]


@pytest.mark.asyncio
async def test_complete_maps_reasoning_effort_to_adaptive_output_config() -> None:
    """Claude 4 effort requires output_config.effort plus adaptive thinking."""
    adapter = AnthropicAdapter(api_key="dummy")
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "ok"
    stub_response = _StubAnthropicResponse(
        content=[text_block],
        model="claude-sonnet-4-6",
        stop_reason="end_turn",
        usage=MagicMock(input_tokens=1, output_tokens=1),
    )
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=stub_response)
    adapter._client = fake_client

    await adapter.complete(
        messages=[Message(role=MessageRole.USER, content="hi")],
        config=CompletionConfig(model="claude-sonnet-4-6", reasoning_effort="high"),
    )

    kwargs = fake_client.messages.create.call_args.kwargs
    assert kwargs["output_config"] == {"effort": "high"}
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in kwargs


@pytest.mark.parametrize(
    "model",
    ["claude-fable-5-20260101", "claude-mythos-5-20260101"],
)
@pytest.mark.asyncio
async def test_complete_omits_unsupported_fable_mythos_fields_for_effort(
    model: str,
) -> None:
    """Fable/Mythos 5 effort requests omit unsupported sampling and prefill."""
    adapter = AnthropicAdapter(api_key="dummy")
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = '{"ok": true}'
    stub_response = _StubAnthropicResponse(
        content=[text_block],
        model=model,
        stop_reason="end_turn",
        usage=MagicMock(input_tokens=1, output_tokens=1),
    )
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=stub_response)
    adapter._client = fake_client

    await adapter.complete(
        messages=[Message(role=MessageRole.USER, content="hi")],
        config=CompletionConfig(
            model=model,
            temperature=0.2,
            top_p=0.4,
            response_format={"type": "json_object"},
            reasoning_effort="medium",
        ),
    )

    kwargs = fake_client.messages.create.call_args.kwargs
    assert kwargs["output_config"] == {"effort": "medium"}
    assert "thinking" not in kwargs
    assert "budget_tokens" not in kwargs
    assert "temperature" not in kwargs
    assert "top_p" not in kwargs
    assert "extra_body" not in kwargs
    assert {"role": "assistant", "content": "{"} not in kwargs["messages"]
    assert "valid JSON object" in kwargs["system"]


@pytest.mark.asyncio
async def test_complete_omits_output_config_when_no_effort() -> None:
    adapter = AnthropicAdapter(api_key="dummy")
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "ok"
    stub_response = _StubAnthropicResponse(
        content=[text_block],
        model="claude-sonnet-4-6",
        stop_reason="end_turn",
        usage=MagicMock(input_tokens=1, output_tokens=1),
    )
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=stub_response)
    adapter._client = fake_client

    await adapter.complete(
        messages=[Message(role=MessageRole.USER, content="hi")],
        config=CompletionConfig(model="claude-sonnet-4-6"),
    )

    assert "output_config" not in fake_client.messages.create.call_args.kwargs
