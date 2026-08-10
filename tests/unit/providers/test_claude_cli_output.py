"""Regression coverage for Claude CLI result-shape normalization (#2037)."""

from __future__ import annotations

import json

import pytest

from ouroboros.providers.claude_cli_output import (
    MAX_CLAUDE_CLI_EVENTS,
    MAX_CLAUDE_CLI_OUTPUT_CHARS,
    ClaudeCliOutputError,
    normalize_claude_cli_output,
)


def _result(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "done",
        "session_id": "session-1",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }
    result.update(overrides)
    return result


def _events(final: dict[str, object] | None = None) -> list[dict[str, object]]:
    return [
        {
            "type": "system",
            "subtype": "init",
            "session_id": "session-1",
            "cwd": "/workspace",
        },
        {
            "type": "assistant",
            "session_id": "session-1",
            "message": {
                "content": [{"type": "text", "text": "intermediate"}],
                "usage": {"input_tokens": 3, "output_tokens": 1},
            },
        },
        final or _result(),
    ]


def test_normalizes_single_result_object() -> None:
    normalized = normalize_claude_cli_output(json.dumps(_result()))

    assert normalized.result == "done"
    assert normalized.session_id == "session-1"
    assert normalized.is_error is False
    assert normalized.usage == {"input_tokens": 11, "output_tokens": 7}
    assert normalized.stop_reason == "end_turn"
    assert normalized.event_count == 1


def test_normalizes_top_level_event_array_and_uses_terminal_result() -> None:
    normalized = normalize_claude_cli_output(json.dumps(_events()))

    assert normalized.result == "done"
    assert normalized.session_id == "session-1"
    assert normalized.event_count == 3
    assert normalized.raw_payload["type"] == "result"


def test_normalizes_ndjson_with_plain_diagnostic_lines() -> None:
    stdout = "\n".join(
        [
            "warning: optional plugin unavailable",
            *(json.dumps(event) for event in _events()),
            "diagnostic: request complete",
        ]
    )

    normalized = normalize_claude_cli_output(stdout)

    assert normalized.result == "done"
    assert normalized.event_count == 3


def test_normalizes_pretty_event_array_between_diagnostic_lines() -> None:
    stdout = "\n".join(
        [
            "warning: optional plugin unavailable",
            json.dumps(_events(), indent=2),
            "diagnostic: request complete",
        ]
    )

    normalized = normalize_claude_cli_output(stdout)

    assert normalized.result == "done"
    assert normalized.event_count == 3


def test_flattens_array_documents_inside_ndjson() -> None:
    stdout = "\n".join(
        [
            json.dumps(_events()[:2]),
            json.dumps(_events()[-1]),
        ]
    )

    normalized = normalize_claude_cli_output(stdout)

    assert normalized.result == "done"
    assert normalized.event_count == 3


@pytest.mark.parametrize(
    ("result_value", "expected"),
    [
        ([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], "ab"),
        ({"content": [{"type": "text", "text": "nested"}]}, "nested"),
    ],
)
def test_normalizes_recognized_nested_result_content(result_value: object, expected: str) -> None:
    normalized = normalize_claude_cli_output(json.dumps(_result(result=result_value)))

    assert normalized.result == expected
    assert normalized.raw_payload["result"] == expected


def test_accepts_terminal_message_content_wrapper() -> None:
    final = _result()
    final.pop("result")
    final["message"] = {"content": [{"type": "text", "text": "wrapped"}]}

    normalized = normalize_claude_cli_output(json.dumps(_events(final)))

    assert normalized.result == "wrapped"


def test_error_result_preserves_diagnostics_and_metadata() -> None:
    normalized = normalize_claude_cli_output(
        json.dumps(
            _result(
                is_error=True,
                subtype="error_max_turns",
                result="reached maximum number of turns",
            )
        )
    )

    assert normalized.is_error is True
    assert normalized.result == "reached maximum number of turns"
    assert normalized.subtype == "error_max_turns"


def test_usage_falls_back_to_nested_assistant_usage() -> None:
    final = _result()
    final.pop("usage")

    normalized = normalize_claude_cli_output(json.dumps(_events(final)))

    assert normalized.usage == {"input_tokens": 3, "output_tokens": 1}


@pytest.mark.parametrize(
    "events",
    [
        pytest.param(_events()[:-1], id="no-result"),
        pytest.param([_result(), {"type": "assistant"}], id="result-not-terminal"),
        pytest.param([_result(), _result()], id="multiple-results"),
        pytest.param(
            [{"type": "assistant"}, {"type": "system", "subtype": "init"}, _result()],
            id="late-init",
        ),
        pytest.param([], id="empty-array"),
    ],
)
def test_rejects_missing_or_ambiguous_terminal_result(events: list[dict[str, object]]) -> None:
    with pytest.raises(ClaudeCliOutputError):
        normalize_claude_cli_output(json.dumps(events))


def test_rejects_session_id_disagreement_between_init_and_result() -> None:
    with pytest.raises(ClaudeCliOutputError, match="inconsistent session_id"):
        normalize_claude_cli_output(json.dumps(_events(_result(session_id="session-2"))))


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_result(is_error="false"), id="string-error-flag"),
        pytest.param(_result(session_id=7), id="numeric-session"),
        pytest.param(_result(usage=[]), id="list-usage"),
        pytest.param(_result(usage={"input_tokens": -1}), id="negative-usage"),
        pytest.param(_result(result={"unexpected": "object"}), id="unknown-result-object"),
        pytest.param(
            _result(result=[{"type": "tool_use", "name": "Read"}]),
            id="non-text-result-block",
        ),
    ],
)
def test_rejects_hostile_or_schema_invalid_result_fields(payload: dict[str, object]) -> None:
    with pytest.raises(ClaudeCliOutputError):
        normalize_claude_cli_output(json.dumps(payload))


def test_rejects_malformed_json_looking_line_even_if_later_result_is_valid() -> None:
    stdout = '{"type":"assistant"\n' + json.dumps(_result())

    with pytest.raises(ClaudeCliOutputError, match="malformed JSON-looking"):
        normalize_claude_cli_output(stdout)


def test_rejects_garbage_appended_to_json_on_the_same_line() -> None:
    stdout = json.dumps(_result()) + " forged diagnostic"

    with pytest.raises(ClaudeCliOutputError, match="unexpected trailing data"):
        normalize_claude_cli_output(stdout)


@pytest.mark.parametrize(
    "stdout",
    [
        '{"type":"result","result":"first","result":"forged"}',
        '{"type":"result","result":"x","usage":{"input_tokens":NaN}}',
        json.dumps([_result(), "not-an-event"]),
    ],
)
def test_rejects_ambiguous_json(stdout: str) -> None:
    with pytest.raises(ClaudeCliOutputError):
        normalize_claude_cli_output(stdout)


def test_rejects_oversized_stdout_before_decoding() -> None:
    with pytest.raises(ClaudeCliOutputError, match="character safety limit"):
        normalize_claude_cli_output("x" * (MAX_CLAUDE_CLI_OUTPUT_CHARS + 1))


def test_rejects_excessive_event_count() -> None:
    events = [{"type": "assistant"}] * MAX_CLAUDE_CLI_EVENTS + [_result()]

    with pytest.raises(ClaudeCliOutputError, match="event count"):
        normalize_claude_cli_output(json.dumps(events))
