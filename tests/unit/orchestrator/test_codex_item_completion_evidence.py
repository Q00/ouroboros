"""Regression tests for Codex CLI item completion evidence (issues #1690 / #1724).

Issue #1690: Codex CLI JSONL ``item.started`` / ``item.completed`` events are not
projected as a correlated tool start and tool result pair.

Issue #1724: Codex tool completions are never journaled as
``execution.tool.completed`` evidence, so the fat-harness verifier sees only
tool starts and rejects truthful claims as FABRICATION_SUSPECTED.

Payload shapes follow the Codex CLI JSONL thread-item contract used by the
existing conversion tests, extended with the item ``id`` field described in the
issue #1690 reproduction steps.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.harness.deliver_gate import _event_has_explicit_tool_success
from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor
from ouroboros.orchestrator.runtime_message_projection import project_runtime_message


def _as_journaled_tool_completed_event(message: AgentMessage) -> BaseEvent:
    """Mirror how the leaf dispatcher journals a tool result message."""
    projected = project_runtime_message(message)
    return BaseEvent(
        type="execution.tool.completed",
        aggregate_type="execution",
        aggregate_id="exec_evidence",
        data={
            "tool_name": projected.tool_name,
            "tool_result_text": projected.content,
            **projected.runtime_metadata,
        },
    )


_COMMAND_ITEM_STARTED = {
    "type": "item.started",
    "item": {
        "id": "item_0",
        "type": "command_execution",
        "command": "pytest -q",
        "status": "in_progress",
    },
}

_COMMAND_ITEM_COMPLETED = {
    "type": "item.completed",
    "item": {
        "id": "item_0",
        "type": "command_execution",
        "command": "pytest -q",
        "aggregated_output": ". [100%]\n1 passed in 0.01s\n",
        "exit_code": 0,
        "status": "completed",
    },
}


class TestCodexItemLifecycleConversion:
    """Issue #1690 — item.started/item.completed must form a correlated pair."""

    def test_item_started_command_execution_produces_correlated_tool_start(self) -> None:
        """item.started(command_execution) must project a tool start, not vanish."""
        runtime = CodexCliRuntime(cli_path="codex")

        messages = runtime._convert_event(_COMMAND_ITEM_STARTED, current_handle=None)

        assert len(messages) == 1
        projected = project_runtime_message(messages[0])
        assert projected.is_tool_call
        assert projected.tool_name == "Bash"
        assert projected.runtime_metadata.get("tool_call_id") == "item_0"

    def test_item_completed_after_started_projects_single_correlated_tool_result(self) -> None:
        """item.completed after item.started must project a tool result, not a second start."""
        runtime = CodexCliRuntime(cli_path="codex")
        runtime._convert_event(_COMMAND_ITEM_STARTED, current_handle=None)

        messages = runtime._convert_event(_COMMAND_ITEM_COMPLETED, current_handle=None)

        assert len(messages) == 1
        projected = project_runtime_message(messages[0])
        assert projected.message_type == "tool_result"
        assert projected.tool_name == "Bash"
        assert projected.runtime_metadata.get("tool_call_id") == "item_0"

    def test_item_completed_without_started_synthesizes_correlated_start_result_pair(
        self,
    ) -> None:
        """Completed-only legacy streams must yield a start+result pair (#1692 blocker 2)."""
        runtime = CodexCliRuntime(cli_path="codex")

        messages = runtime._convert_event(_COMMAND_ITEM_COMPLETED, current_handle=None)

        assert len(messages) == 2
        start, result = (project_runtime_message(message) for message in messages)
        assert start.is_tool_call
        assert start.tool_name == "Bash"
        assert start.runtime_metadata.get("tool_call_id") == "item_0"
        assert result.message_type == "tool_result"
        assert result.tool_name == "Bash"
        assert result.runtime_metadata.get("tool_call_id") == "item_0"

    def test_failed_file_change_completion_is_not_stamped_success(self) -> None:
        """A failed file_change must not be converted into success evidence."""
        runtime = CodexCliRuntime(cli_path="codex")

        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_2",
                    "type": "file_change",
                    "status": "failed",
                    "changes": [{"path": "src/app.py", "kind": "update"}],
                },
            },
            current_handle=None,
        )

        assert messages
        assert all(message.data.get("subtype") != "success" for message in messages)
        result_messages = [
            message for message in messages if message.data.get("subtype") == "tool_result"
        ]
        assert result_messages, "failed file_change produced no tool result message"
        assert all(message.data.get("is_error") is True for message in result_messages)
        assert all(
            not _event_has_explicit_tool_success(_as_journaled_tool_completed_event(message))
            for message in result_messages
        )

    def test_multi_file_change_completion_emits_per_path_correlation_ids(self) -> None:
        """Each changed path must carry its own "{item_id}:{path}" correlation id."""
        runtime = CodexCliRuntime(cli_path="codex")
        runtime._convert_event(
            {
                "type": "item.started",
                "item": {
                    "id": "item_9",
                    "type": "file_change",
                    "status": "in_progress",
                    "changes": [
                        {"path": "src/app.py", "kind": "update"},
                        {"path": "tests/test_app.py", "kind": "add"},
                    ],
                },
            },
            current_handle=None,
        )

        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_9",
                    "type": "file_change",
                    "status": "completed",
                    "changes": [
                        {"path": "src/app.py", "kind": "update"},
                        {"path": "tests/test_app.py", "kind": "add"},
                    ],
                },
            },
            current_handle=None,
        )

        projections = [project_runtime_message(message) for message in messages]
        assert [projection.message_type for projection in projections] == [
            "tool_result",
            "tool_result",
        ]
        assert [projection.runtime_metadata.get("tool_call_id") for projection in projections] == [
            "item_9:src/app.py",
            "item_9:tests/test_app.py",
        ]
        assert all(
            projection.runtime_metadata.get("is_error") is False for projection in projections
        )

    def test_nonzero_exit_completion_is_error_and_never_success_evidence(self) -> None:
        """A non-zero exit code must mark the result as an error, never success."""
        runtime = CodexCliRuntime(cli_path="codex")
        runtime._convert_event(_COMMAND_ITEM_STARTED, current_handle=None)

        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_0",
                    "type": "command_execution",
                    "command": "pytest -q",
                    "aggregated_output": "1 failed in 0.01s\n",
                    "exit_code": 1,
                    "status": "completed",
                },
            },
            current_handle=None,
        )

        assert len(messages) == 1
        result = messages[0]
        assert result.data.get("subtype") == "tool_result"
        assert result.data.get("is_error") is True
        assert not _event_has_explicit_tool_success(_as_journaled_tool_completed_event(result))

    def test_malformed_completion_metadata_fails_closed_without_success_claim(self) -> None:
        """Unknown/malformed completion metadata must never become success evidence."""
        runtime = CodexCliRuntime(cli_path="codex")
        runtime._convert_event(_COMMAND_ITEM_STARTED, current_handle=None)

        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_0",
                    "type": "command_execution",
                    "command": "pytest -q",
                    "exit_code": "0",  # malformed: string, not int
                    "status": 7,  # malformed: not a status string
                },
            },
            current_handle=None,
        )

        assert len(messages) == 1
        result = messages[0]
        assert result.data.get("subtype") == "tool_result"
        assert "is_error" not in result.data
        assert not _event_has_explicit_tool_success(_as_journaled_tool_completed_event(result))

    def test_exit_zero_completion_is_explicit_success_evidence(self) -> None:
        """An explicit exit-0 completion must satisfy the deliver-gate success check."""
        runtime = CodexCliRuntime(cli_path="codex")
        runtime._convert_event(_COMMAND_ITEM_STARTED, current_handle=None)

        messages = runtime._convert_event(_COMMAND_ITEM_COMPLETED, current_handle=None)

        assert len(messages) == 1
        result = messages[0]
        assert result.data.get("is_error") is False
        assert _event_has_explicit_tool_success(_as_journaled_tool_completed_event(result))


