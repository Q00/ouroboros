"""Measured ACP updates map to existing runtime messages, not SDK event names."""

from __future__ import annotations

import pytest

from ouroboros.copilot.acp_events import CopilotAcpEventTranslator
from ouroboros.orchestrator.runtime_message_projection import (
    project_runtime_message,
    should_emit_runtime_progress,
)
from ouroboros.providers.ourocode_acp_client import AcpClientError


def update(update_type: str, **fields: object) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": "session-1", "update": {"sessionUpdate": update_type, **fields}},
    }


def test_assistant_deltas_preserve_whitespace_and_form_final_answer() -> None:
    translator = CopilotAcpEventTranslator("session-1")
    deltas = ["Hello", " ", "world", "!\n"]
    messages = [
        translator.translate(update("agent_message_chunk", content={"type": "text", "text": t}))[0]
        for t in deltas
    ]
    assert [message.content for message in messages] == deltas
    assert [
        project_runtime_message(m).runtime_metadata["content_delta"] for m in messages
    ] == deltas
    assert all(should_emit_runtime_progress(m, 1) for m in messages)
    result = translator.finish({"stopReason": "end_turn"}, None)
    assert result.is_final and not result.is_error
    assert result.content == "Hello world!\n"


def test_tool_start_progress_and_completion_do_not_duplicate_calls() -> None:
    translator = CopilotAcpEventTranslator("session-1")
    start = translator.translate(
        update(
            "tool_call",
            toolCallId="t1",
            kind="execute",
            title="Running tests",
            status="pending",
            rawInput={"command": "python3 -m unittest"},
        )
    )[0]
    progress = translator.translate(
        update(
            "tool_call_update",
            toolCallId="t1",
            status="in_progress",
            content=[{"type": "content", "content": {"type": "text", "text": "test 1: ok"}}],
        )
    )[0]
    complete = translator.translate(
        update(
            "tool_call_update",
            toolCallId="t1",
            status="completed",
            rawOutput={"content": "2 passed", "exitCode": 0},
        )
    )[0]
    assert project_runtime_message(start).is_tool_call
    assert start.tool_name == "Bash"
    assert start.data["tool_input"] == {"command": "python3 -m unittest"}
    projected = project_runtime_message(progress)
    assert not projected.is_tool_call and not projected.is_tool_result
    assert projected.runtime_signal == "tool_progress"
    assert projected.runtime_metadata["tool_output"] == "test 1: ok"
    assert project_runtime_message(complete).is_tool_result
    assert complete.content == "2 passed"
    assert complete.data["exit_code"] == 0
    assert {m.data["tool_call_id"] for m in (start, progress, complete)} == {"t1"}
    assert not translator.translate(update("tool_call_update", toolCallId="t1", status="completed"))


def test_interleaved_tools_keep_inputs_and_parent_hierarchy() -> None:
    translator = CopilotAcpEventTranslator("session-1")
    for tool_id in ("one", "two"):
        translator.translate(
            update(
                "tool_call",
                toolCallId=tool_id,
                kind="read",
                rawInput={"path": f"{tool_id}.py"},
                _meta={"agentId": f"agent-{tool_id}", "parentToolCallId": "task-parent"},
            )
        )
    for tool_id in ("two", "one"):
        message = translator.translate(
            update("tool_call_update", toolCallId=tool_id, status="completed")
        )[0]
        metadata = project_runtime_message(message).runtime_metadata
        assert metadata["tool_input"] == {"path": f"{tool_id}.py"}
        assert metadata["agent_id"] == f"agent-{tool_id}"
        assert metadata["parent_tool_call_id"] == "task-parent"


def test_completion_before_start_synthesizes_one_correlated_start() -> None:
    translator = CopilotAcpEventTranslator("session-1")
    messages = translator.translate(
        update("tool_call_update", toolCallId="late", kind="read", status="completed")
    )
    assert len(messages) == 2
    assert project_runtime_message(messages[0]).is_tool_call
    assert project_runtime_message(messages[1]).is_tool_result
    assert not translator.translate(update("tool_call", toolCallId="late", status="pending"))


