"""Readings of a goal: structural checks on the one readings call."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.examples.readings import (
    MAX_READING_CHARS,
    parse_readings,
    reading_id_for,
    state_readings,
)
from ouroboros.providers.base import CompletionConfig, CompletionResponse, Message, UsageInfo

GOAL = "parse_duration should accept compound values like 1h30 and return seconds."


def reply(*items: Any) -> str:
    return json.dumps({"readings": list(items)})


def test_a_reading_whose_span_is_in_the_goal_is_kept() -> None:
    result = parse_readings(
        reply({"span": "compound values like 1h30", "reading": "1h30 is 90 minutes"}), GOAL, 4
    )
    assert [(r.span, r.text) for r in result.readings] == [
        ("compound values like 1h30", "1h30 is 90 minutes")
    ]
    assert result.readings[0].reading_id == reading_id_for("1h30 is 90 minutes")
    assert result.dropped == 0 and result.call_failed is None


def test_span_containment_collapses_whitespace_like_check_span() -> None:
    result = parse_readings(
        reply({"span": "compound   values\nlike 1h30", "reading": "x"}), GOAL, 4
    )
    assert len(result.readings) == 1


@pytest.mark.parametrize(
    "item",
    [
        {"span": "not in the goal at all", "reading": "x"},
        {"span": "1h30", "reading": "too short a span"},
        {"span": "compound values", "reading": "   "},
        {"span": "compound values", "reading": "x" * (MAX_READING_CHARS + 1)},
        {"span": 3, "reading": "x"},
        {"span": "compound values"},
        "compound values",
    ],
)
def test_a_reading_that_fails_the_check_is_dropped(item: Any) -> None:
    result = parse_readings(reply(item), GOAL, 4)
    assert result.readings == () and result.dropped == 1


def test_identical_text_is_merged_and_the_cap_holds() -> None:
    result = parse_readings(
        reply(
            {"span": "compound values", "reading": "same"},
            {"span": "return seconds", "reading": " same "},
            {"span": "return seconds", "reading": "second"},
            {"span": "return seconds", "reading": "third"},
        ),
        GOAL,
        2,
    )
    assert [r.text for r in result.readings] == ["same", "second"]
    assert result.dropped == 2


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("not json", "reply_not_json"),
        ("[]", "reply_without_readings"),
        ('{"x": 1}', "reply_without_readings"),
    ],
)
def test_a_reply_without_readings_keeps_none(text: str, reason: str) -> None:
    result = parse_readings(text, GOAL, 4)
    assert result.readings == () and result.call_failed == reason


class _Llm:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[list[Message]] = []

    async def complete(self, messages: list[Message], config: CompletionConfig) -> Any:
        self.calls.append(messages)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


async def test_state_readings_makes_one_call_with_the_goal_and_facts() -> None:
    content = reply({"span": "compound values", "reading": "r"})
    llm = _Llm(Result.ok(CompletionResponse(content, "m", UsageInfo(1, 1, 2))))
    result = await state_readings(
        llm, CompletionConfig(model="m"), goal=GOAL, repo_facts=["fact one"], max_readings=4
    )
    assert len(llm.calls) == 1
    user = llm.calls[0][-1].content
    assert GOAL in user and "fact one" in user
    assert [r.text for r in result.readings] == ["r"]


async def test_a_failed_or_raising_call_keeps_no_reading() -> None:
    failed = await state_readings(
        _Llm(Result.err(ProviderError("down"))),
        CompletionConfig(model="m"),
        goal=GOAL,
        repo_facts=[],
        max_readings=4,
    )
    assert failed.readings == () and failed.call_failed == "call_failed:ProviderError"
    raised = await state_readings(
        _Llm(RuntimeError("boom")),
        CompletionConfig(model="m"),
        goal=GOAL,
        repo_facts=[],
        max_readings=4,
    )
    assert raised.readings == () and raised.call_failed == "call_raised:RuntimeError"
