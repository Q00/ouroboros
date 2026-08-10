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
    assert normalized.usage == {
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
    }
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


@pytest.mark.parametrize("separator", ["", " ", "\t"])
def test_rejects_concatenated_json_documents_without_newline(separator: str) -> None:
    stdout = json.dumps({"type": "assistant"}) + separator + json.dumps(_result())

    with pytest.raises(ClaudeCliOutputError, match="newline boundary"):
        normalize_claude_cli_output(stdout)


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


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {
                "type": "result",
                "is_error": True,
                "result": None,
                "subtype": "error_max_turns",
                "session_id": "error-session",
                "usage": {"input_tokens": 9, "output_tokens": 2},
            },
            id="typed-null",
        ),
        pytest.param(
            {
                "type": "result",
                "is_error": True,
                "subtype": "error_max_turns",
                "session_id": "error-session",
                "usage": {"input_tokens": 9, "output_tokens": 2},
            },
            id="typed-omitted",
        ),
        pytest.param(
            {
                "is_error": True,
                "result": None,
                "subtype": "error_max_turns",
                "session_id": "error-session",
                "usage": {"input_tokens": 9, "output_tokens": 2},
            },
            id="untyped-null",
        ),
        pytest.param(
            {
                "is_error": True,
                "subtype": "error_max_turns",
                "session_id": "error-session",
                "usage": {"input_tokens": 9, "output_tokens": 2},
            },
            id="untyped-omitted",
        ),
    ],
)
def test_error_envelope_allows_empty_result_and_preserves_metadata(
    payload: dict[str, object],
) -> None:
    normalized = normalize_claude_cli_output(json.dumps(payload))

    assert normalized.result == ""
    assert normalized.is_error is True
    assert normalized.subtype == "error_max_turns"
    assert normalized.session_id == "error-session"
    assert normalized.usage == {
        "input_tokens": 9,
        "output_tokens": 2,
        "total_tokens": 11,
    }
    assert normalized.raw_payload["result"] == ""


@pytest.mark.parametrize("typed", [False, True], ids=["untyped", "typed"])
@pytest.mark.parametrize("include_null", [False, True], ids=["omitted", "null"])
def test_success_envelope_rejects_empty_result(typed: bool, include_null: bool) -> None:
    payload: dict[str, object] = {"is_error": False}
    if typed:
        payload["type"] = "result"
    if include_null:
        payload["result"] = None

    with pytest.raises(ClaudeCliOutputError, match="result"):
        normalize_claude_cli_output(json.dumps(payload))


def test_usage_falls_back_to_nested_assistant_usage() -> None:
    final = _result()
    final.pop("usage")

    normalized = normalize_claude_cli_output(json.dumps(_events(final)))

    assert normalized.usage == {
        "input_tokens": 3,
        "output_tokens": 1,
        "total_tokens": 4,
    }


def test_missing_terminal_usage_aggregates_every_assistant_turn() -> None:
    final = _result()
    final.pop("usage")
    events = _events(final)
    events.insert(
        -1,
        {
            "type": "assistant",
            "session_id": "session-1",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 7,
                "cached_input_tokens": 4,
            },
        },
    )

    normalized = normalize_claude_cli_output(json.dumps(events))

    assert normalized.usage == {
        "input_tokens": 103,
        "output_tokens": 21,
        "cache_read_input_tokens": 7,
        "cached_input_tokens": 4,
        "total_tokens": 131,
    }


def test_terminal_usage_is_authoritative_and_never_double_counted() -> None:
    events = _events(_result(usage={"input_tokens": 110, "output_tokens": 22}))
    events.insert(
        -1,
        {
            "type": "assistant",
            "session_id": "session-1",
            "message": {"usage": {"input_tokens": 100, "output_tokens": 20}},
        },
    )

    normalized = normalize_claude_cli_output(json.dumps(events))

    assert normalized.usage == {
        "input_tokens": 110,
        "output_tokens": 22,
        "total_tokens": 132,
    }