@pytest.mark.parametrize("status,exit_code", [("failed", None), ("completed", 1)])
def test_tool_failures_are_not_successful_commands(status: str, exit_code: int | None) -> None:
    translator = CopilotAcpEventTranslator("session-1")
    message = translator.translate(
        update(
            "tool_call",
            toolCallId="bad",
            kind="execute",
            status=status,
            rawOutput={"message": "Tests failed", "exitCode": exit_code},
        )
    )[-1]
    assert message.data["is_error"] is True
    assert message.data["tool_result"]["isError"] is True


def test_final_answer_excludes_tool_output_progress_and_private_reasoning() -> None:
    translator = CopilotAcpEventTranslator("session-1")
    translator.translate(update("agent_message_chunk", content={"type": "text", "text": "Working"}))
    translator.translate(update("tool_call", toolCallId="x", kind="read", status="pending"))
    translator.translate(
        update(
            "tool_call_update", toolCallId="x", status="completed", rawOutput={"content": "tool"}
        )
    )
    assert not translator.translate(
        update("agent_thought_chunk", content={"type": "text", "text": "private"})
    )
    translator.translate(update("agent_message_chunk", content={"type": "text", "text": "Done"}))
    assert translator.finish({"stopReason": "end_turn"}, None).content == "Done"


@pytest.mark.parametrize("stop_reason", ["cancelled", "max_tokens", "refusal", "unknown"])
def test_non_end_turn_results_are_errors(stop_reason: str) -> None:
    message = CopilotAcpEventTranslator("session-1").finish({"stopReason": stop_reason}, None)
    assert message.is_final and message.is_error
    assert message.data["stop_reason"] == stop_reason


def test_billed_usage_is_taken_from_prompt_result_not_context_capacity() -> None:
    translator = CopilotAcpEventTranslator("session-1")
    assert not translator.translate(update("usage_update", used=4096, size=200000))
    result = translator.finish(
        {
            "stopReason": "end_turn",
            "usage": {"inputTokens": 20, "outputTokens": 5, "cachedReadTokens": 10},
        },
        None,
    )
    assert result.data["usage"] == {
        "input_tokens": 20,
        "output_tokens": 5,
        "cache_read_input_tokens": 10,
    }


@pytest.mark.parametrize("bad", [-1, True, "12", float("nan"), float("inf"), 10**1000])
def test_malformed_usage_is_rejected_as_a_whole(bad: object) -> None:
    result = CopilotAcpEventTranslator("session-1").finish(
        {"stopReason": "end_turn", "usage": {"inputTokens": 1, "outputTokens": bad}}, None
    )
    assert result.data["usage_invalid"] is True
    assert "usage" not in result.data


def test_foreign_sessions_and_unknown_extensions_are_ignored() -> None:
    translator = CopilotAcpEventTranslator("session-1")
    frame = update("agent_message_chunk", content={"type": "text", "text": "other"})
    frame["params"]["sessionId"] = "session-2"
    assert not translator.translate(frame)
    assert not translator.translate(update("future_preview_extension", arbitrary="data"))


@pytest.mark.parametrize("kind", ["tool_call", "tool_call_update"])
def test_missing_tool_id_is_a_protocol_failure(kind: str) -> None:
    with pytest.raises(AcpClientError, match="toolCallId"):
        CopilotAcpEventTranslator("session-1").translate(update(kind, status="completed"))


def test_plan_is_status_not_private_reasoning() -> None:
    message = CopilotAcpEventTranslator("session-1").translate(
        update("plan", entries=[{"content": "Run targeted tests", "status": "in_progress"}])
    )[0]
    assert message.type == "system"
    assert message.content == "in_progress: Run targeted tests"
    assert "thinking" not in message.data


def test_measured_grep_input_is_not_mislabeled_as_glob() -> None:
    message = CopilotAcpEventTranslator("session-1").translate(
        update(
            "tool_call",
            toolCallId="grep",
            kind="read",
            rawInput={"pattern": "TODO", "output_mode": "content", "-n": True},
        )
    )[0]
    assert message.tool_name == "Grep"


