"""Focused retry-resume coverage for AC-scoped OpenCode sessions."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor


@pytest.mark.asyncio
async def test_restarted_executor_starts_fresh_handle_for_next_retry_attempt() -> None:
    """A reopened retry must keep AC scope but start with a fresh runtime handle."""

    class _StubRetryResumeRuntime:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self._runtime_handle_backend = "opencode"
            self._cwd = "/tmp/project"
            self._permission_mode = "acceptEdits"

        @property
        def runtime_backend(self) -> str:
            return self._runtime_handle_backend

        @property
        def working_directory(self) -> str | None:
            return self._cwd

        @property
        def permission_mode(self) -> str | None:
            return self._permission_mode

        async def execute_task(
            self,
            prompt: str,
            tools: list[str] | None = None,
            system_prompt: str | None = None,
            resume_handle: RuntimeHandle | None = None,
            resume_session_id: str | None = None,
        ):
            self.calls.append(
                {
                    "prompt": prompt,
                    "tools": tools,
                    "system_prompt": system_prompt,
                    "resume_handle": resume_handle,
                    "resume_session_id": resume_session_id,
                }
            )
            bound_handle = RuntimeHandle(
                backend=resume_handle.backend if resume_handle is not None else "opencode",
                kind=resume_handle.kind if resume_handle is not None else "implementation_session",
                native_session_id="opencode-session-retry-attempt-2",
                cwd=resume_handle.cwd if resume_handle is not None else "/tmp/project",
                approval_mode=(
                    resume_handle.approval_mode if resume_handle is not None else "acceptEdits"
                ),
                metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
            )
            yield AgentMessage(
                type="result",
                content="[TASK_COMPLETE]",
                data={"subtype": "success"},
                resume_handle=bound_handle,
            )

    persisted_handle = RuntimeHandle(
        backend="opencode",
        kind="implementation_session",
        native_session_id="opencode-session-retry",
        cwd="/tmp/project",
        approval_mode="acceptEdits",
        metadata={
            "scope": "ac",
            "session_role": "implementation",
            "retry_attempt": 0,
            "ac_index": 1,
            "session_scope_id": "orch_123_ac_1",
            "session_state_path": (
                "execution.workflows.orch_123.acceptance_criteria.ac_1.implementation_session"
            ),
            "server_session_id": "server-99",
        },
    )
    event_store = AsyncMock()
    event_store.replay = AsyncMock(
        return_value=[
            BaseEvent(
                type="execution.session.started",
                aggregate_type="execution",
                aggregate_id="orch_123_ac_1",
                data={
                    "retry_attempt": 0,
                    "session_state_path": (
                        "execution.workflows.orch_123.acceptance_criteria."
                        "ac_1.implementation_session"
                    ),
                    "runtime": persisted_handle.to_dict(),
                },
            ),
            BaseEvent(
                type="execution.session.failed",
                aggregate_type="execution",
                aggregate_id="orch_123_ac_1",
                data={
                    "retry_attempt": 0,
                    "session_state_path": (
                        "execution.workflows.orch_123.acceptance_criteria."
                        "ac_1.implementation_session"
                    ),
                    "runtime": persisted_handle.to_dict(),
                    "success": False,
                },
            ),
        ]
    )
    event_store.append = AsyncMock()
    runtime = _StubRetryResumeRuntime()
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=event_store,
        console=MagicMock(),
        enable_decomposition=False,
    )

    result = await executor._execute_atomic_ac(
        ac_index=1,
        ac_content="Resume the failed AC implementation from the persisted session",
        session_id="orch_123",
        tools=["Read", "Edit"],
        system_prompt="system",
        seed_goal="Ship the feature",
        depth=0,
        start_time=datetime.now(UTC),
        retry_attempt=1,
    )

    resume_handle = runtime.calls[0]["resume_handle"]
    assert isinstance(resume_handle, RuntimeHandle)
    assert resume_handle.native_session_id is None
    assert "server_session_id" not in resume_handle.metadata
    assert resume_handle.metadata["retry_attempt"] == 1
    assert resume_handle.metadata["attempt_number"] == 2
    assert resume_handle.metadata["ac_index"] == 1
    assert resume_handle.metadata["session_scope_id"] == "orch_123_ac_1"
    assert resume_handle.metadata["session_attempt_id"] == "orch_123_ac_1_attempt_2"
    event_store.replay.assert_awaited_once_with("execution", "orch_123_ac_1")
    assert result.runtime_handle is not None
    assert result.runtime_handle.native_session_id == "opencode-session-retry-attempt-2"
    assert result.runtime_handle.metadata == resume_handle.metadata
