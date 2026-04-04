"""Tests for backend-neutral runtime message projection."""

from __future__ import annotations

from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.orchestrator.mcp_tools import (
    normalize_opencode_tool_result,
    normalize_runtime_tool_result,
)
from ouroboros.orchestrator.runtime_message_projection import project_runtime_message


class TestRuntimeMessageProjection:
    """Tests for transcript/event projection of runtime messages."""

    def test_projects_opencode_session_started_metadata_into_standard_signal(self) -> None:
        """Session-start lifecycle updates should stay system-scoped and resumable."""
        message = AgentMessage(
            type="system",
            content="OpenCode session initialized",
            data={
                "subtype": "init",
                "session_id": "oc-session-1",
                "server_session_id": "server-42",
            },
            resume_handle=RuntimeHandle(
                backend="opencode",
                native_session_id="oc-session-1",
                cwd="/tmp/project",
                approval_mode="acceptEdits",
                metadata={
                    "runtime_event_type": "session.started",
                    "server_session_id": "server-42",
                },
            ),
        )

        projected = project_runtime_message(message)

        assert projected.message_type == "system"
        assert projected.runtime_signal == "session_started"
        assert projected.runtime_status == "running"
        assert projected.runtime_metadata["session_id"] == "oc-session-1"
        assert projected.runtime_metadata["server_session_id"] == "server-42"
        assert projected.runtime_metadata["runtime_signal"] == "session_started"
        assert projected.runtime_metadata["runtime_status"] == "running"

    def test_projects_recovery_catalog_mismatch_metadata_for_audit_persistence(self) -> None:
        """Replacement-session recovery metadata should survive into persisted audit payloads."""
        catalog_mismatch = {
            "expected_tool_catalog": [
                {"id": "builtin:Read", "name": "Read"},
                {"id": "builtin:Edit", "name": "Edit"},
            ],
            "replacement_tool_catalog": [
                {"id": "builtin:Read", "name": "Read"},
                {"id": "builtin:Bash", "name": "Bash"},
            ],
            "missing_tool_ids": ["builtin:Edit"],
            "unexpected_tool_ids": ["builtin:Bash"],
            "changed_tool_ids": [],
        }
        message = AgentMessage(
            type="system",
            content="Recovered in replacement session: oc-session-2",
            data={
                "subtype": "init",
                "session_id": "oc-session-2",
                "recovery": {
                    "kind": "replacement_session",
                    "replaced_session_id": "oc-session-1",
                    "replacement_session_id": "oc-session-2",
                    "catalog_mismatch": catalog_mismatch,
                },
                "catalog_mismatch": catalog_mismatch,
            },
            resume_handle=RuntimeHandle(
                backend="opencode",
                native_session_id="oc-session-2",
                cwd="/tmp/project",
                approval_mode="acceptEdits",
                metadata={
                    "runtime_event_type": "session.started",
                    "server_session_id": "server-202",
                },
            ),
        )

        projected = project_runtime_message(message)

        assert projected.message_type == "system"
        assert projected.runtime_signal == "session_started"
        assert projected.runtime_status == "running"
        assert projected.runtime_metadata["recovery"]["kind"] == "replacement_session"
        assert projected.runtime_metadata["recovery"]["replaced_session_id"] == "oc-session-1"
        assert projected.runtime_metadata["catalog_mismatch"]["missing_tool_ids"] == [
            "builtin:Edit"
        ]
        assert projected.runtime_metadata["catalog_mismatch"]["unexpected_tool_ids"] == [
            "builtin:Bash"
        ]

    def test_projects_opencode_result_progress_as_standard_result_signal(self) -> None:
        """Terminal OpenCode result events should project as shared result output."""
        message = AgentMessage(
            type="assistant",
            content="Applied the requested changes.",
            data={"subtype": "result_progress"},
            resume_handle=RuntimeHandle(
                backend="opencode",
                native_session_id="oc-session-2",
                metadata={"runtime_event_type": "result.completed"},
            ),
        )

        projected = project_runtime_message(message)

        assert projected.message_type == "result"
        assert projected.content == "Applied the requested changes."
        assert projected.runtime_signal == "session_completed"
        assert projected.runtime_status == "completed"
        assert projected.runtime_metadata["runtime_signal"] == "session_completed"
        assert projected.runtime_metadata["runtime_status"] == "completed"

    def test_projects_opencode_failure_progress_as_standard_failed_result_signal(self) -> None:
        """Terminal OpenCode failure events should project into shared failed-result signals."""
        message = AgentMessage(
            type="assistant",
            content="OpenCode session disconnected",
            data={
                "subtype": "runtime_error",
                "error_type": "SessionDisconnected",
            },
            resume_handle=RuntimeHandle(
                backend="opencode",
                cwd="/tmp/project",
                approval_mode="acceptEdits",
                metadata={
                    "runtime_event_type": "run.failed",
                    "server_session_id": "server-99",
                },
            ),
        )

        projected = project_runtime_message(message)

        assert projected.message_type == "result"
        assert projected.runtime_signal == "session_failed"
        assert projected.runtime_status == "failed"
        assert projected.runtime_metadata["error_type"] == "SessionDisconnected"
        assert projected.runtime_metadata["server_session_id"] == "server-99"
        assert projected.runtime_metadata["runtime_signal"] == "session_failed"
        assert projected.runtime_metadata["runtime_status"] == "failed"

    def test_projects_reconnect_metadata_from_runtime_handle_when_turn_payload_omits_ids(
        self,
    ) -> None:
        """Projected OpenCode turns should stay reconnectable from the carried handle alone."""
        message = AgentMessage(
            type="assistant",
            content="Continuing implementation in the existing OpenCode session.",
            resume_handle=RuntimeHandle(
                backend="opencode",
                kind="implementation_session",
                native_session_id="oc-session-3",
                cwd="/tmp/project",
                approval_mode="acceptEdits",
                metadata={
                    "server_session_id": "server-303",
                    "capability_graph": [{"name": "Read"}],
                    "control_plane": [{"name": "Read", "execution_mode": "parallel"}],
                    "session_scope_id": "orch_123_ac_3",
                    "session_state_path": (
                        "execution.workflows.orch_123.acceptance_criteria."
                        "ac_3.implementation_session"
                    ),
                    "session_role": "implementation",
                    "retry_attempt": 0,
                    "runtime_event_type": "assistant.message.delta",
                },
            ),
        )

        projected = project_runtime_message(message)

        assert projected.message_type == "assistant"
        assert projected.runtime_metadata["session_id"] == "oc-session-3"
        assert projected.runtime_metadata["server_session_id"] == "server-303"
        assert projected.runtime_metadata["resume_session_id"] == "oc-session-3"
        assert projected.runtime_metadata["capability_graph"] == [{"name": "Read"}]
        assert projected.runtime_metadata["control_plane"] == [
            {"name": "Read", "execution_mode": "parallel"}
        ]
        assert projected.runtime_metadata["runtime"] == {
            "backend": "opencode",
            "kind": "implementation_session",
            "native_session_id": "oc-session-3",
            "cwd": "/tmp/project",
            "approval_mode": "acceptEdits",
            "metadata": {
                "server_session_id": "server-303",
                "capability_graph": [{"name": "Read"}],
                "control_plane": [{"name": "Read", "execution_mode": "parallel"}],
                "session_scope_id": "orch_123_ac_3",
                "session_state_path": (
                    "execution.workflows.orch_123.acceptance_criteria.ac_3.implementation_session"
                ),
                "session_role": "implementation",
                "retry_attempt": 0,
            },
        }

    def test_projects_opencode_tool_completed_payload_as_serialized_tool_result(self) -> None:
        """OpenCode tool completions should attach MCP-compatible tool result data."""
        message = AgentMessage(
            type="assistant",
            content="",
            data={
                "subtype": "tool_result",
                "tool_name": "Bash",
                "tool_input": {"command": "pytest -q"},
                "tool_result": normalize_opencode_tool_result(
                    {
                        "type": "tool.completed",
                        "tool_name": "Bash",
                        "stdout": "pytest -q passed",
                        "exit_code": 0,
                    }
                ),
            },
            resume_handle=RuntimeHandle(
                backend="opencode",
                native_session_id="oc-session-3",
                metadata={"runtime_event_type": "tool.completed"},
            ),
        )

        projected = project_runtime_message(message)

        assert projected.message_type == "tool_result"
        assert projected.tool_name == "Bash"
        assert projected.content == "pytest -q passed"
        assert projected.tool_result is not None
        assert projected.tool_result["text_content"] == "pytest -q passed"
        assert projected.tool_result["is_error"] is False
        assert projected.tool_result["meta"]["runtime_event_type"] == "tool.completed"
        assert projected.runtime_signal == "tool_completed"
        assert projected.runtime_status == "running"
        assert projected.runtime_metadata["tool_result"] == projected.tool_result

    def test_projects_ac_tracking_from_tool_result_payload_when_content_is_generic(self) -> None:
        """AC markers in normalized tool results should survive generic progress text."""
        message = AgentMessage(
            type="assistant",
            content="Tool completed successfully.",
            data={
                "subtype": "tool_result",
                "tool_name": "Edit",
                "tool_result": normalize_runtime_tool_result("[AC_COMPLETE: 2] Done!"),
            },
        )

        projected = project_runtime_message(message)

        assert projected.content == "Tool completed successfully."
        assert projected.runtime_metadata["ac_tracking"] == {"started": [], "completed": [2]}

    def test_projects_opencode_tool_failed_payload_as_serialized_tool_result(self) -> None:
        """OpenCode tool failures should project as non-fatal tool-result metadata."""
        message = AgentMessage(
            type="assistant",
            content="",
            data={
                "subtype": "tool_result",
                "tool_name": "Bash",
                "tool_input": {"command": "pytest -q"},
                "tool_result": normalize_opencode_tool_result(
                    {
                        "type": "tool.failed",
                        "tool_name": "Bash",
                        "stderr": "1 test failed",
                        "error": {
                            "message": "Command exited with code 1",
                            "type": "CommandFailed",
                        },
                        "exit_code": 1,
                    }
                ),
            },
            resume_handle=RuntimeHandle(
                backend="opencode",
                native_session_id="oc-session-4",
                metadata={"runtime_event_type": "tool.failed"},
            ),
        )

        projected = project_runtime_message(message)

        assert projected.message_type == "tool_result"
        assert projected.tool_name == "Bash"
        assert projected.content == "1 test failed\nCommand exited with code 1"
        assert projected.tool_result is not None
        assert projected.tool_result["is_error"] is True
        assert projected.tool_result["meta"]["exit_status"] == 1
        assert projected.tool_result["meta"]["error_type"] == "CommandFailed"
        assert projected.runtime_signal == "tool_completed"
        assert projected.runtime_status == "running"
        assert projected.runtime_metadata["tool_result"] == projected.tool_result

    def test_projects_structured_content_part_tool_metadata(self) -> None:
        """Structured session-message metadata should survive runtime projection."""
        message = AgentMessage(
            type="assistant",
            content="",
            data={
                "subtype": "tool_result",
                "tool_name": "github_search",
                "tool_call_id": "tool-call-1",
                "content_part_index": 2,
                "content_part_type": "mcp_tool_result",
                "tool_result": normalize_opencode_tool_result(
                    {
                        "type": "mcp_tool_result",
                        "tool_name": "github_search",
                        "tool_call_id": "tool-call-1",
                        "result": {"text": "Found matching repository."},
                    }
                ),
            },
        )

        projected = project_runtime_message(message)

        assert projected.message_type == "tool_result"
        assert projected.content == "Found matching repository."
        assert projected.runtime_metadata["tool_call_id"] == "tool-call-1"
        assert projected.runtime_metadata["content_part_index"] == 2
        assert projected.runtime_metadata["content_part_type"] == "mcp_tool_result"