@pytest.mark.parametrize(
    ("raw_usage", "expected"),
    [
        pytest.param(
            {"total_tokens": 132},
            {"total_tokens": 132},
            id="total-only-stays-unallocated",
        ),
        pytest.param(
            {"input_tokens": 110, "output_tokens": 22},
            {"input_tokens": 110, "output_tokens": 22, "total_tokens": 132},
            id="components-derive-total",
        ),
        pytest.param(
            {"input_tokens": 110, "output_tokens": 22, "total_tokens": 132},
            {"input_tokens": 110, "output_tokens": 22, "total_tokens": 132},
            id="consistent-total-and-components",
        ),
        pytest.param(
            {"prompt_tokens": 110, "completion_tokens": 22},
            {"input_tokens": 110, "output_tokens": 22, "total_tokens": 132},
            id="primary-aliases-canonicalized",
        ),
        pytest.param(
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_creation_input_tokens": 8,
                "cache_read_input_tokens": 4,
                "cached_input_tokens": 90,
            },
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_creation_input_tokens": 8,
                "cache_read_input_tokens": 4,
                "cached_input_tokens": 90,
                "total_tokens": 132,
            },
            id="secondary-additive-and-diagnostic-counters",
        ),
    ],
)
def test_usage_is_one_canonical_accounting_authority(
    raw_usage: dict[str, int], expected: dict[str, int]
) -> None:
    normalized = normalize_claude_cli_output(json.dumps(_result(usage=raw_usage)))

    assert normalized.usage == expected
    assert normalized.raw_payload["usage"] == expected


@pytest.mark.parametrize(
    "raw_usage",
    [
        pytest.param(
            {"input_tokens": 110, "output_tokens": 22, "total_tokens": 12},
            id="inconsistent-total",
        ),
        pytest.param(
            {"input_tokens": 110, "total_tokens": 132},
            id="partial-components-with-total",
        ),
        pytest.param(
            {"input_tokens": 110, "prompt_tokens": 109, "output_tokens": 22},
            id="conflicting-alias",
        ),
        pytest.param(
            {"input_tokens": 110, "output_tokens": 22, "reasoning_tokens": 1},
            id="unsupported-token-counter",
        ),
        pytest.param({}, id="empty-usage"),
    ],
)
def test_invalid_terminal_usage_never_falls_back_to_assistant_totals(
    raw_usage: dict[str, int],
) -> None:
    events = _events(_result(usage=raw_usage))

    with pytest.raises(ClaudeCliOutputError, match="usage|token"):
        normalize_claude_cli_output(json.dumps(events))


def test_fallback_aggregates_reconciled_per_turn_totals_and_metadata() -> None:
    final = _result()
    final.pop("usage")
    events = [
        {
            "type": "assistant",
            "session_id": "session-1",
            "message": {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_creation_input_tokens": 3,
                    "total_tokens": 123,
                    "service_tier": "standard",
                }
            },
        },
        {
            "type": "assistant",
            "session_id": "session-1",
            "message": {
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 4,
                    "total_tokens": 16,
                    "service_tier": "standard",
                }
            },
        },
        final,
    ]

    normalized = normalize_claude_cli_output(json.dumps(events))

    assert normalized.usage == {
        "input_tokens": 110,
        "output_tokens": 22,
        "cache_creation_input_tokens": 3,
        "cache_read_input_tokens": 4,
        "total_tokens": 139,
        "service_tier": "standard",
    }


def test_fallback_synthesizes_only_total_when_a_turn_has_no_breakdown() -> None:
    final = _result()
    final.pop("usage")
    events = [
        {"type": "assistant", "usage": {"total_tokens": 120}},
        {
            "type": "assistant",
            "message": {"usage": {"input_tokens": 10, "output_tokens": 2}},
        },
        final,
    ]

    normalized = normalize_claude_cli_output(json.dumps(events))

    assert normalized.usage == {"total_tokens": 132}


@pytest.mark.parametrize(
    "events",
    [
        pytest.param(
            [
                {
                    "type": "assistant",
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                    "message": {"usage": {"input_tokens": 10, "output_tokens": 2}},
                }
            ],
            id="conflicting-sources",
        ),
        pytest.param(
            [
                {"type": "assistant", "usage": {"input_tokens": 100}},
                {
                    "type": "assistant",
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                },
            ],
            id="partial-turn",
        ),
        pytest.param(
            [
                {
                    "type": "assistant",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "total_tokens": 1,
                    },
                },
                {
                    "type": "assistant",
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                },
            ],
            id="inconsistent-total",
        ),
    ],
)
def test_rejects_ambiguous_or_incomplete_multi_turn_fallback_usage(
    events: list[dict[str, object]],
) -> None:
    final = _result()
    final.pop("usage")

    with pytest.raises(ClaudeCliOutputError, match="usage|token"):
        normalize_claude_cli_output(json.dumps([*events, final]))