class TestCodexCompletionReviewRoundOne:
    """Regressions for the first bot review on the contract PR."""

    def test_nested_failure_wins_over_outer_completed_status(self) -> None:
        """Failure precedence: a nested failed status must never become success."""
        runtime = CodexCliRuntime(cli_path="codex")
        event = {
            "type": "item.completed",
            "item": {
                "id": "item_5",
                "type": "command_execution",
                "command": "pytest -q",
                "status": "completed",
                "result": {"status": "failed", "output": "1 failed"},
            },
        }

        messages = runtime._convert_event(event, current_handle=None)

        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        assert len(results) == 1
        assert results[0].data.get("is_error") is True, (
            "outer status=completed must not shadow the nested result.status=failed"
        )
        assert (
            _event_has_explicit_tool_success(_as_journaled_tool_completed_event(results[0]))
            is False
        )

    def test_cancelled_item_with_stale_nested_completed_is_never_success(self) -> None:
        """Cancellation must win over a stale nested completed status."""
        runtime = CodexCliRuntime(cli_path="codex")
        event = {
            "type": "item.completed",
            "item": {
                "id": "item_6",
                "type": "command_execution",
                "command": "pytest -q",
                "status": "cancelled",
                "result": {"status": "completed", "output": "partial"},
            },
        }

        messages = runtime._convert_event(event, current_handle=None)

        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        assert len(results) == 1
        assert results[0].data.get("is_error") is True, (
            "a cancelled item must never be laundered into success by stale nested metadata"
        )
        assert (
            _event_has_explicit_tool_success(_as_journaled_tool_completed_event(results[0]))
            is False
        )

    def test_returncode_alias_nonzero_is_error(self) -> None:
        """Every accepted exit-code alias must feed the failure verdict."""
        runtime = CodexCliRuntime(cli_path="codex")
        for alias in ("exit_code", "exitCode", "returncode", "return_code"):
            event = {
                "type": "item.completed",
                "item": {
                    "id": f"item_{alias}",
                    "type": "command_execution",
                    "command": "pytest -q",
                    "status": "completed",
                    alias: 1,
                },
            }

            messages = runtime._convert_event(event, current_handle=None)

            results = [m for m in messages if m.data.get("subtype") == "tool_result"]
            assert len(results) == 1, alias
            assert results[0].data.get("is_error") is True, (
                f"non-zero {alias} must override the completed status"
            )

    def test_web_search_start_records_exactly_the_query(self) -> None:
        """web_search tool input must carry item['query'], not the whole item."""
        runtime = CodexCliRuntime(cli_path="codex")
        event = {
            "type": "item.started",
            "item": {
                "id": "item_ws",
                "type": "web_search",
                "query": "ouroboros seed contract",
                "status": "in_progress",
            },
        }

        messages = runtime._convert_event(event, current_handle=None)

        assert len(messages) == 1
        tool_input = messages[0].data.get("tool_input") or {}
        assert tool_input.get("query") == "ouroboros seed contract"

    def test_idless_web_search_lifecycle_pairs_without_duplicate_start(self) -> None:
        """Status changes must not alter the id-less correlation signature."""
        runtime = CodexCliRuntime(cli_path="codex")
        started = {
            "type": "item.started",
            "item": {
                "type": "web_search",
                "query": "ouroboros seed contract",
                "status": "in_progress",
            },
        }
        completed = {
            "type": "item.completed",
            "item": {
                "type": "web_search",
                "query": "ouroboros seed contract",
                "status": "completed",
            },
        }

        start_messages = runtime._convert_event(started, current_handle=None)
        completion_messages = runtime._convert_event(completed, current_handle=None)

        assert len(start_messages) == 1
        assert len(completion_messages) == 1, (
            "an id-less completion whose signature drifted with status "
            "synthesized a duplicate start"
        )
        assert completion_messages[0].data.get("subtype") == "tool_result"

    def test_new_thread_resets_item_correlation_state(self) -> None:
        """A new thread's completed-only item must synthesize its own start."""
        runtime = CodexCliRuntime(cli_path="codex")
        thread_started = {"type": "thread.started", "thread_id": "t1"}

        # Stream A: full lifecycle for item_0.
        runtime._convert_event(thread_started, current_handle=None)
        runtime._convert_event(_COMMAND_ITEM_STARTED, current_handle=None)
        runtime._convert_event(_COMMAND_ITEM_COMPLETED, current_handle=None)

        # Stream B on the same adapter: completed-only reuse of the same item id.
        runtime._convert_event({"type": "thread.started", "thread_id": "t2"}, current_handle=None)
        messages = runtime._convert_event(_COMMAND_ITEM_COMPLETED, current_handle=None)

        assert len(messages) == 2, (
            "stale correlation state from the previous thread suppressed the "
            "synthetic start for the new thread's completed-only item"
        )
        assert messages[0].data.get("tool_call_id") == "item_0"
        assert messages[1].data.get("subtype") == "tool_result"


