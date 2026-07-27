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
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.harness.deliver_gate import _event_has_explicit_tool_success
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.orchestrator.evidence.claims import _runtime_message_has_success_signal
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

    def test_keyed_web_search_lifecycle_pairs_without_duplicate_start(self) -> None:
        """A keyed web_search start/completed pair correlates by id, no duplicate."""
        runtime = CodexCliRuntime(cli_path="codex")
        start_messages = runtime._convert_event(
            {
                "type": "item.started",
                "item": {
                    "id": "w1",
                    "type": "web_search",
                    "query": "ouroboros seed contract",
                    "status": "in_progress",
                },
            },
            current_handle=None,
        )
        completion_messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "w1",
                    "type": "web_search",
                    "query": "ouroboros seed contract",
                    "status": "completed",
                },
            },
            current_handle=None,
        )

        assert len(start_messages) == 1
        assert start_messages[0].data.get("tool_call_id") == "w1"
        assert (start_messages[0].data.get("tool_input") or {}).get("query") == (
            "ouroboros seed contract"
        )
        assert len(completion_messages) == 1
        assert completion_messages[0].data.get("subtype") == "tool_result"
        assert completion_messages[0].data.get("tool_call_id") == "w1"

    @pytest.mark.parametrize(
        "status",
        [
            "canceled",
            "aborted",
            "interrupted",
            "killed",
            "timeout",
            "timed_out",
            "Cancelled ",
            "TIMED_OUT",
        ],
    )
    def test_every_non_success_terminal_status_is_error(self, status: str) -> None:
        """All non-success terminal statuses resolve to failure, however cased."""
        runtime = CodexCliRuntime(cli_path="codex")
        event = {
            "type": "item.completed",
            "item": {
                "id": "item_ts",
                "type": "command_execution",
                "command": "pytest -q",
                "status": status,
            },
        }

        messages = runtime._convert_event(event, current_handle=None)

        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        assert len(results) == 1
        assert results[0].data.get("is_error") is True, status

    def test_nested_and_conflicting_exit_aliases_resolve_consistently(self) -> None:
        """Nested alias placement and alias conflicts keep verdict and metadata aligned."""
        runtime = CodexCliRuntime(cli_path="codex")

        nested = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_na",
                    "type": "command_execution",
                    "command": "pytest -q",
                    "status": "completed",
                    "result": {"returncode": 1},
                },
            },
            current_handle=None,
        )
        nested_results = [m for m in nested if m.data.get("subtype") == "tool_result"]
        assert len(nested_results) == 1
        assert nested_results[0].data.get("is_error") is True
        tool_result = nested_results[0].data.get("tool_result") or {}
        assert (tool_result.get("meta") or {}).get("exit_status") == 1, (
            "verdict and journaled exit_status must not contradict"
        )

        conflicting = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_cf",
                    "type": "command_execution",
                    "command": "pytest -q",
                    "status": "completed",
                    "exit_code": 0,
                    "returncode": 1,
                },
            },
            current_handle=None,
        )
        conflict_results = [m for m in conflicting if m.data.get("subtype") == "tool_result"]
        assert len(conflict_results) == 1
        assert conflict_results[0].data.get("is_error") is True, (
            "failure precedence must win over a conflicting success alias"
        )
        conflict_meta = (conflict_results[0].data.get("tool_result") or {}).get("meta") or {}
        assert conflict_meta.get("exit_status") == 1, (
            "persisted exit_status must carry the failing code, not the stale zero alias"
        )

    def test_replayed_keyed_completion_is_suppressed(self) -> None:
        """Replaying one keyed item.completed must not emit a second pair."""
        runtime = CodexCliRuntime(cli_path="codex")

        first = runtime._convert_event(_COMMAND_ITEM_COMPLETED, current_handle=None)
        replay = runtime._convert_event(_COMMAND_ITEM_COMPLETED, current_handle=None)

        assert len(first) == 2, "completed-only must synthesize exactly one start+result pair"
        assert replay == [], (
            "a replayed keyed completion emitted duplicate lifecycle messages; "
            "downstream exact correlation would see two starts sharing the id"
        )

    def test_replayed_completion_after_real_lifecycle_is_suppressed(self) -> None:
        """A duplicate completion after a real started/completed pair emits nothing."""
        runtime = CodexCliRuntime(cli_path="codex")
        runtime._convert_event(_COMMAND_ITEM_STARTED, current_handle=None)
        runtime._convert_event(_COMMAND_ITEM_COMPLETED, current_handle=None)

        replay = runtime._convert_event(_COMMAND_ITEM_COMPLETED, current_handle=None)

        assert replay == []

    def test_declined_status_is_error_even_with_stale_nested_completed(self) -> None:
        """Codex's declined terminal status must resolve to failure with precedence."""
        runtime = CodexCliRuntime(cli_path="codex")
        for item in (
            {
                "id": "item_d1",
                "type": "command_execution",
                "command": "pytest -q",
                "status": "declined",
            },
            {
                "id": "item_d2",
                "type": "command_execution",
                "command": "pytest -q",
                "status": "declined",
                "result": {"status": "completed"},
            },
        ):
            messages = runtime._convert_event(
                {"type": "item.completed", "item": item}, current_handle=None
            )
            results = [m for m in messages if m.data.get("subtype") == "tool_result"]
            assert len(results) == 1, item["id"]
            assert results[0].data.get("is_error") is True, item["id"]
            assert (
                _event_has_explicit_tool_success(_as_journaled_tool_completed_event(results[0]))
                is False
            ), item["id"]

    def test_command_completed_status_alone_never_claims_success(self) -> None:
        """command_execution success requires a validated zero exit, not lifecycle status."""
        runtime = CodexCliRuntime(cli_path="codex")
        for item_id, extra in (
            ("item_nc1", {}),  # no exit code at all
            ("item_nc2", {"exit_code": "1"}),  # malformed exit alongside completed
        ):
            item = {
                "id": item_id,
                "type": "command_execution",
                "command": "pytest -q",
                "status": "completed",
                "aggregated_output": ". [100%]\n1 passed in 0.01s\n",
                **extra,
            }
            messages = runtime._convert_event(
                {"type": "item.completed", "item": item}, current_handle=None
            )
            results = [m for m in messages if m.data.get("subtype") == "tool_result"]
            assert len(results) == 1, item_id
            assert results[0].data.get("is_error") is not False, (
                f"{item_id}: lifecycle completion alone must not claim command success"
            )
            assert (
                _event_has_explicit_tool_success(_as_journaled_tool_completed_event(results[0]))
                is False
            ), item_id

    def test_consecutive_idless_completions_each_pair_without_orphans(self) -> None:
        """Identical id-less completed-only items must each emit a full pair."""
        runtime = CodexCliRuntime(cli_path="codex")
        event = {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "pytest -q",
                "exit_code": 0,
                "status": "completed",
            },
        }

        shapes = []
        for _ in range(3):
            messages = runtime._convert_event(event, current_handle=None)
            shapes.append([m.data.get("subtype") or "start" for m in messages])

        assert shapes == [
            ["start", "tool_result"],
            ["start", "tool_result"],
            ["start", "tool_result"],
        ], f"id-less completed-only lifecycles must never emit orphan results: {shapes}"

    def test_unknown_command_verdict_never_reads_as_success_signal(self) -> None:
        """Every consumer must honor the tri-state verdict, not raw status."""
        runtime = CodexCliRuntime(cli_path="codex")
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_uv",
                    "type": "command_execution",
                    "command": "pytest -q",
                    "status": "completed",
                    "exit_code": "1",
                    "aggregated_output": ". [100%]\n1 passed in 0.01s\n",
                },
            },
            current_handle=None,
        )
        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        assert len(results) == 1
        assert _runtime_message_has_success_signal(results[0]) is False, (
            "the in-memory verifier read an unvalidated completed status as success"
        )
        assert results[0].data.get("status") is None, (
            "an unknown verdict must not forward a bare success-implying status"
        )
        assert results[0].data.get("reported_status") == "completed"

    def test_mcp_error_envelope_is_failure_with_reason(self) -> None:
        """An MCP error envelope wins over a completed status and keeps the reason."""
        runtime = CodexCliRuntime(cli_path="codex")
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_mcp1",
                    "type": "mcp_tool_call",
                    "name": "filesystem.write",
                    "status": "completed",
                    "error": {"message": "permission denied"},
                },
            },
            current_handle=None,
        )
        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        assert len(results) == 1
        assert results[0].data.get("is_error") is True
        assert (
            _event_has_explicit_tool_success(_as_journaled_tool_completed_event(results[0]))
            is False
        )
        tool_result = results[0].data.get("tool_result") or {}
        assert "permission denied" in (tool_result.get("text_content") or "")

    def test_mcp_result_content_is_extracted_on_success(self) -> None:
        """A successful MCP result's content blocks become the result text."""
        runtime = CodexCliRuntime(cli_path="codex")
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_mcp2",
                    "type": "mcp_tool_call",
                    "name": "calculator.add",
                    "status": "completed",
                    "result": {"content": [{"type": "text", "text": "42"}]},
                },
            },
            current_handle=None,
        )
        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        assert len(results) == 1
        assert results[0].data.get("is_error") is False
        tool_result = results[0].data.get("tool_result") or {}
        assert "42" in (tool_result.get("text_content") or "")

    def test_replayed_keyed_start_is_suppressed(self) -> None:
        """start -> replayed start -> completion must yield exactly one pair."""
        runtime = CodexCliRuntime(cli_path="codex")

        first = runtime._convert_event(_COMMAND_ITEM_STARTED, current_handle=None)
        replay = runtime._convert_event(_COMMAND_ITEM_STARTED, current_handle=None)
        completion = runtime._convert_event(_COMMAND_ITEM_COMPLETED, current_handle=None)

        assert len(first) == 1
        assert replay == [], (
            "a replayed keyed item.started emitted a duplicate start; exact "
            "correlation requires one matching start per id"
        )
        assert len(completion) == 1
        assert completion[0].data.get("subtype") == "tool_result"

    def test_conflicting_same_id_completion_remains_visible(self) -> None:
        """A conflicting terminal envelope must not vanish behind dedup."""
        runtime = CodexCliRuntime(cli_path="codex")
        base = {
            "id": "item_cf2",
            "type": "command_execution",
            "command": "pytest -q",
            "status": "completed",
        }

        first = runtime._convert_event(
            {"type": "item.completed", "item": {**base, "exit_code": 0}}, current_handle=None
        )
        conflicting = runtime._convert_event(
            {"type": "item.completed", "item": {**base, "exit_code": 1}}, current_handle=None
        )
        identical_replay = runtime._convert_event(
            {"type": "item.completed", "item": {**base, "exit_code": 1}}, current_handle=None
        )

        assert len(first) == 2
        conflict_results = [m for m in conflicting if m.data.get("subtype") == "tool_result"]
        assert len(conflict_results) == 1, (
            "a conflicting failed completion was silently dropped by id-only dedup"
        )
        assert conflict_results[0].data.get("is_error") is True
        assert identical_replay == [], "a semantically identical replay must stay suppressed"

    def test_keyed_completion_with_mismatched_signature_does_not_pair(self) -> None:
        """A same-id completion for a different command must not inherit the start."""
        runtime = CodexCliRuntime(cli_path="codex")
        runtime._convert_event(
            {
                "type": "item.started",
                "item": {
                    "id": "item_sig",
                    "type": "command_execution",
                    "command": "pytest tests/test_a.py",
                    "status": "in_progress",
                },
            },
            current_handle=None,
        )

        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_sig",
                    "type": "command_execution",
                    "command": "printf '1 passed'",
                    "exit_code": 0,
                    "status": "completed",
                },
            },
            current_handle=None,
        )

        assert len(messages) == 2, (
            "a signature-mismatched completion paired with the pytest start and "
            "would be accepted as successful test evidence"
        )

    def test_same_thread_header_replay_preserves_correlation(self) -> None:
        """Replaying the current thread header must not wipe correlation state."""
        runtime = CodexCliRuntime(cli_path="codex")
        header = {"type": "thread.started", "thread_id": "t1"}

        runtime._convert_event(header, current_handle=None)
        runtime._convert_event(_COMMAND_ITEM_STARTED, current_handle=None)
        runtime._convert_event(header, current_handle=None)
        completion = runtime._convert_event(_COMMAND_ITEM_COMPLETED, current_handle=None)

        assert len(completion) == 1, (
            "the same-thread header replay cleared correlation and a duplicate "
            "start was synthesized"
        )
        assert completion[0].data.get("subtype") == "tool_result"

    def test_native_mcp_tool_identity_is_preserved(self) -> None:
        """Native tool+server MCP items must not journal as generic mcp_tool."""
        runtime = CodexCliRuntime(cli_path="codex")
        messages = runtime._convert_event(
            {
                "type": "item.started",
                "item": {
                    "id": "item_native",
                    "type": "mcp_tool_call",
                    "tool": "write",
                    "server": "filesystem",
                    "status": "in_progress",
                },
            },
            current_handle=None,
        )

        assert len(messages) == 1
        assert messages[0].tool_name == "filesystem.write"

    def test_reported_status_is_persisted_in_result_meta(self) -> None:
        """The audit trail keeps the raw status inside the journaled result meta."""
        runtime = CodexCliRuntime(cli_path="codex")
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_rs",
                    "type": "command_execution",
                    "command": "pytest -q",
                    "status": "completed",
                    "exit_code": "1",
                },
            },
            current_handle=None,
        )
        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        assert len(results) == 1
        meta = (results[0].data.get("tool_result") or {}).get("meta") or {}
        assert meta.get("reported_status") == "completed"

    def test_structured_content_mapping_is_serialized(self) -> None:
        """Mapping structured_content must reach the result text as JSON."""
        runtime = CodexCliRuntime(cli_path="codex")
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_sc",
                    "type": "mcp_tool_call",
                    "name": "calculator.add",
                    "status": "completed",
                    "result": {"content": [], "structured_content": {"answer": 42}},
                },
            },
            current_handle=None,
        )
        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        assert len(results) == 1
        assert "42" in ((results[0].data.get("tool_result") or {}).get("text_content") or "")

    def test_mcp_argument_mismatch_does_not_pair(self) -> None:
        """A same-id MCP completion with different arguments must not inherit the start."""
        runtime = CodexCliRuntime(cli_path="codex")
        runtime._convert_event(
            {
                "type": "item.started",
                "item": {
                    "id": "item_mcparg",
                    "type": "mcp_tool_call",
                    "tool": "write",
                    "server": "filesystem",
                    "arguments": {"path": "a"},
                    "status": "in_progress",
                },
            },
            current_handle=None,
        )

        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_mcparg",
                    "type": "mcp_tool_call",
                    "tool": "write",
                    "server": "filesystem",
                    "arguments": {"path": "b"},
                    "status": "completed",
                },
            },
            current_handle=None,
        )

        assert len(messages) == 2, (
            "an MCP completion with mismatched arguments paired with the start "
            "and would be accepted as its evidence"
        )

    def test_same_verdict_completions_with_different_output_stay_visible(self) -> None:
        """Non-identical evidence with the same verdict must not be dropped as a replay."""
        runtime = CodexCliRuntime(cli_path="codex")
        base = {
            "id": "item_out",
            "type": "command_execution",
            "command": "pytest -q",
            "exit_code": 0,
            "status": "completed",
        }

        first = runtime._convert_event(
            {"type": "item.completed", "item": {**base, "aggregated_output": "1 passed"}},
            current_handle=None,
        )
        second = runtime._convert_event(
            {"type": "item.completed", "item": {**base, "aggregated_output": "1 failed"}},
            current_handle=None,
        )
        identical = runtime._convert_event(
            {"type": "item.completed", "item": {**base, "aggregated_output": "1 failed"}},
            current_handle=None,
        )

        assert len(first) == 2
        second_results = [m for m in second if m.data.get("subtype") == "tool_result"]
        assert len(second_results) == 1, (
            "a same-verdict completion with different output was dropped as an exact replay"
        )
        assert "1 failed" in (
            (second_results[0].data.get("tool_result") or {}).get("text_content") or ""
        )
        assert identical == [], "a genuinely identical replay must stay suppressed"

    def test_malformed_error_envelope_never_claims_success(self) -> None:
        """A present but malformed non-null error envelope must fail closed."""
        runtime = CodexCliRuntime(cli_path="codex")
        for bad_error in (7, ["boom"], {"code": 500}):
            messages = runtime._convert_event(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_be",
                        "type": "mcp_tool_call",
                        "name": "filesystem.write",
                        "status": "completed",
                        "error": bad_error,
                    },
                },
                current_handle=None,
            )
            results = [m for m in messages if m.data.get("subtype") == "tool_result"]
            assert len(results) == 1, repr(bad_error)
            assert results[0].data.get("is_error") is not False, (
                f"a malformed error envelope {bad_error!r} was read as success"
            )
            assert (
                _event_has_explicit_tool_success(_as_journaled_tool_completed_event(results[0]))
                is False
            ), repr(bad_error)

    def test_file_change_conflicting_kind_does_not_pair(self) -> None:
        """A same-path completion whose mutation kind differs must not inherit the start."""
        runtime = CodexCliRuntime(cli_path="codex")
        runtime._convert_event(
            {
                "type": "item.started",
                "item": {
                    "id": "fc1",
                    "type": "file_change",
                    "status": "in_progress",
                    "changes": [{"path": "src/app.py", "kind": "add"}],
                },
            },
            current_handle=None,
        )

        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "fc1",
                    "type": "file_change",
                    "status": "completed",
                    "changes": [{"path": "src/app.py", "kind": "delete"}],
                },
            },
            current_handle=None,
        )

        assert len(messages) == 2, (
            "a file_change completion with a conflicting mutation kind paired "
            "with the start and would be accepted as its evidence"
        )

    def test_distinct_websearch_actions_same_id_stay_visible(self) -> None:
        """Same-id web_search completions differing only in a non-fingerprinted field stay visible."""
        runtime = CodexCliRuntime(cli_path="codex")
        base = {
            "id": "ws1",
            "type": "web_search",
            "query": "ouroboros",
            "status": "completed",
        }

        first = runtime._convert_event(
            {"type": "item.completed", "item": {**base, "action": "search"}}, current_handle=None
        )
        second = runtime._convert_event(
            {"type": "item.completed", "item": {**base, "action": "open"}}, current_handle=None
        )
        identical = runtime._convert_event(
            {"type": "item.completed", "item": {**base, "action": "open"}}, current_handle=None
        )

        assert len(first) == 2
        second_results = [m for m in second if m.data.get("subtype") == "tool_result"]
        assert len(second_results) == 1, (
            "a distinct same-id web_search completion was dropped as an exact replay"
        )
        assert identical == [], "a genuinely identical replay must stay suppressed"

    def test_distinct_mcp_nontext_content_same_id_stays_visible(self) -> None:
        """Same-id MCP completions with different non-text content must not collapse."""
        runtime = CodexCliRuntime(cli_path="codex")
        base = {
            "id": "mc1",
            "type": "mcp_tool_call",
            "name": "screenshot.capture",
            "status": "completed",
        }

        first = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {**base, "result": {"content": [{"type": "image", "data": "AAAA"}]}},
            },
            current_handle=None,
        )
        second = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {**base, "result": {"content": [{"type": "image", "data": "BBBB"}]}},
            },
            current_handle=None,
        )

        assert len(first) == 2
        second_results = [m for m in second if m.data.get("subtype") == "tool_result"]
        assert len(second_results) == 1, (
            "a same-id MCP completion with different image content was dropped as a replay"
        )

    def test_command_cwd_mismatch_does_not_pair(self) -> None:
        """A same-id command completion in a different cwd must not inherit the start."""
        runtime = CodexCliRuntime(cli_path="codex")
        runtime._convert_event(
            {
                "type": "item.started",
                "item": {
                    "id": "cwd1",
                    "type": "command_execution",
                    "command": "pytest -q",
                    "cwd": "/workspace/a",
                    "status": "in_progress",
                },
            },
            current_handle=None,
        )

        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "cwd1",
                    "type": "command_execution",
                    "command": "pytest -q",
                    "cwd": "/workspace/b",
                    "exit_code": 0,
                    "status": "completed",
                },
            },
            current_handle=None,
        )

        assert len(messages) == 2, "a command completion in a different cwd paired with the start"

    def test_command_start_persists_cwd_in_tool_input(self) -> None:
        """The working directory must be visible in the persisted tool input."""
        runtime = CodexCliRuntime(cli_path="codex")
        messages = runtime._convert_event(
            {
                "type": "item.started",
                "item": {
                    "id": "cwd2",
                    "type": "command_execution",
                    "command": "pytest -q",
                    "cwd": "/workspace/a",
                    "status": "in_progress",
                },
            },
            current_handle=None,
        )
        assert len(messages) == 1
        assert (messages[0].data.get("tool_input") or {}).get("cwd") == "/workspace/a"

    def test_file_change_diff_and_structured_kind_mismatch_do_not_pair(self) -> None:
        """Different diffs / structured kinds must not share a signature."""
        runtime = CodexCliRuntime(cli_path="codex")
        runtime._convert_event(
            {
                "type": "item.started",
                "item": {
                    "id": "fcd",
                    "type": "file_change",
                    "status": "in_progress",
                    "changes": [{"path": "a.py", "kind": {"update": {"unified_diff": "- old"}}}],
                },
            },
            current_handle=None,
        )
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "fcd",
                    "type": "file_change",
                    "status": "completed",
                    "changes": [{"path": "a.py", "kind": {"delete": {}}}],
                },
            },
            current_handle=None,
        )
        assert len(messages) == 2, (
            "a structured delete completion inherited an update start's signature"
        )

    def test_mcp_content_blocks_are_preserved(self) -> None:
        """A rich MCP result must keep its content blocks, not just the caption."""
        runtime = CodexCliRuntime(cli_path="codex")
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "mcprich",
                    "type": "mcp_tool_call",
                    "name": "screenshot.capture",
                    "status": "completed",
                    "result": {
                        "content": [
                            {"type": "text", "text": "here is the shot"},
                            {"type": "image", "data": "AAAA", "mimeType": "image/png"},
                            {"type": "resource", "resource": {"uri": "file:///x"}},
                        ],
                        "structured_content": {"width": 800},
                    },
                },
            },
            current_handle=None,
        )
        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        assert len(results) == 1
        tool_result = results[0].data.get("tool_result") or {}
        blocks = tool_result.get("content") or []
        types = [b.get("type") for b in blocks if isinstance(b, dict)]
        assert "image" in types, "the image content block was dropped"
        assert "resource" in types, "the resource content block was dropped"

    def test_mcp_content_blocks_survive_projection_with_values(self) -> None:
        """Rich MCP blocks must keep their values through project_runtime_message."""
        runtime = CodexCliRuntime(cli_path="codex")
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "mcpproj",
                    "type": "mcp_tool_call",
                    "name": "screenshot.capture",
                    "status": "completed",
                    "result": {
                        "content": [
                            {"type": "text", "text": "caption"},
                            {"type": "image", "data": "AAAA", "mimeType": "image/png"},
                            {"type": "resource", "resource": {"uri": "file:///x", "text": "R"}},
                        ],
                        "structured_content": {"width": 800},
                    },
                },
            },
            current_handle=None,
        )
        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        assert len(results) == 1
        projected = project_runtime_message(results[0])
        blocks = (projected.tool_result or {}).get("content") or []
        by_type = {b.get("type"): b for b in blocks if isinstance(b, dict)}
        assert by_type.get("image", {}).get("mime_type") == "image/png", (
            "image mime lost in projection"
        )
        assert by_type.get("image", {}).get("data") == "AAAA", "image data lost in projection"
        assert by_type.get("resource", {}).get("uri") == "file:///x", (
            "resource uri lost in projection"
        )
        structured = by_type.get("structured", {})
        assert "800" in json.dumps(structured), "structured payload lost in projection"

    def test_idless_multifile_paths_have_stable_correlation_ids(self) -> None:
        """Each path in an id-less multi-file item must carry a stable synthetic id."""
        runtime = CodexCliRuntime(cli_path="codex")
        item = {
            "type": "file_change",
            "status": "completed",
            "changes": [
                {"path": "src/app.py", "kind": "update"},
                {"path": "tests/test_app.py", "kind": "add"},
            ],
        }
        messages = runtime._convert_event(
            {"type": "item.completed", "item": item}, current_handle=None
        )
        starts = [m for m in messages if m.data.get("subtype") != "tool_result"]
        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        start_ids = [m.data.get("tool_call_id") for m in starts]
        result_ids = [m.data.get("tool_call_id") for m in results]
        assert None not in start_ids and None not in result_ids, (
            "an id-less multi-file item left tool_call_id=None, breaking exact correlation"
        )
        assert len(set(result_ids)) == 2, "the two paths must have distinct correlation ids"
        assert set(start_ids) == set(result_ids), "start and result ids must correlate per path"

    def test_nested_mcp_error_message_becomes_result_text(self) -> None:
        """A nested result.error.message must surface as the journaled failure reason."""
        runtime = CodexCliRuntime(cli_path="codex")
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "nerr",
                    "type": "mcp_tool_call",
                    "name": "filesystem.write",
                    "status": "completed",
                    "result": {"error": {"message": "disk full"}},
                },
            },
            current_handle=None,
        )
        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        assert len(results) == 1
        assert results[0].data.get("is_error") is True
        tool_result = results[0].data.get("tool_result") or {}
        assert "disk full" in (tool_result.get("text_content") or ""), (
            "the nested error reason was dropped, leaving the journaled failure blank"
        )

    def test_consecutive_idless_same_path_edits_get_unique_ids(self) -> None:
        """Two id-less completed-only edits of one path must not share a correlation id."""
        runtime = CodexCliRuntime(cli_path="codex")
        event = {
            "type": "item.completed",
            "item": {
                "type": "file_change",
                "status": "completed",
                "changes": [{"path": "a.py", "kind": "update"}],
            },
        }

        first = runtime._convert_event(event, current_handle=None)
        second = runtime._convert_event(event, current_handle=None)

        def ids(msgs):
            return {m.data.get("tool_call_id") for m in msgs}

        assert None not in ids(first) and None not in ids(second)
        assert len(first) == 2 and len(second) == 2
        # A start and its result share one id within an invocation.
        assert len(ids(first)) == 1 and len(ids(second)) == 1
        # ...but the two invocations must be distinct so exact correlation
        # never sees two starts sharing one id.
        assert ids(first).isdisjoint(ids(second)), (
            "two id-less edits of the same path shared a correlation id"
        )

    def test_idless_pair_start_and_result_share_one_id(self) -> None:
        """An id-less start then completion must correlate on a single shared id."""
        runtime = CodexCliRuntime(cli_path="codex")
        item = {
            "type": "file_change",
            "status": "in_progress",
            "changes": [{"path": "a.py", "kind": "update"}],
        }
        start = runtime._convert_event({"type": "item.started", "item": item}, current_handle=None)
        completion = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {**item, "status": "completed"},
            },
            current_handle=None,
        )
        assert len(start) == 1
        assert len(completion) == 1
        assert start[0].data.get("tool_call_id") is not None
        assert start[0].data.get("tool_call_id") == completion[0].data.get("tool_call_id")

    def test_file_change_toplevel_path_mismatch_does_not_pair(self) -> None:
        """Top-level path changes must be part of the signature."""
        runtime = CodexCliRuntime(cli_path="codex")
        runtime._convert_event(
            {
                "type": "item.started",
                "item": {
                    "id": "tp",
                    "type": "file_change",
                    "status": "in_progress",
                    "path": "old.py",
                    "changes": [{"kind": "update", "unified_diff": "- x"}],
                },
            },
            current_handle=None,
        )
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "tp",
                    "type": "file_change",
                    "status": "completed",
                    "path": "new.py",
                    "changes": [{"kind": "update", "unified_diff": "- x"}],
                },
            },
            current_handle=None,
        )
        assert len(messages) == 2, (
            "a completion with a different top-level path inherited the start's signature"
        )

    def test_mcp_resource_blob_and_meta_are_preserved(self) -> None:
        """resource.blob reaches content data and result _meta reaches tool_result meta."""
        runtime = CodexCliRuntime(cli_path="codex")
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "blob",
                    "type": "mcp_tool_call",
                    "name": "fs.read",
                    "status": "completed",
                    "result": {
                        "content": [
                            {
                                "type": "resource",
                                "resource": {"uri": "file:///b", "blob": "QkxPQg=="},
                            }
                        ],
                        "_meta": {"trace_id": "t-123"},
                    },
                },
            },
            current_handle=None,
        )
        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        assert len(results) == 1
        tool_result = results[0].data.get("tool_result") or {}
        blocks = tool_result.get("content") or []
        resource_block = next((b for b in blocks if b.get("type") == "resource"), {})
        assert resource_block.get("data") == "QkxPQg==", "resource blob was dropped"
        mcp_meta = (tool_result.get("meta") or {}).get("mcp_meta") or {}
        assert mcp_meta.get("trace_id") == "t-123", "result _meta was dropped or de-namespaced"

    def test_mcp_iserror_wire_field_fails_closed(self) -> None:
        """The canonical MCP result.isError flag must win over a completed status."""
        runtime = CodexCliRuntime(cli_path="codex")
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "iserr",
                    "type": "mcp_tool_call",
                    "name": "fs.write",
                    "status": "completed",
                    "result": {"isError": True, "content": [{"type": "text", "text": "nope"}]},
                },
            },
            current_handle=None,
        )
        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        assert len(results) == 1
        assert results[0].data.get("is_error") is True
        assert (
            _event_has_explicit_tool_success(_as_journaled_tool_completed_event(results[0]))
            is False
        )

    def test_mcp_camelcase_structured_content_survives_projection(self) -> None:
        """The canonical structuredContent wire field must be preserved."""
        runtime = CodexCliRuntime(cli_path="codex")
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "sc",
                    "type": "mcp_tool_call",
                    "name": "calc.add",
                    "status": "completed",
                    "result": {"content": [], "structuredContent": {"answer": 99}},
                },
            },
            current_handle=None,
        )
        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        assert len(results) == 1
        projected = project_runtime_message(results[0])
        blocks = (projected.tool_result or {}).get("content") or []
        structured = next((b for b in blocks if b.get("type") == "structured"), {})
        assert "99" in json.dumps(structured), "canonical structuredContent was discarded"

    def test_command_iserror_false_alone_never_claims_success(self) -> None:
        """isError is not part of the command contract; it cannot grant success."""
        runtime = CodexCliRuntime(cli_path="codex")
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "ce",
                    "type": "command_execution",
                    "command": "pytest -q",
                    "status": "completed",
                    "isError": False,
                },
            },
            current_handle=None,
        )
        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        assert len(results) == 1
        assert results[0].data.get("is_error") is not False, (
            "a command with only isError=false and no valid exit claimed success"
        )

    def test_malformed_iserror_string_fails_closed(self) -> None:
        """A present-but-malformed isError must poison the success claim."""
        runtime = CodexCliRuntime(cli_path="codex")
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "me",
                    "type": "mcp_tool_call",
                    "name": "fs.write",
                    "status": "completed",
                    "result": {"isError": "false"},
                },
            },
            current_handle=None,
        )
        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        assert len(results) == 1
        assert results[0].data.get("is_error") is not False, (
            "a malformed isError string was ignored and status granted success"
        )
        assert (
            _event_has_explicit_tool_success(_as_journaled_tool_completed_event(results[0]))
            is False
        )

    def test_mcp_result_meta_cannot_forge_authority_keys(self) -> None:
        """Opaque MCP _meta must not populate the authoritative exit_status key."""
        runtime = CodexCliRuntime(cli_path="codex")
        # Unknown verdict (no exit, no explicit success), but _meta forges exit_status=0.
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "meta",
                    "type": "mcp_tool_call",
                    "name": "fs.write",
                    "result": {"_meta": {"exit_status": 0, "trace_id": "t-1"}},
                },
            },
            current_handle=None,
        )
        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        assert len(results) == 1
        meta = (results[0].data.get("tool_result") or {}).get("meta") or {}
        assert meta.get("exit_status") != 0, "opaque MCP _meta forged an authoritative exit_status"
        assert (
            _event_has_explicit_tool_success(_as_journaled_tool_completed_event(results[0]))
            is False
        ), "forged _meta.exit_status turned an unknown MCP verdict into success"
        # audit data must still be preserved, just namespaced
        assert "t-1" in json.dumps(meta), "MCP audit metadata was lost entirely"

    def test_tool_result_does_not_inherit_stale_terminal_handle_event_type(self) -> None:
        """A resumed handle's terminal runtime_event_type must not leak onto results."""
        runtime = CodexCliRuntime(cli_path="codex")
        stale_handle = RuntimeHandle(
            backend="codex_cli",
            metadata={"runtime_event_type": "run.completed"},
        )
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "resume",
                    "type": "command_execution",
                    "command": "pytest -q",
                    "status": "completed",
                    "exit_code": "1",
                },
            },
            current_handle=stale_handle,
        )
        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        assert len(results) == 1
        projected = project_runtime_message(results[0])
        event_type = (projected.runtime_metadata or {}).get("runtime_event_type") or ""
        assert not event_type.endswith((".completed", ".succeeded")), (
            f"the result inherited a stale terminal event type: {event_type!r}"
        )
        assert (
            _event_has_explicit_tool_success(_as_journaled_tool_completed_event(results[0]))
            is False
        )

    def test_synthesized_keyed_start_suppresses_later_start_replay(self) -> None:
        """A completed-only keyed item then a late start must not duplicate the start."""
        runtime = CodexCliRuntime(cli_path="codex")
        completed = {
            "type": "item.completed",
            "item": {
                "id": "late",
                "type": "command_execution",
                "command": "pytest -q",
                "exit_code": 0,
                "status": "completed",
            },
        }
        started = {
            "type": "item.started",
            "item": {
                "id": "late",
                "type": "command_execution",
                "command": "pytest -q",
                "status": "in_progress",
            },
        }

        pair = runtime._convert_event(completed, current_handle=None)
        late_start = runtime._convert_event(started, current_handle=None)

        assert len(pair) == 2
        assert late_start == [], (
            "a late keyed start after a synthesized completion emitted a duplicate start"
        )

    def test_stray_exit_code_on_noncommand_item_is_not_success(self) -> None:
        """A stray exit_code=0 on a non-command item must not forge success."""
        runtime = CodexCliRuntime(cli_path="codex")
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {"id": "w", "type": "web_search", "query": "cats", "exit_code": 0},
            },
            current_handle=None,
        )
        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        assert len(results) == 1
        assert _runtime_message_has_success_signal(results[0]) is False, (
            "in-memory verifier read a stray non-command exit_code=0 as success"
        )
        assert (
            _event_has_explicit_tool_success(_as_journaled_tool_completed_event(results[0]))
            is False
        ), "deliver gate read a stray non-command exit_status=0 as success"

    def test_voided_command_verdict_does_not_leak_exit_success(self) -> None:
        """A command verdict voided by a malformed field must not forge success via exit_code."""
        runtime = CodexCliRuntime(cli_path="codex")
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "c",
                    "type": "command_execution",
                    "command": "pytest -q",
                    "exit_code": 0,
                    "success": "yes",
                },
            },
            current_handle=None,
        )
        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        assert len(results) == 1
        assert results[0].data.get("is_error") is None
        assert _runtime_message_has_success_signal(results[0]) is False
        assert (
            _event_has_explicit_tool_success(_as_journaled_tool_completed_event(results[0]))
            is False
        )

    def test_validated_command_success_still_journals_exit_status(self) -> None:
        """A genuinely successful command (is_error=False) keeps authoritative exit_status."""
        runtime = CodexCliRuntime(cli_path="codex")
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "ok",
                    "type": "command_execution",
                    "command": "pytest -q",
                    "exit_code": 0,
                    "status": "completed",
                    "aggregated_output": "1 passed",
                },
            },
            current_handle=None,
        )
        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        assert results[0].data.get("is_error") is False
        assert (
            _event_has_explicit_tool_success(_as_journaled_tool_completed_event(results[0])) is True
        ), "a validated command success lost its authoritative exit_status"

    def test_tool_start_does_not_inherit_stale_terminal_handle_event_type(self) -> None:
        """The start half must also not leak a resumed handle's terminal event type."""
        runtime = CodexCliRuntime(cli_path="codex")
        stale_handle = RuntimeHandle(
            backend="codex_cli", metadata={"runtime_event_type": "run.completed"}
        )
        messages = runtime._convert_event(
            {
                "type": "item.started",
                "item": {
                    "id": "s",
                    "type": "command_execution",
                    "command": "pytest -q",
                    "status": "in_progress",
                },
            },
            current_handle=stale_handle,
        )
        assert len(messages) == 1
        projected = project_runtime_message(messages[0])
        event_type = (projected.runtime_metadata or {}).get("runtime_event_type") or ""
        assert not event_type.endswith((".completed", ".succeeded")), (
            f"the start inherited a stale terminal event type: {event_type!r}"
        )

    def test_failure_under_alternate_wire_keys_is_detected(self) -> None:
        """A failure token under errorCode/statusCode must not be invisible."""
        runtime = CodexCliRuntime(cli_path="codex")
        for fail_field in ("errorCode", "statusCode", "exit"):
            messages = runtime._convert_event(
                {
                    "type": "item.completed",
                    "item": {
                        "id": f"f_{fail_field}",
                        "type": "file_change",
                        "status": "completed",
                        "changes": [{"path": "a.py", "kind": "update"}],
                        fail_field: 1,
                    },
                },
                current_handle=None,
            )
            results = [m for m in messages if m.data.get("subtype") == "tool_result"]
            assert results[0].data.get("is_error") is True, (
                f"a nonzero {fail_field} failure token was invisible and left success"
            )

    def test_mcp_toolname_wire_spelling_is_recognized(self) -> None:
        """MCP identity must recognize the toolName wire spelling."""
        runtime = CodexCliRuntime(cli_path="codex")
        messages = runtime._convert_event(
            {
                "type": "item.started",
                "item": {
                    "id": "tn",
                    "type": "mcp_tool_call",
                    "toolName": "write",
                    "server": "fs",
                    "status": "in_progress",
                },
            },
            current_handle=None,
        )
        assert messages[0].tool_name == "fs.write"

    def test_malformed_failure_code_alias_cannot_be_promoted_to_success(self) -> None:
        """A present-but-malformed failure-code alias must block a status success."""
        runtime = CodexCliRuntime(cli_path="codex")
        for bad in ({"statusCode": "500"}, {"errorCode": "E_FAIL"}):
            messages = runtime._convert_event(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "fc",
                        "type": "mcp_tool_call",
                        "name": "t",
                        "status": "completed",
                        "result": bad,
                    },
                },
                current_handle=None,
            )
            results = [m for m in messages if m.data.get("subtype") == "tool_result"]
            assert results[0].data.get("is_error") is not False, (
                f"a malformed failure-code alias {bad} was ignored and status promoted success"
            )

    def test_command_nested_input_workdir_is_part_of_identity(self) -> None:
        """input.workdir must be in the command identity and persisted tool input."""
        runtime = CodexCliRuntime(cli_path="codex")
        start = runtime._convert_event(
            {
                "type": "item.started",
                "item": {
                    "id": "wd",
                    "type": "command_execution",
                    "command": "pytest -q",
                    "input": {"workdir": "/w/a"},
                    "status": "in_progress",
                },
            },
            current_handle=None,
        )
        assert (start[0].data.get("tool_input") or {}).get("cwd") == "/w/a"
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "wd",
                    "type": "command_execution",
                    "command": "pytest -q",
                    "input": {"workdir": "/w/b"},
                    "exit_code": 0,
                    "status": "completed",
                },
            },
            current_handle=None,
        )
        assert len(messages) == 2, "a completion with a different nested workdir mispaired"

    def test_mcp_meta_secrets_are_redacted_before_persistence(self) -> None:
        """Sensitive MCP _meta keys must not survive into durable journal metadata."""
        runtime = CodexCliRuntime(cli_path="codex")
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "sec",
                    "type": "mcp_tool_call",
                    "name": "t",
                    "status": "completed",
                    "result": {
                        "_meta": {
                            "trace_id": "t-1",
                            "authorization": "Bearer xyz",
                            "nested": {"api_key": "sk-secret", "ok": 1},
                        }
                    },
                },
            },
            current_handle=None,
        )
        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        serialized = json.dumps(results[0].data.get("tool_result") or {})
        assert "xyz" not in serialized, "authorization secret survived into journal metadata"
        assert "sk-secret" not in serialized, "nested api_key survived into journal metadata"
        assert "t-1" in serialized, "non-sensitive audit data was lost"

    def test_completion_dedup_stores_bounded_digest(self) -> None:
        """The dedup set must not retain multi-megabyte completion envelopes."""
        runtime = CodexCliRuntime(cli_path="codex")
        big = "x" * 2_000_000
        runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "big",
                    "type": "command_execution",
                    "command": "pytest -q",
                    "exit_code": 0,
                    "status": "completed",
                    "aggregated_output": big,
                },
            },
            current_handle=None,
        )
        # Reach into the default scope's dedup set and assert entries are bounded.
        keys = runtime._default_item_scope.completed_item_keys
        assert keys, "no dedup key recorded"
        assert all(len(k) < 4096 for k in keys), (
            "dedup stored an unbounded envelope instead of a fixed-size digest"
        )

    def test_resumed_tool_start_keeps_started_event_type(self) -> None:
        """Neutralizing a start handle must not stamp it with a result event type."""
        runtime = CodexCliRuntime(cli_path="codex")
        stale = RuntimeHandle(backend="codex_cli", metadata={"runtime_event_type": "run.completed"})
        messages = runtime._convert_event(
            {
                "type": "item.started",
                "item": {
                    "id": "st",
                    "type": "command_execution",
                    "command": "pytest -q",
                    "status": "in_progress",
                },
            },
            current_handle=stale,
        )
        projected = project_runtime_message(messages[0])
        event_type = (projected.runtime_metadata or {}).get("runtime_event_type") or ""
        assert event_type != "tool.result", "a tool start was stamped with the result event type"
        assert not event_type.endswith((".completed", ".succeeded"))

    def test_oauth_token_meta_keys_are_redacted(self) -> None:
        """Common OAuth token keys must be redacted from durable meta."""
        runtime = CodexCliRuntime(cli_path="codex")
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "tk",
                    "type": "mcp_tool_call",
                    "name": "t",
                    "status": "completed",
                    "result": {
                        "_meta": {
                            "token": "tok-1",
                            "refresh_token": "ref-2",
                            "idToken": "id-3",
                            "trace_id": "keep",
                        }
                    },
                },
            },
            current_handle=None,
        )
        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        serialized = json.dumps(results[0].data.get("tool_result") or {})
        for secret in ("tok-1", "ref-2", "id-3"):
            assert secret not in serialized, f"{secret} survived into journal metadata"
        assert "keep" in serialized

    def test_nonstring_status_is_malformed_not_ignored(self) -> None:
        """A present non-string status must poison the success claim."""
        runtime = CodexCliRuntime(cli_path="codex")
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "st",
                    "type": "command_execution",
                    "command": "pytest -q",
                    "exit_code": 0,
                    "status": 7,
                },
            },
            current_handle=None,
        )
        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        assert results[0].data.get("is_error") is None, (
            "a non-string status was ignored and exit 0 claimed success"
        )
        assert (
            _event_has_explicit_tool_success(_as_journaled_tool_completed_event(results[0]))
            is False
        )

    def test_same_id_different_tool_type_does_not_pair(self) -> None:
        """A reused id across different tool types must not correlate."""
        runtime = CodexCliRuntime(cli_path="codex")
        runtime._convert_event(
            {
                "type": "item.started",
                "item": {
                    "id": "reuse",
                    "type": "command_execution",
                    "command": "cats",
                    "status": "in_progress",
                },
            },
            current_handle=None,
        )
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "reuse",
                    "type": "web_search",
                    "query": "cats",
                    "status": "completed",
                },
            },
            current_handle=None,
        )
        assert len(messages) == 2, (
            "a WebSearch completion inherited a Bash start under the same reused id"
        )

    def test_mcp_resource_text_reaches_result_summary(self) -> None:
        """A resource block's text must populate the human-readable result text."""
        runtime = CodexCliRuntime(cli_path="codex")
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "rt",
                    "type": "mcp_tool_call",
                    "name": "fs.read",
                    "status": "completed",
                    "result": {
                        "content": [{"type": "resource", "resource": {"text": "file body"}}]
                    },
                },
            },
            current_handle=None,
        )
        results = [m for m in messages if m.data.get("subtype") == "tool_result"]
        text = (results[0].data.get("tool_result") or {}).get("text_content") or ""
        assert "file body" in text, "resource text was lost from the result summary"

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