def test_rejects_multi_turn_counter_sum_overflow() -> None:
    final = _result()
    final.pop("usage")
    events = [
        {
            "type": "assistant",
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "cached_input_tokens": (1 << 63) - 1,
            },
        },
        {
            "type": "assistant",
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "cached_input_tokens": 1,
            },
        },
        final,
    ]

    with pytest.raises(ClaudeCliOutputError, match="aggregated usage.cached_input_tokens"):
        normalize_claude_cli_output(json.dumps(events))


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


@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        pytest.param(
            '{"type":"result","is_error":false,"result":"done","trace":' + "9" * 4301 + "}",
            "integer exceeds",
            id="oversized-integer",
        ),
        pytest.param(
            '{"type":"result","is_error":false,"result":"done","score":1e9999}',
            "non-finite JSON number",
            id="raw-overflowing-float",
        ),
        pytest.param(
            '{"type":"result","is_error":false,"result":"done",'
            '"usage":{"input_tokens":1,"cache_read_input_tokens":1e9999}}',
            "non-finite JSON number",
            id="secondary-usage-overflowing-float",
        ),
    ],
)
def test_rejects_unbounded_or_non_finite_numbers(stdout: str, message: str) -> None:
    with pytest.raises(ClaudeCliOutputError, match=message):
        normalize_claude_cli_output(stdout)


@pytest.mark.parametrize(
    "token_key",
    [
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
    ],
)
def test_rejects_thousand_digit_token_counters_and_aliases(token_key: str) -> None:
    stdout = (
        '{"type":"result","is_error":false,"result":"done","usage":{"'
        + token_key
        + '":'
        + "9" * 1000
        + "}}"
    )

    with pytest.raises(
        ClaudeCliOutputError,
        match=rf"usage\.{token_key} must be a bounded non-negative integer",
    ):
        normalize_claude_cli_output(stdout)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="null"),
        pytest.param(True, id="boolean"),
        pytest.param(-1, id="negative"),
        pytest.param(1.5, id="fractional"),
    ],
)
def test_rejects_invalid_secondary_token_counter_types(value: object) -> None:
    payload = _result(usage={"input_tokens": 1, "cache_read_input_tokens": value})

    with pytest.raises(
        ClaudeCliOutputError,
        match="usage.cache_read_input_tokens must be a bounded non-negative integer",
    ):
        normalize_claude_cli_output(json.dumps(payload))


@pytest.mark.parametrize(
    ("alias", "value"),
    [
        pytest.param("prompt_tokens", True, id="boolean-prompt-alias"),
        pytest.param("completion_tokens", 1.5, id="fractional-completion-alias"),
        pytest.param("prompt_tokens", -1, id="negative-prompt-alias"),
    ],
)
def test_rejects_malformed_primary_aliases(alias: str, value: object) -> None:
    usage: dict[str, object] = {"prompt_tokens": 1, "completion_tokens": 2}
    usage[alias] = value
    payload = _result(usage=usage)

    with pytest.raises(
        ClaudeCliOutputError,
        match=rf"usage\.{alias} must be a bounded non-negative integer",
    ):
        normalize_claude_cli_output(json.dumps(payload))


def test_rejects_oversized_stdout_before_decoding() -> None:
    with pytest.raises(ClaudeCliOutputError, match="character safety limit"):
        normalize_claude_cli_output("x" * (MAX_CLAUDE_CLI_OUTPUT_CHARS + 1))


def test_rejects_excessive_event_count() -> None:
    events = [{"type": "assistant"}] * MAX_CLAUDE_CLI_EVENTS + [_result()]

    with pytest.raises(ClaudeCliOutputError, match="event count"):
        normalize_claude_cli_output(json.dumps(events))