class TestCodexCompletionJournaling:
    """Issue #1724 — completions must reach the execution.tool.completed journal."""

    @pytest.mark.asyncio
    async def test_codex_command_completion_is_journaled_as_execution_tool_completed(
        self,
    ) -> None:
        """A successful Codex command lifecycle must persist completed-tool evidence."""
        conversion_runtime = CodexCliRuntime(cli_path="codex")
        codex_events: list[dict[str, Any]] = [
            {"type": "thread.started", "thread_id": "thread-evidence-1"},
            _COMMAND_ITEM_STARTED,
            _COMMAND_ITEM_COMPLETED,
        ]

        class StubCodexRuntime:
            _runtime_handle_backend = "codex_cli"
            _cwd = "/tmp/project"
            _permission_mode = "bypassPermissions"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(self, **kwargs: Any):
                handle = None
                for event in codex_events:
                    for message in conversion_runtime._convert_event(event, current_handle=handle):
                        if message.resume_handle is not None:
                            handle = message.resume_handle
                        yield message
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                )

        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=StubCodexRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Run the test suite via the Codex CLI runtime",
            session_id="sess_codex_evidence",
            tools=["Bash"],
            system_prompt="test",
            seed_goal="Journal Codex completions",
            depth=0,
            start_time=datetime.now(UTC),
        )

        appended_events = [call.args[0] for call in event_store.append.await_args_list]
        started_events = [
            event for event in appended_events if event.type == "execution.tool.started"
        ]
        completed_events = [
            event for event in appended_events if event.type == "execution.tool.completed"
        ]

        assert started_events, "Codex command lifecycle produced no tool-start journal event"
        assert completed_events, (
            "Codex item.completed(command_execution) with exit_code 0 produced no "
            "execution.tool.completed journal event — the deliver gate cannot "
            "prove the command succeeded (issue #1724)"
        )
        assert len(started_events) == 1, "the synthesized pair must not duplicate the start"
        assert started_events[0].data.get("tool_call_id") == "item_0"
        assert completed_events[0].data.get("tool_call_id") == "item_0"
        assert _event_has_explicit_tool_success(completed_events[0])
