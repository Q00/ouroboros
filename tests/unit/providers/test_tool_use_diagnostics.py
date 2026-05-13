from __future__ import annotations

from dataclasses import dataclass

from ouroboros.providers.tool_use_diagnostics import (
    count_tool_use_blocks,
    diagnose_tool_use_turn,
)


@dataclass
class Block:
    type: str
    id: str = "tool-1"


@dataclass
class Message:
    stop_reason: str
    content: list[object]


class ToolUseBlock:
    name = "Read"
    input = {"file_path": "README.md"}


class TextBlock:
    text = "thinking text"


def test_detects_stop_reason_tool_use_without_tool_blocks() -> None:
    diagnostic = diagnose_tool_use_turn(
        {"stop_reason": "tool_use", "content": [{"type": "text", "text": "continue"}]},
        provider="claude-code",
    )

    assert diagnostic.is_malformed is True
    assert diagnostic.retryable is True
    assert diagnostic.tool_use_count == 0
    assert diagnostic.to_dict()["reason"].startswith("stop_reason=tool_use")


def test_valid_tool_use_turn_is_not_malformed() -> None:
    diagnostic = diagnose_tool_use_turn(
        {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "abc"}]},
        provider="claude-code",
    )

    assert diagnostic.is_malformed is False
    assert diagnostic.retryable is False
    assert diagnostic.tool_use_count == 1


def test_object_style_provider_messages_are_supported() -> None:
    message = Message(stop_reason="tool_use", content=[Block(type="tool_use")])

    assert count_tool_use_blocks(message) == 1
    assert diagnose_tool_use_turn(message, provider="sdk-object").is_malformed is False


def test_sdk_tool_use_block_class_without_type_is_supported() -> None:
    message = Message(stop_reason="tool_use", content=[ToolUseBlock()])

    diagnostic = diagnose_tool_use_turn(message, provider="claude-code-sdk")

    assert count_tool_use_blocks(message) == 1
    assert diagnostic.is_malformed is False
    assert diagnostic.retryable is False
    assert diagnostic.tool_use_count == 1


def test_sdk_text_block_without_type_is_not_counted_as_tool_use() -> None:
    message = Message(stop_reason="tool_use", content=[TextBlock()])

    diagnostic = diagnose_tool_use_turn(message, provider="claude-code-sdk")

    assert count_tool_use_blocks(message) == 0
    assert diagnostic.is_malformed is True
    assert diagnostic.retryable is True


def test_non_tool_use_stop_reason_is_consistent_even_without_content() -> None:
    diagnostic = diagnose_tool_use_turn(
        {"stop_reason": "end_turn", "content": []},
        provider="claude-code",
    )

    assert diagnostic.is_malformed is False
    assert diagnostic.retryable is False
    assert diagnostic.stop_reason == "end_turn"