@pytest.mark.parametrize("code", [0, 1, 137, -15])
def test_measured_shell_footer_preserves_real_command_exit_status(code: int) -> None:
    message = CopilotAcpEventTranslator("session-1").translate(
        update(
            "tool_call",
            toolCallId="shell",
            kind="execute",
            status="completed",
            rawOutput={"content": f"test output\n<shellId: 0 completed with exit code {code}>"},
        )
    )[-1]
    assert message.data["exit_code"] == code
    assert message.data["is_error"] is (code != 0)


def test_conflicting_exit_codes_keep_failure_instead_of_zero() -> None:
    message = CopilotAcpEventTranslator("session-1").translate(
        update(
            "tool_call",
            toolCallId="shell",
            kind="execute",
            status="completed",
            rawOutput={"exitCode": 1, "returncode": 0},
        )
    )[-1]
    assert message.data["exit_code"] == 1
    assert message.data["is_error"] is True


def test_footer_is_only_interpreted_for_shell_and_at_the_end() -> None:
    translator = CopilotAcpEventTranslator("session-1")
    for tool_id, kind, suffix in [("file", "read", ""), ("shell", "execute", "\nmore output")]:
        message = translator.translate(
            update(
                "tool_call",
                toolCallId=tool_id,
                kind=kind,
                status="completed",
                rawOutput={"content": f"<shellId: 0 completed with exit code 1>{suffix}"},
            )
        )[-1]
        assert "exit_code" not in message.data


def test_large_text_chunks_are_split_without_losing_replay_content() -> None:
    translator = CopilotAcpEventTranslator("session-1")
    text = "x" * 100000 + "\n"
    messages = translator.translate(
        update("agent_message_chunk", content={"type": "text", "text": text})
    )
    assert len(messages) == 2
    assert "".join(m.data["content_delta"] for m in messages) == text
    assert all(should_emit_runtime_progress(m, 1) for m in messages)
    assert translator.finish({"stopReason": "end_turn"}, None).content == text


@pytest.mark.parametrize("code", [0, 1])
def test_measured_structured_shell_exit_takes_precedence_over_output_text(code: int) -> None:
    message = CopilotAcpEventTranslator("session-1").translate(
        update(
            "tool_call",
            toolCallId="shell",
            kind="execute",
            status="completed",
            rawOutput={
                "content": "<shellId: 0 completed with exit code 99>",
                "contents": [{"type": "shell_exit", "shellId": "0", "exitCode": code}],
            },
        )
    )[-1]
    assert message.data["exit_code"] == code
    assert message.data["is_error"] is (code != 0)


def test_measured_native_task_is_named_without_guessing_missing_hierarchy() -> None:
    message = CopilotAcpEventTranslator("session-1").translate(
        update(
            "tool_call",
            toolCallId="delegate",
            kind="other",
            rawInput={"agent_type": "explore", "mode": "sync", "prompt": "Read README.md"},
        )
    )[0]
    assert message.tool_name == "Task"
    assert "parent_tool_call_id" not in message.data
    assert "agent_id" not in message.data


def test_measured_no_tools_notices_are_status_not_final_answer() -> None:
    translator = CopilotAcpEventTranslator("session-1", empty_tool_marker="ouroboros_no_tools_abc")
    for text in (
        "Info: Disabled tools: bash, view",
        'Info: Unknown tool name in the tool allowlist: "ouroboros_no_tools_abc"',
    ):
        messages = translator.translate(
            update("agent_message_chunk", content={"type": "text", "text": text})
        )
        assert messages[0].type == "system"
        assert "content_delta" not in messages[0].data
    translator.translate(
        update("agent_message_chunk", content={"type": "text", "text": "The answer."})
    )
    assert translator.finish({"stopReason": "end_turn"}, None).content == "The answer."


def test_new_envelope_correlation_wins_without_losing_tool_parent() -> None:
    translator = CopilotAcpEventTranslator("session-1")
    translator.translate(
        update(
            "tool_call",
            toolCallId="one",
            kind="read",
            id="start-event",
            timestamp="start-time",
            parentToolCallId="parent",
        )
    )
    frame = update("tool_call_update", toolCallId="one", status="completed")
    frame["_meta"] = {"id": "completion-event", "timestamp": "completion-time"}
    message = translator.translate(frame)[0]
    assert message.data["source_event_id"] == "completion-event"
    assert message.data["source_timestamp"] == "completion-time"
    assert message.data["parent_tool_call_id"] == "parent"
