"""Tests for staged result handling in ParallelACExecutor."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.types import MCPToolDefinition
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.orchestrator.coordinator import CoordinatorReview, FileConflict
from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph
from ouroboros.orchestrator.level_context import ACContextSummary, LevelContext
from ouroboros.orchestrator.parallel_executor import (
    ACExecutionOutcome,
    ACExecutionResult,
    ParallelACExecutor,
    StageExecutionOutcome,
)


def _make_seed(*acceptance_criteria: str) -> Seed:
    """Build a minimal seed for parallel executor tests."""
    return Seed(
        goal="Implement staged AC execution",
        constraints=(),
        acceptance_criteria=acceptance_criteria,
        ontology_schema=OntologySchema(
            name="ParallelExecution",
            description="Test schema",
        ),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )


def _make_executor() -> ParallelACExecutor:
    """Create an executor with mocked dependencies and muted event emitters."""
    executor = ParallelACExecutor(
        adapter=MagicMock(),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
    )
    executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
    executor._emit_workflow_progress = AsyncMock()
    executor._emit_level_started = AsyncMock()
    executor._emit_level_completed = AsyncMock()
    executor._emit_subtask_event = AsyncMock()
    return executor


def _make_replaying_event_store() -> tuple[AsyncMock, list[BaseEvent]]:
    """Create an async event-store mock that replays previously appended events."""
    event_store = AsyncMock()
    appended_events: list[BaseEvent] = []

    async def _append(event: BaseEvent) -> None:
        appended_events.append(event)

    async def _replay(aggregate_type: str, aggregate_id: str) -> list[BaseEvent]:
        return [
            event
            for event in appended_events
            if event.aggregate_type == aggregate_type and event.aggregate_id == aggregate_id
        ]

    event_store.append.side_effect = _append
    event_store.replay.side_effect = _replay
    return event_store, appended_events


class TestParallelACExecutor:
    """Tests for staged hybrid result handling."""

    @pytest.mark.asyncio
    async def test_atomic_ac_uses_ac_scoped_runtime_handle(self) -> None:
        """Atomic AC execution should seed a fresh AC-scoped runtime handle."""

        class _StubImplementationRuntime:
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
                    kind=resume_handle.kind
                    if resume_handle is not None
                    else "implementation_session",
                    native_session_id="opencode-session-1",
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

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_StubImplementationRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=2,
            ac_content="Implement AC 3",
            session_id="orch_123",
            tools=["Read", "Edit"],
            tool_catalog=(
                MCPToolDefinition(name="Read", description="Read a file from the workspace."),
                MCPToolDefinition(
                    name="Edit", description="Edit an existing file in the workspace."
                ),
            ),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        runtime_call = executor._adapter.calls[0]
        resume_handle = runtime_call["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.backend == "opencode"
        assert resume_handle.kind == "implementation_session"
        assert resume_handle.native_session_id is None
        assert resume_handle.cwd == "/tmp/project"
        assert resume_handle.approval_mode == "acceptEdits"
        assert resume_handle.metadata["ac_id"] == "orch_123_ac_2"
        assert resume_handle.metadata["scope"] == "ac"
        assert resume_handle.metadata["session_role"] == "implementation"
        assert resume_handle.metadata["retry_attempt"] == 0
        assert resume_handle.metadata["attempt_number"] == 1
        assert resume_handle.metadata["ac_index"] == 2
        assert [tool["name"] for tool in resume_handle.metadata["tool_catalog"]] == [
            "Read",
            "Edit",
        ]
        assert resume_handle.metadata["session_scope_id"] == "orch_123_ac_2"
        assert resume_handle.metadata["session_attempt_id"] == "orch_123_ac_2_attempt_1"
        assert (
            resume_handle.metadata["session_state_path"]
            == "execution.workflows.orch_123.acceptance_criteria.ac_2.implementation_session"
        )
        started_event = next(
            event for event in appended_events if event.type == "execution.session.started"
        )
        assert [tool["name"] for tool in started_event.data["tool_catalog"]] == ["Read", "Edit"]
        assert [
            tool["name"] for tool in started_event.data["runtime"]["metadata"]["tool_catalog"]
        ] == ["Read", "Edit"]
        assert started_event.data["session_attempt_id"] == "orch_123_ac_2_attempt_1"
        assert result.success is True
        assert result.session_id == "opencode-session-1"
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id == "opencode-session-1"

    @pytest.mark.asyncio
    async def test_remembered_runtime_handle_preserves_live_controls(self) -> None:
        """AC-scope rebinding should preserve live observe/terminate callbacks."""
        executor = _make_executor()
        control_calls = {"observe": 0, "terminate": 0}

        async def _observe(handle: RuntimeHandle) -> dict[str, object]:
            control_calls["observe"] += 1
            snapshot = handle.snapshot()
            snapshot["observed"] = True
            return snapshot

        async def _terminate(_handle: RuntimeHandle) -> bool:
            control_calls["terminate"] += 1
            return True

        rebound = executor._remember_ac_runtime_handle(
            0,
            RuntimeHandle(
                backend="opencode",
                kind="implementation_session",
                native_session_id="oc-session-1",
                metadata={"server_session_id": "server-1"},
            ).bind_controls(
                observe_callback=_observe,
                terminate_callback=_terminate,
            ),
            execution_context_id="orch_ctrl",
        )

        assert rebound is not None
        assert rebound.metadata["session_scope_id"] == "orch_ctrl_ac_0"
        assert rebound.can_terminate is True

        observed = await rebound.observe()
        assert observed["observed"] is True
        assert observed["control_session_id"] == "server-1"
        assert await rebound.terminate() is True
        assert control_calls == {"observe": 1, "terminate": 1}

    @pytest.mark.asyncio
    async def test_completed_ac_attempt_does_not_reuse_cached_runtime_handle(self) -> None:
        """Terminal AC attempts should drop the cached session before the next invocation."""

        class _StubResumeRuntime:
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
                native_session_id = f"opencode-session-{len(self.calls)}"
                bound_handle = RuntimeHandle(
                    backend=resume_handle.backend if resume_handle is not None else "opencode",
                    kind=resume_handle.kind
                    if resume_handle is not None
                    else "implementation_session",
                    native_session_id=native_session_id,
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

        runtime = _StubResumeRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
        )

        first_attempt = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Implement AC 2",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )
        resumed_attempt = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Implement AC 2",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )

        first_handle = runtime.calls[0]["resume_handle"]
        second_handle = runtime.calls[1]["resume_handle"]
        assert isinstance(first_handle, RuntimeHandle)
        assert isinstance(second_handle, RuntimeHandle)
        assert first_handle.native_session_id is None
        assert second_handle.native_session_id is None
        assert second_handle.metadata["session_scope_id"] == "orch_123_ac_1"
        assert second_handle.metadata["retry_attempt"] == 0
        assert second_handle.metadata["session_attempt_id"] == "orch_123_ac_1_attempt_1"
        assert first_attempt.runtime_handle is not None
        assert resumed_attempt.runtime_handle is not None
        assert first_attempt.runtime_handle.native_session_id == "opencode-session-1"
        assert resumed_attempt.runtime_handle.native_session_id == "opencode-session-2"
        assert executor._ac_runtime_handles == {}

    @pytest.mark.asyncio
    async def test_try_decompose_ac_times_out_and_falls_back_to_atomic(self) -> None:
        """A hung decomposition child should time out and fall back to atomic execution."""

        class _HangingRuntime:
            def __init__(self) -> None:
                self.cancelled = False

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                del prompt, tools, system_prompt, resume_handle, resume_session_id
                try:
                    await asyncio.Future()
                    if False:  # pragma: no cover
                        yield AgentMessage(type="assistant", content="")
                finally:
                    self.cancelled = True

        runtime = _HangingRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=True,
        )

        with patch(
            "ouroboros.orchestrator.parallel_executor.DECOMPOSITION_TIMEOUT_SECONDS",
            0.01,
        ):
            result = await executor._try_decompose_ac(
                ac_content="Implement the full OpenCode runtime adapter.",
                ac_index=0,
                seed_goal="Ship OpenCode support",
                tools=["Read", "Edit"],
                system_prompt="system",
            )

        assert result is None
        assert runtime.cancelled is True

    @pytest.mark.asyncio
    async def test_runtime_handle_cache_isolated_between_acceptance_criteria(self) -> None:
        """Completing one AC must not seed a different AC with its prior runtime session."""

        class _StubCrossACRuntime:
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
                    kind=resume_handle.kind
                    if resume_handle is not None
                    else "implementation_session",
                    native_session_id=f"opencode-session-{len(self.calls)}",
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

        runtime = _StubCrossACRuntime()
        event_store, _ = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        first_result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )
        second_result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Implement AC 2",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        first_handle = runtime.calls[0]["resume_handle"]
        second_handle = runtime.calls[1]["resume_handle"]
        assert isinstance(first_handle, RuntimeHandle)
        assert isinstance(second_handle, RuntimeHandle)
        assert first_handle.native_session_id is None
        assert second_handle.native_session_id is None
        assert first_handle.metadata["session_scope_id"] == "orch_123_ac_0"
        assert second_handle.metadata["session_scope_id"] == "orch_123_ac_1"
        assert first_handle.metadata["session_attempt_id"] == "orch_123_ac_0_attempt_1"
        assert second_handle.metadata["session_attempt_id"] == "orch_123_ac_1_attempt_1"
        assert second_handle.metadata["ac_index"] == 1
        assert first_result.runtime_handle is not None
        assert second_result.runtime_handle is not None
        assert first_result.runtime_handle.native_session_id == "opencode-session-1"
        assert second_result.runtime_handle.native_session_id == "opencode-session-2"
        assert executor._ac_runtime_handles == {}

    @pytest.mark.asyncio
    async def test_restarted_executor_rejects_persisted_runtime_handle_from_another_ac(
        self,
    ) -> None:
        """A persisted runtime handle must not resume when its metadata belongs to another AC."""

        class _StubFreshRuntime:
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
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        kind="implementation_session",
                        native_session_id="opencode-session-fresh",
                        cwd="/tmp/project",
                        approval_mode="acceptEdits",
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        current_state_path = (
            "execution.workflows.orch_123.acceptance_criteria.ac_1.implementation_session"
        )
        current_attempt_id = "orch_123_ac_1_attempt_1"
        foreign_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-foreign",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "ac_id": "orch_123_ac_0",
                "scope": "ac",
                "session_role": "implementation",
                "retry_attempt": 0,
                "attempt_number": 1,
                "ac_index": 0,
                "session_scope_id": "orch_123_ac_0",
                "session_attempt_id": "orch_123_ac_0_attempt_1",
                "session_state_path": (
                    "execution.workflows.orch_123.acceptance_criteria.ac_0.implementation_session"
                ),
                "server_session_id": "server-foreign",
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
                        "attempt_number": 1,
                        "session_scope_id": "orch_123_ac_1",
                        "session_attempt_id": current_attempt_id,
                        "session_state_path": current_state_path,
                        "runtime": foreign_handle.to_dict(),
                    },
                )
            ]
        )
        event_store.append = AsyncMock()
        runtime = _StubFreshRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Keep AC sessions isolated",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )

        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.native_session_id is None
        assert resume_handle.metadata["ac_index"] == 1
        assert resume_handle.metadata["session_scope_id"] == "orch_123_ac_1"
        assert resume_handle.metadata["session_attempt_id"] == current_attempt_id
        assert "server_session_id" not in resume_handle.metadata
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id == "opencode-session-fresh"

    @pytest.mark.asyncio
    async def test_cached_runtime_handle_from_another_ac_is_not_reused(self) -> None:
        """An in-memory runtime-handle cache entry must not leak a foreign AC session."""

        class _StubFreshRuntime:
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
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        kind="implementation_session",
                        native_session_id="opencode-session-current",
                        cwd="/tmp/project",
                        approval_mode="acceptEdits",
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        runtime = _StubFreshRuntime()
        event_store = AsyncMock()
        event_store.replay = AsyncMock(return_value=[])
        event_store.append = AsyncMock()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity = executor._resolve_ac_runtime_identity(
            1,
            execution_context_id="orch_123",
            retry_attempt=0,
        )
        executor._ac_runtime_handles[runtime_identity.cache_key] = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-foreign",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "ac_id": "orch_123_ac_0",
                "scope": "ac",
                "session_role": "implementation",
                "retry_attempt": 0,
                "attempt_number": 1,
                "ac_index": 0,
                "session_scope_id": "orch_123_ac_0",
                "session_attempt_id": "orch_123_ac_0_attempt_1",
                "session_state_path": (
                    "execution.workflows.orch_123.acceptance_criteria.ac_0.implementation_session"
                ),
                "server_session_id": "server-foreign",
            },
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Keep AC sessions isolated",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )

        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.native_session_id is None
        assert resume_handle.metadata["ac_index"] == 1
        assert resume_handle.metadata["session_scope_id"] == "orch_123_ac_1"
        assert resume_handle.metadata["session_attempt_id"] == "orch_123_ac_1_attempt_1"
        assert "server_session_id" not in resume_handle.metadata
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id == "opencode-session-current"

    @pytest.mark.asyncio
    async def test_atomic_ac_persists_reconnectable_handle_before_native_session_id(self) -> None:
        """OpenCode AC lifecycle should persist once the runtime exposes a resumable handle."""

        class _StubReconnectableRuntime:
            def __init__(self) -> None:
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
                assert isinstance(resume_handle, RuntimeHandle)
                reconnectable_handle = RuntimeHandle(
                    backend=resume_handle.backend,
                    kind=resume_handle.kind,
                    conversation_id="conversation-9",
                    previous_response_id="response-9",
                    transcript_path="/tmp/opencode-runtime.jsonl",
                    cwd=resume_handle.cwd,
                    approval_mode=resume_handle.approval_mode,
                    updated_at="2026-03-13T09:00:00+00:00",
                    metadata={
                        **dict(resume_handle.metadata),
                        "server_session_id": "server-42",
                        "runtime_event_type": "session.ready",
                    },
                )
                yield AgentMessage(
                    type="system",
                    content="OpenCode session ready for reconnect.",
                    data={"server_session_id": "server-42"},
                    resume_handle=reconnectable_handle,
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=reconnectable_handle,
                )

        event_store = AsyncMock()
        event_store.replay = AsyncMock(return_value=[])
        event_store.append = AsyncMock()
        executor = ParallelACExecutor(
            adapter=_StubReconnectableRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Persist reconnectable OpenCode implementation handles",
            session_id="orch_123",
            tools=["Read"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        appended_events = [call.args[0] for call in event_store.append.await_args_list]
        started_event = next(
            event for event in appended_events if event.type == "execution.session.started"
        )
        completed_event = next(
            event for event in appended_events if event.type == "execution.session.completed"
        )

        assert result.success is True
        assert result.session_id is None
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id is None
        assert result.runtime_handle.conversation_id == "conversation-9"
        assert result.runtime_handle.previous_response_id == "response-9"
        assert result.runtime_handle.transcript_path == "/tmp/opencode-runtime.jsonl"
        assert result.runtime_handle.metadata["server_session_id"] == "server-42"
        assert started_event.data["session_id"] == "server-42"
        assert started_event.data["server_session_id"] == "server-42"
        assert started_event.data["runtime"]["native_session_id"] is None
        assert started_event.data["runtime"]["metadata"]["server_session_id"] == "server-42"
        assert "conversation_id" not in started_event.data["runtime"]
        assert "previous_response_id" not in started_event.data["runtime"]
        assert "transcript_path" not in started_event.data["runtime"]
        assert "updated_at" not in started_event.data["runtime"]
        assert completed_event.data["session_id"] == "server-42"

    @pytest.mark.asyncio
    async def test_restarted_executor_loads_persisted_runtime_handle_for_same_attempt(self) -> None:
        """A fresh executor should rehydrate the same-attempt runtime handle from events."""

        class _StubPersistedResumeRuntime:
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
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=resume_handle,
                )

        persisted_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-9",
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
                )
            ]
        )
        event_store.append = AsyncMock()
        runtime = _StubPersistedResumeRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Resume the interrupted AC implementation session",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )

        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.native_session_id == "opencode-session-9"
        assert resume_handle.metadata["server_session_id"] == "server-99"
        event_store.replay.assert_awaited_once_with("execution", "orch_123_ac_1")
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id == resume_handle.native_session_id
        assert result.runtime_handle.metadata == resume_handle.metadata

    @pytest.mark.asyncio
    async def test_restarted_executor_ignores_invalid_persisted_runtime_handle_for_same_attempt(
        self,
    ) -> None:
        """Malformed persisted runtime payloads should be skipped in favor of a fresh handle."""

        class _StubInvalidPersistedHandleRuntime:
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
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=resume_handle,
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
                        "runtime": {
                            "kind": "implementation_session",
                            "cwd": "/tmp/project",
                            "approval_mode": "acceptEdits",
                            "metadata": {
                                "scope": "ac",
                                "session_role": "implementation",
                                "retry_attempt": 0,
                                "ac_index": 1,
                                "session_scope_id": "orch_123_ac_1",
                                "session_state_path": (
                                    "execution.workflows.orch_123.acceptance_criteria."
                                    "ac_1.implementation_session"
                                ),
                                "server_session_id": "server-invalid",
                            },
                        },
                    },
                )
            ]
        )
        event_store.append = AsyncMock()
        runtime = _StubInvalidPersistedHandleRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Recover from malformed persisted runtime state",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )

        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.backend == "opencode"
        assert resume_handle.native_session_id is None
        assert resume_handle.metadata["session_scope_id"] == "orch_123_ac_1"
        assert resume_handle.metadata["session_role"] == "implementation"
        assert "server_session_id" not in resume_handle.metadata
        event_store.replay.assert_awaited_once_with("execution", "orch_123_ac_1")
        # Compare handles ignoring updated_at (timestamp set at creation time
        # may differ by microseconds from the one stored in the result).
        result_handle = replace(result.runtime_handle, updated_at=None)
        expected_handle = replace(resume_handle, updated_at=None)
        assert result_handle == expected_handle

    @pytest.mark.asyncio
    async def test_restarted_executor_prefers_latest_resumed_runtime_handle_for_same_attempt(
        self,
    ) -> None:
        """Resume should hydrate from the newest active lifecycle event for the same attempt."""

        class _StubResumedHandleRuntime:
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
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=resume_handle,
                )

        started_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-started",
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
                "server_session_id": "server-started",
            },
        )
        resumed_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-resumed",
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
                "server_session_id": "server-resumed",
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
                        "runtime": started_handle.to_dict(),
                    },
                ),
                BaseEvent(
                    type="execution.session.resumed",
                    aggregate_type="execution",
                    aggregate_id="orch_123_ac_1",
                    data={
                        "retry_attempt": 0,
                        "session_state_path": (
                            "execution.workflows.orch_123.acceptance_criteria."
                            "ac_1.implementation_session"
                        ),
                        "runtime": resumed_handle.to_dict(),
                    },
                ),
            ]
        )
        event_store.append = AsyncMock()
        runtime = _StubResumedHandleRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Resume the latest persisted implementation session",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )

        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.native_session_id == "opencode-session-resumed"
        assert resume_handle.metadata["server_session_id"] == "server-resumed"
        event_store.replay.assert_awaited_once_with("execution", "orch_123_ac_1")
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id == resume_handle.native_session_id
        assert result.runtime_handle.metadata == resume_handle.metadata

    @pytest.mark.asyncio
    async def test_restarted_executor_does_not_cross_resume_into_another_execution_context(
        self,
    ) -> None:
        """Persisted AC handles must stay bound to the parent execution/session context."""

        class _StubFreshRuntime:
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
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        kind="implementation_session",
                        native_session_id="opencode-session-fresh",
                        cwd="/tmp/project",
                        approval_mode="acceptEdits",
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        event_store = AsyncMock()
        event_store.replay = AsyncMock(return_value=[])
        event_store.append = AsyncMock()
        runtime = _StubFreshRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Start a new implementation session in a different execution context",
            session_id="orch_new",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.native_session_id is None
        assert resume_handle.metadata["session_scope_id"] == "orch_new_ac_1"
        assert (
            resume_handle.metadata["session_state_path"]
            == "execution.workflows.orch_new.acceptance_criteria.ac_1.implementation_session"
        )
        event_store.replay.assert_awaited_once_with("execution", "orch_new_ac_1")
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id == "opencode-session-fresh"

    @pytest.mark.asyncio
    async def test_restarted_executor_ignores_terminal_runtime_handle_for_same_attempt(
        self,
    ) -> None:
        """Persisted terminal events should not revive a completed AC attempt."""

        class _StubTerminalAwareRuntime:
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
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        kind="implementation_session",
                        native_session_id="opencode-session-fresh",
                        cwd="/tmp/project",
                        approval_mode="acceptEdits",
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        persisted_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-terminal",
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
                    type="execution.session.completed",
                    aggregate_type="execution",
                    aggregate_id="orch_123_ac_1",
                    data={
                        "retry_attempt": 0,
                        "session_state_path": (
                            "execution.workflows.orch_123.acceptance_criteria."
                            "ac_1.implementation_session"
                        ),
                        "runtime": persisted_handle.to_dict(),
                        "success": True,
                    },
                ),
            ]
        )
        event_store.append = AsyncMock()
        runtime = _StubTerminalAwareRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Start a fresh session after terminal completion",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )

        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.native_session_id is None
        assert resume_handle.metadata["session_scope_id"] == "orch_123_ac_1"
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id == "opencode-session-fresh"
        assert executor._ac_runtime_handles == {}

    @pytest.mark.asyncio
    async def test_retry_reopens_failed_ac_with_same_scope_and_new_attempt_audit(self) -> None:
        """Retry attempts should start a fresh session while emitting a new attempt identity."""

        class _StubRetryRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"
                self._attempt = 0

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
                native_session_id = f"opencode-session-{self._attempt}"
                is_error = self._attempt == 0
                self._attempt += 1
                bound_handle = RuntimeHandle(
                    backend=resume_handle.backend if resume_handle is not None else "opencode",
                    kind=resume_handle.kind
                    if resume_handle is not None
                    else "implementation_session",
                    native_session_id=native_session_id,
                    cwd=resume_handle.cwd if resume_handle is not None else "/tmp/project",
                    approval_mode=(
                        resume_handle.approval_mode if resume_handle is not None else "acceptEdits"
                    ),
                    metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                )
                yield AgentMessage(
                    type="result",
                    content="retry me" if is_error else "[TASK_COMPLETE]",
                    data={"subtype": "error" if is_error else "success"},
                    resume_handle=bound_handle,
                )

        runtime = _StubRetryRuntime()
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        first_attempt = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )
        retry_attempt = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=1,
        )

        first_handle = runtime.calls[0]["resume_handle"]
        second_handle = runtime.calls[1]["resume_handle"]
        assert isinstance(first_handle, RuntimeHandle)
        assert isinstance(second_handle, RuntimeHandle)
        assert first_handle.native_session_id is None
        assert second_handle.native_session_id is None
        assert first_handle.metadata["session_scope_id"] == "orch_123_ac_0"
        assert second_handle.metadata["session_scope_id"] == "orch_123_ac_0"
        assert first_handle.metadata["session_attempt_id"] == "orch_123_ac_0_attempt_1"
        assert second_handle.metadata["session_attempt_id"] == "orch_123_ac_0_attempt_2"
        assert (
            first_handle.metadata["session_state_path"]
            == second_handle.metadata["session_state_path"]
            == "execution.workflows.orch_123.acceptance_criteria.ac_0.implementation_session"
        )
        assert first_handle.metadata["retry_attempt"] == 0
        assert second_handle.metadata["retry_attempt"] == 1
        assert first_attempt.ac_index == retry_attempt.ac_index == 0
        assert first_attempt.success is False
        assert retry_attempt.success is True
        assert first_attempt.session_id == "opencode-session-0"
        assert retry_attempt.session_id == "opencode-session-1"
        assert first_attempt.retry_attempt == 0
        assert retry_attempt.retry_attempt == 1
        assert first_attempt.runtime_handle is not None
        assert retry_attempt.runtime_handle is not None
        assert first_attempt.runtime_handle.native_session_id == "opencode-session-0"
        assert retry_attempt.runtime_handle.native_session_id == "opencode-session-1"
        lifecycle_events = [
            event
            for event in appended_events
            if event.type
            in {
                "execution.session.started",
                "execution.session.failed",
                "execution.session.completed",
            }
        ]
        assert [event.type for event in lifecycle_events] == [
            "execution.session.started",
            "execution.session.failed",
            "execution.session.started",
            "execution.session.completed",
        ]
        assert [event.data["session_attempt_id"] for event in lifecycle_events] == [
            "orch_123_ac_0_attempt_1",
            "orch_123_ac_0_attempt_1",
            "orch_123_ac_0_attempt_2",
            "orch_123_ac_0_attempt_2",
        ]
        assert executor._ac_runtime_handles == {}

    @pytest.mark.asyncio
    async def test_retry_executes_on_reconciled_workspace_context(self) -> None:
        """Retry prompts should include prior reconciled workspace context."""

        class _StubContextRuntime:
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
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        kind="implementation_session",
                        native_session_id="opencode-session-retry",
                        cwd="/tmp/project",
                        approval_mode="acceptEdits",
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        runtime = _StubContextRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
        )
        reconciled_context = LevelContext(
            level_number=1,
            completed_acs=(
                ACContextSummary(
                    ac_index=1,
                    ac_content="Reconcile the shared auth helpers",
                    success=True,
                    files_modified=("src/auth.py",),
                    key_output="Shared auth helpers are reconciled",
                ),
            ),
            coordinator_review=CoordinatorReview(
                level_number=1,
                review_summary="Merged the auth helper edits into the shared workspace",
                fixes_applied=("Merged src/auth.py conflict",),
                warnings_for_next_level=("Continue from the reconciled src/auth.py state",),
            ),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Finish wiring the auth retry flow",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            level_contexts=[reconciled_context],
            retry_attempt=1,
        )

        prompt = runtime.calls[0]["prompt"]
        assert isinstance(prompt, str)
        assert "## Previous Work Context" in prompt
        assert "Shared auth helpers are reconciled" in prompt
        assert "## Coordinator Review (Level 1)" in prompt
        assert "Merged the auth helper edits into the shared workspace" in prompt
        assert "Continue from the reconciled src/auth.py state" in prompt
        assert "## Retry Context" in prompt
        assert "retry attempt 1" in prompt
        assert "current shared workspace state" in prompt
        assert result.success is True
        assert result.retry_attempt == 1
        assert result.session_id == "opencode-session-retry"

    @pytest.mark.asyncio
    async def test_aggregates_mixed_stage_outcomes(self) -> None:
        """A later stage may be partially executable while blocked dependents are withheld."""
        seed = _make_seed(
            "Build the shared model",
            "Implement the fragile integration",
            "Add endpoint on top of the model",
            "Wire reporting to the fragile integration",
        )
        graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),
                ACNode(index=1, content=seed.acceptance_criteria[1], depends_on=()),
                ACNode(index=2, content=seed.acceptance_criteria[2], depends_on=(0,)),
                ACNode(index=3, content=seed.acceptance_criteria[3], depends_on=(1,)),
            ),
            execution_levels=((0, 1), (2, 3)),
        )
        executor = _make_executor()

        async def fake_execute_single_ac(**kwargs: object) -> ACExecutionResult:
            ac_index = kwargs["ac_index"]
            ac_content = kwargs["ac_content"]
            if ac_index == 0:
                return ACExecutionResult(
                    ac_index=0,
                    ac_content=str(ac_content),
                    success=True,
                    final_message="Shared model complete",
                )
            if ac_index == 1:
                return ACExecutionResult(
                    ac_index=1,
                    ac_content=str(ac_content),
                    success=False,
                    error="Integration step failed",
                )
            if ac_index == 2:
                return ACExecutionResult(
                    ac_index=2,
                    ac_content=str(ac_content),
                    success=True,
                    final_message="Endpoint complete",
                )
            msg = f"AC {ac_index} should have been blocked before execution"
            raise AssertionError(msg)

        executor._execute_single_ac = fake_execute_single_ac  # type: ignore[method-assign]

        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=graph.to_execution_plan(),
            session_id="sess_stage_mixed",
            execution_id="exec_stage_mixed",
            tools=["Read", "Edit"],
            system_prompt="test",
        )

        assert result.success_count == 2
        assert result.failure_count == 1
        assert result.blocked_count == 1
        assert result.invalid_count == 0
        assert result.skipped_count == 1
        assert [r.outcome for r in result.results] == [
            ACExecutionOutcome.SUCCEEDED,
            ACExecutionOutcome.FAILED,
            ACExecutionOutcome.SUCCEEDED,
            ACExecutionOutcome.BLOCKED,
        ]

        assert len(result.stages) == 2
        assert result.stages[0].outcome == StageExecutionOutcome.PARTIAL
        assert result.stages[0].started is True
        assert result.stages[1].outcome == StageExecutionOutcome.PARTIAL
        assert result.stages[1].success_count == 1
        assert result.stages[1].blocked_count == 1
        executor._emit_level_started.assert_awaited()

    @pytest.mark.asyncio
    async def test_fully_blocked_stage_does_not_start(self) -> None:
        """If all ACs in a later stage depend on a failed AC, that stage is blocked but recorded."""
        seed = _make_seed(
            "Create the foundational abstraction",
            "Build the first dependent flow",
            "Build the second dependent flow",
        )
        graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),
                ACNode(index=1, content=seed.acceptance_criteria[1], depends_on=(0,)),
                ACNode(index=2, content=seed.acceptance_criteria[2], depends_on=(0,)),
            ),
            execution_levels=((0,), (1, 2)),
        )
        executor = _make_executor()
        executed_indices: list[int] = []

        async def fake_execute_single_ac(**kwargs: object) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            executed_indices.append(ac_index)
            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=str(kwargs["ac_content"]),
                success=False,
                error="Foundation failed",
            )

        executor._execute_single_ac = fake_execute_single_ac  # type: ignore[method-assign]

        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=graph.to_execution_plan(),
            session_id="sess_stage_blocked",
            execution_id="exec_stage_blocked",
            tools=["Read", "Edit"],
            system_prompt="test",
        )

        assert executed_indices == [0]
        assert result.success_count == 0
        assert result.failure_count == 1
        assert result.blocked_count == 2
        assert result.skipped_count == 2
        assert len(result.stages) == 2
        assert result.stages[0].outcome == StageExecutionOutcome.FAILED
        assert result.stages[1].started is False
        assert result.stages[1].outcome == StageExecutionOutcome.BLOCKED
        assert result.stages[1].blocked_count == 2

        assert executor._emit_level_started.await_count == 1
        assert executor._emit_level_completed.await_count == 2
        blocked_completion = executor._emit_level_completed.await_args_list[1].kwargs
        assert blocked_completion["started"] is False
        assert blocked_completion["blocked_count"] == 2
        assert blocked_completion["outcome"] == StageExecutionOutcome.BLOCKED.value

    @pytest.mark.asyncio
    async def test_runs_serial_stages_in_order(self) -> None:
        """The executor should not dispatch the next stage until the current one finishes."""
        seed = _make_seed("Implement parser", "Implement formatter", "Wire runner")
        graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),
                ACNode(index=1, content=seed.acceptance_criteria[1], depends_on=()),
                ACNode(index=2, content=seed.acceptance_criteria[2], depends_on=(0, 1)),
            ),
            execution_levels=((0, 1), (2,)),
        )
        executor = _make_executor()

        stage_one_started: set[int] = set()
        stage_one_completed: list[int] = []
        release_stage_one = asyncio.Event()
        all_stage_one_started = asyncio.Event()
        stage_two_started = asyncio.Event()
        stage_two_started_after: frozenset[int] | None = None

        async def fake_execute_single_ac(**kwargs: object) -> ACExecutionResult:
            nonlocal stage_two_started_after
            ac_index = int(kwargs["ac_index"])
            ac_content = str(kwargs["ac_content"])

            if ac_index in (0, 1):
                stage_one_started.add(ac_index)
                if stage_one_started == {0, 1}:
                    all_stage_one_started.set()
                await release_stage_one.wait()
                stage_one_completed.append(ac_index)
            elif ac_index == 2:
                stage_two_started_after = frozenset(stage_one_completed)
                stage_two_started.set()

            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=ac_content,
                success=True,
                final_message=f"AC {ac_index} complete",
            )

        with patch.object(executor, "_execute_single_ac", side_effect=fake_execute_single_ac):
            execution_task = asyncio.create_task(
                executor.execute_parallel(
                    seed=seed,
                    execution_plan=graph.to_execution_plan(),
                    session_id="sess_stage_order",
                    execution_id="exec_stage_order",
                    tools=["Read"],
                    system_prompt="test",
                )
            )

            await asyncio.wait_for(all_stage_one_started.wait(), timeout=1)
            assert stage_two_started.is_set() is False

            release_stage_one.set()
            result = await asyncio.wait_for(execution_task, timeout=1)

        assert result.all_succeeded is True
        assert result.success_count == 3
        assert stage_two_started.is_set() is True
        assert stage_two_started_after == frozenset({0, 1})

    @pytest.mark.asyncio
    async def test_consumes_stage_batches_sequentially_within_stage_boundaries(self) -> None:
        """Batch-aware stages should run batch-by-batch without crossing stage boundaries."""
        seed = _make_seed(
            "Build parser core",
            "Build formatter core",
            "Assemble shared CLI",
            "Wire end-to-end runner",
        )
        executor = _make_executor()

        execution_plan = SimpleNamespace(
            stages=(
                SimpleNamespace(
                    index=0,
                    ac_indices=(),
                    batches=(
                        SimpleNamespace(ac_indices=(0, 1)),
                        SimpleNamespace(ac_indices=(2,)),
                    ),
                ),
                SimpleNamespace(
                    index=1,
                    ac_indices=(),
                    batches=(SimpleNamespace(ac_indices=(3,)),),
                ),
            ),
            total_stages=2,
            execution_levels=((0, 1, 2), (3,)),
            get_dependencies=lambda ac_index: {3: (2,)}.get(ac_index, ()),
        )

        first_batch_started: set[int] = set()
        release_first_batch = asyncio.Event()
        all_first_batch_started = asyncio.Event()
        second_batch_started = asyncio.Event()
        stage_two_started = asyncio.Event()

        async def fake_execute_single_ac(**kwargs: object) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            ac_content = str(kwargs["ac_content"])

            if ac_index in (0, 1):
                first_batch_started.add(ac_index)
                if first_batch_started == {0, 1}:
                    all_first_batch_started.set()
                await release_first_batch.wait()
            elif ac_index == 2:
                second_batch_started.set()
            elif ac_index == 3:
                stage_two_started.set()

            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=ac_content,
                success=True,
                final_message=f"AC {ac_index} complete",
            )

        with patch.object(executor, "_execute_single_ac", side_effect=fake_execute_single_ac):
            execution_task = asyncio.create_task(
                executor.execute_parallel(
                    seed=seed,
                    execution_plan=execution_plan,
                    session_id="sess_stage_batches",
                    execution_id="exec_stage_batches",
                    tools=["Read"],
                    system_prompt="test",
                )
            )

            await asyncio.wait_for(all_first_batch_started.wait(), timeout=1)
            assert second_batch_started.is_set() is False
            assert stage_two_started.is_set() is False

            release_first_batch.set()
            result = await asyncio.wait_for(execution_task, timeout=1)

        assert result.all_succeeded is True
        assert result.success_count == 4
        assert second_batch_started.is_set() is True
        assert stage_two_started.is_set() is True

    @pytest.mark.asyncio
    async def test_aggregates_stage_batch_results_with_failures_and_blocked_dependents(
        self,
    ) -> None:
        """Stage aggregation should include all batch outcomes before moving to the next stage."""
        seed = _make_seed(
            "Build parser core",
            "Build formatter core",
            "Wire parser command",
            "Wire formatter command",
        )
        executor = _make_executor()

        execution_plan = SimpleNamespace(
            stages=(
                SimpleNamespace(
                    index=0,
                    ac_indices=(),
                    batches=(
                        SimpleNamespace(ac_indices=(0,)),
                        SimpleNamespace(ac_indices=(1,)),
                    ),
                ),
                SimpleNamespace(
                    index=1,
                    ac_indices=(),
                    batches=(SimpleNamespace(ac_indices=(2, 3)),),
                ),
            ),
            total_stages=2,
            execution_levels=((0, 1), (2, 3)),
            get_dependencies=lambda ac_index: {2: (0,), 3: (1,)}.get(ac_index, ()),
        )

        async def fake_execute_single_ac(**kwargs: object) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            ac_content = str(kwargs["ac_content"])
            if ac_index == 0:
                return ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=ac_content,
                    success=False,
                    error="Parser core failed",
                )

            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=ac_content,
                success=True,
                final_message=f"AC {ac_index} complete",
            )

        executor._execute_single_ac = fake_execute_single_ac  # type: ignore[method-assign]

        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=execution_plan,
            session_id="sess_stage_batch_outcomes",
            execution_id="exec_stage_batch_outcomes",
            tools=["Read"],
            system_prompt="test",
        )

        assert [r.outcome for r in result.results] == [
            ACExecutionOutcome.FAILED,
            ACExecutionOutcome.SUCCEEDED,
            ACExecutionOutcome.BLOCKED,
            ACExecutionOutcome.SUCCEEDED,
        ]
        assert result.success_count == 2
        assert result.failure_count == 1
        assert result.blocked_count == 1
        assert result.invalid_count == 0
        assert len(result.stages) == 2
        assert result.stages[0].ac_indices == (0, 1)
        assert result.stages[0].outcome == StageExecutionOutcome.PARTIAL
        assert result.stages[1].ac_indices == (2, 3)
        assert result.stages[1].outcome == StageExecutionOutcome.PARTIAL

    @pytest.mark.asyncio
    async def test_records_coordinator_results_at_level_scope_without_ac_attribution(self) -> None:
        """Coordinator reconciliation should persist level-scoped events and artifacts only."""
        seed = _make_seed(
            "Update the shared module imports",
            "Wire the shared module into the runtime",
        )
        graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),
                ACNode(index=1, content=seed.acceptance_criteria[1], depends_on=()),
            ),
            execution_levels=((0, 1),),
        )
        executor = _make_executor()
        executor._coordinator.detect_file_conflicts = MagicMock(
            return_value=[FileConflict(file_path="src/shared.py", ac_indices=(0, 1))]
        )
        executor._coordinator.run_review = AsyncMock(
            return_value=CoordinatorReview(
                level_number=1,
                conflicts_detected=(
                    FileConflict(
                        file_path="src/shared.py",
                        ac_indices=(0, 1),
                        resolved=True,
                        resolution_description="Merged by coordinator",
                    ),
                ),
                review_summary="Resolved shared.py conflict",
                fixes_applied=("Merged overlapping import edits",),
                warnings_for_next_level=("Verify shared.py integration paths",),
                duration_seconds=1.5,
                session_id="coord-session-1",
                session_scope_id="level_1_coordinator",
                session_state_path=".ouroboros/execution_runtime/level_1_coordinator/session.json",
                final_output=(
                    '{"review_summary":"Resolved shared.py conflict",'
                    '"fixes_applied":["Merged overlapping import edits"],'
                    '"warnings_for_next_level":["Verify shared.py integration paths"],'
                    '"conflicts_resolved":["src/shared.py"]}'
                ),
                messages=(
                    AgentMessage(
                        type="assistant",
                        content="Inspecting shared file",
                        tool_name="Read",
                        data={"tool_input": {"file_path": "src/shared.py"}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content="Reconciling overlap",
                        data={"thinking": "Merge the import changes without changing behavior."},
                    ),
                ),
            )
        )

        async def fake_execute_single_ac(**kwargs: object) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=str(kwargs["ac_content"]),
                success=True,
                messages=(
                    AgentMessage(
                        type="assistant",
                        content="Editing shared module",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": "src/shared.py"}},
                    ),
                ),
                final_message=f"AC {ac_index + 1} complete",
            )

        executor._execute_single_ac = fake_execute_single_ac  # type: ignore[method-assign]

        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=graph.to_execution_plan(),
            session_id="sess_coord_scope",
            execution_id="exec_coord_scope",
            tools=["Read", "Edit"],
            system_prompt="test",
        )

        appended_events = [call.args[0] for call in executor._event_store.append.await_args_list]

        assert result.success_count == 2
        assert len(result.stages) == 1
        assert result.stages[0].coordinator_review is not None
        assert result.stages[0].coordinator_review.review_summary == "Resolved shared.py conflict"
        assert result.stages[0].coordinator_review.artifact_scope == "level"
        assert result.stages[0].coordinator_review.artifact_owner == "coordinator"
        assert result.stages[0].coordinator_review.artifact_owner_id == "level_1_coordinator"

        assert [event.type for event in appended_events] == [
            "execution.coordinator.started",
            "execution.coordinator.tool.started",
            "execution.coordinator.thinking",
            "execution.coordinator.completed",
        ]
        for event in appended_events:
            assert event.aggregate_id == "exec_coord_scope:l0:coord"
            assert event.data["scope"] == "level"
            assert event.data["session_role"] == "coordinator"
            assert event.data["level_number"] == 1
            assert event.data["stage_index"] == 0
            assert "ac_id" not in event.data
            assert "ac_index" not in event.data
            assert "acceptance_criterion" not in event.data

        assert appended_events[-1].data["artifact_type"] == "coordinator_review"
        assert appended_events[-1].data["artifact_scope"] == "level"
        assert appended_events[-1].data["artifact_owner"] == "coordinator"
        assert appended_events[-1].data["artifact_owner_id"] == "level_1_coordinator"
        assert (
            appended_events[-1].data["artifact"]
            == '{"review_summary":"Resolved shared.py conflict","fixes_applied":["Merged overlapping import edits"],"warnings_for_next_level":["Verify shared.py integration paths"],"conflicts_resolved":["src/shared.py"]}'
        )

    @pytest.mark.asyncio
    async def test_returns_reconciled_level_contexts_for_retry_handoff(self) -> None:
        """Completed stage contexts should be returned for retry workspace handoff."""
        seed = _make_seed(
            "Land the shared runtime update",
            "Repair the follow-up integration",
        )
        graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),
                ACNode(index=1, content=seed.acceptance_criteria[1], depends_on=()),
            ),
            execution_levels=((0, 1),),
        )
        executor = _make_executor()
        executor._coordinator.detect_file_conflicts = MagicMock(
            return_value=[FileConflict(file_path="src/shared.py", ac_indices=(0, 1))]
        )
        executor._coordinator.run_review = AsyncMock(
            return_value=CoordinatorReview(
                level_number=1,
                review_summary="Reconciled shared workspace",
                fixes_applied=("Merged shared.py edits",),
                warnings_for_next_level=("Retry AC 2 against the merged shared.py state",),
            )
        )

        async def fake_execute_single_ac(**kwargs: object) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            ac_content = str(kwargs["ac_content"])
            if ac_index == 0:
                return ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=ac_content,
                    success=True,
                    messages=(
                        AgentMessage(
                            type="assistant",
                            content="Updated shared module",
                            tool_name="Edit",
                            data={"tool_input": {"file_path": "src/shared.py"}},
                        ),
                    ),
                    final_message="Shared runtime landed",
                )
            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=ac_content,
                success=False,
                messages=(
                    AgentMessage(
                        type="assistant",
                        content="Need to revisit integration",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": "src/shared.py"}},
                    ),
                ),
                error="Integration failed",
            )

        executor._execute_single_ac = fake_execute_single_ac  # type: ignore[method-assign]

        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=graph.to_execution_plan(),
            session_id="sess_retry_handoff",
            execution_id="exec_retry_handoff",
            tools=["Read", "Edit"],
            system_prompt="test",
        )

        assert len(result.reconciled_level_contexts) == 1
        handoff = result.reconciled_level_contexts[0]
        assert handoff.level_number == 1
        assert handoff.coordinator_review is not None
        assert handoff.coordinator_review.review_summary == "Reconciled shared workspace"
        assert handoff.completed_acs[0].success is True

    @pytest.mark.asyncio
    async def test_reopened_execution_uses_reconciled_workspace_handoff(self) -> None:
        """Retries should seed reopened ACs with the latest reconciled workspace context."""
        seed = _make_seed("Retry the failed shared runtime integration")
        graph = DependencyGraph(
            nodes=(ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),),
            execution_levels=((0,),),
        )
        executor = _make_executor()
        handoff = LevelContext(
            level_number=1,
            completed_acs=(),
            coordinator_review=CoordinatorReview(
                level_number=1,
                review_summary="Workspace was reconciled after the previous failure",
                fixes_applied=("Merged shared.py before retry",),
                warnings_for_next_level=(
                    "Build on the reconciled shared.py, not the earlier draft",
                ),
            ),
        )
        captured_contexts: list[LevelContext] = []

        async def fake_execute_single_ac(**kwargs: object) -> ACExecutionResult:
            captured_contexts.extend(kwargs["level_contexts"])
            return ACExecutionResult(
                ac_index=int(kwargs["ac_index"]),
                ac_content=str(kwargs["ac_content"]),
                success=True,
                final_message="Retried successfully",
            )

        executor._execute_single_ac = fake_execute_single_ac  # type: ignore[method-assign]

        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=graph.to_execution_plan(),
            session_id="sess_retry_reopen",
            execution_id="exec_retry_reopen",
            tools=["Read", "Edit"],
            system_prompt="test",
            reconciled_level_contexts=[handoff],
        )

        assert result.success_count == 1
        assert captured_contexts == [handoff]

    @pytest.mark.asyncio
    async def test_atomic_ac_events_include_retry_attempt_metadata(self) -> None:
        """AC-scoped runtime events should preserve AC id while recording retry attempts."""

        class StubRuntime:
            _runtime_handle_backend = "opencode"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return "/tmp/project"

            @property
            def permission_mode(self) -> str | None:
                return "acceptEdits"

            async def execute_task(self, **kwargs: object):
                resume_handle = kwargs["resume_handle"]
                assert isinstance(resume_handle, RuntimeHandle)
                assert resume_handle.metadata["retry_attempt"] == 2
                yield AgentMessage(
                    type="assistant",
                    content="Retrying the implementation",
                    tool_name="Edit",
                    data={
                        "tool_input": {"file_path": "src/app.py"},
                        "thinking": "Reopen the same AC with a fresh runtime session.",
                    },
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                )

        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=StubRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=3,
            ac_content="Fix the failing AC",
            session_id="sess_retry",
            tools=["Edit"],
            system_prompt="test",
            seed_goal="Ship the fix",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=2,
        )

        appended_events = [call.args[0] for call in event_store.append.await_args_list]

        assert result.success is True
        assert result.retry_attempt == 2
        assert result.attempt_number == 3
        tool_event = next(
            event for event in appended_events if event.type == "execution.tool.started"
        )
        thinking_event = next(
            event for event in appended_events if event.type == "execution.agent.thinking"
        )
        completed_event = next(
            event for event in appended_events if event.type == "execution.session.completed"
        )

        assert tool_event.aggregate_id == "sess_retry_ac_3"
        assert tool_event.data["ac_id"] == "sess_retry_ac_3"
        assert tool_event.data["retry_attempt"] == 2
        assert tool_event.data["attempt_number"] == 3
        assert tool_event.data["session_attempt_id"] == "sess_retry_ac_3_attempt_3"
        assert thinking_event.aggregate_id == "sess_retry_ac_3"
        assert thinking_event.data["ac_id"] == "sess_retry_ac_3"
        assert thinking_event.data["retry_attempt"] == 2
        assert thinking_event.data["attempt_number"] == 3
        assert thinking_event.data["session_attempt_id"] == "sess_retry_ac_3_attempt_3"
        assert completed_event.aggregate_id == "sess_retry_ac_3"
        assert completed_event.data["ac_id"] == "sess_retry_ac_3"
        assert completed_event.data["retry_attempt"] == 2
        assert completed_event.data["attempt_number"] == 3
        assert completed_event.data["session_attempt_id"] == "sess_retry_ac_3_attempt_3"
        assert completed_event.data["success"] is True

    @pytest.mark.asyncio
    async def test_atomic_ac_events_capture_opencode_tool_metadata_and_results(self) -> None:
        """OpenCode AC sessions should emit normalized tool start/completion metadata."""
        from ouroboros.orchestrator.mcp_tools import (
            normalize_runtime_tool_definition,
            normalize_runtime_tool_result,
        )

        class StubRuntime:
            _runtime_handle_backend = "opencode"
            _cwd = "/tmp/project"
            _permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(self, **kwargs: object):
                resume_handle = kwargs["resume_handle"]
                assert isinstance(resume_handle, RuntimeHandle)
                runtime_handle = RuntimeHandle(
                    backend="opencode",
                    native_session_id="oc-session-7",
                    cwd="/tmp/project",
                    approval_mode="acceptEdits",
                    metadata={"runtime_event_type": "tool.started"},
                )
                yield AgentMessage(
                    type="assistant",
                    content="Calling tool: Edit: src/app.py",
                    tool_name="Edit",
                    data={
                        "tool_input": {"file_path": "src/app.py"},
                        "tool_definition": normalize_runtime_tool_definition(
                            "Edit",
                            {"file_path": "src/app.py"},
                        ),
                    },
                    resume_handle=runtime_handle,
                )
                yield AgentMessage(
                    type="assistant",
                    content="Updated src/app.py",
                    data={
                        "subtype": "tool_result",
                        "tool_name": "Edit",
                        "tool_result": normalize_runtime_tool_result("Updated src/app.py"),
                    },
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        native_session_id="oc-session-7",
                        cwd="/tmp/project",
                        approval_mode="acceptEdits",
                        metadata={"runtime_event_type": "tool.completed"},
                    ),
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                )

        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=StubRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Wire OpenCode runtime events",
            session_id="sess_opencode",
            tools=["Edit"],
            system_prompt="test",
            seed_goal="Ship the adapter",
            depth=0,
            start_time=datetime.now(UTC),
        )

        appended_events = [call.args[0] for call in event_store.append.await_args_list]
        tool_started = next(
            event for event in appended_events if event.type == "execution.tool.started"
        )
        tool_completed = next(
            event for event in appended_events if event.type == "execution.tool.completed"
        )

        assert result.success is True
        assert tool_started.data["tool_definition"]["name"] == "Edit"
        assert tool_started.data["runtime_backend"] == "opencode"
        assert tool_started.data["runtime"]["native_session_id"] == "oc-session-7"
        assert tool_completed.data["tool_name"] == "Edit"
        assert tool_completed.data["tool_result"]["text_content"] == "Updated src/app.py"
        assert tool_completed.data["runtime_event_type"] == "tool.completed"

    @pytest.mark.asyncio
    async def test_atomic_ac_projects_empty_tool_result_content_into_completion_events(
        self,
    ) -> None:
        """Tool-result projection should preserve completion text even when message content is empty."""
        from ouroboros.orchestrator.mcp_tools import normalize_runtime_tool_result

        class StubRuntime:
            _runtime_handle_backend = "opencode"
            _cwd = "/tmp/project"
            _permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(self, **kwargs: object):
                resume_handle = kwargs["resume_handle"]
                assert isinstance(resume_handle, RuntimeHandle)
                yield AgentMessage(
                    type="assistant",
                    content="",
                    data={
                        "subtype": "tool_result",
                        "tool_name": "Edit",
                        "tool_result": normalize_runtime_tool_result("[AC_COMPLETE: 1] Done!"),
                    },
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        native_session_id="oc-session-8",
                        cwd="/tmp/project",
                        approval_mode="acceptEdits",
                        metadata={"runtime_event_type": "tool.completed"},
                    ),
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                )

        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=StubRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Project OpenCode completion markers",
            session_id="sess_projection",
            tools=["Edit"],
            system_prompt="test",
            seed_goal="Ship the projection wiring",
            depth=0,
            start_time=datetime.now(UTC),
        )

        appended_events = [call.args[0] for call in event_store.append.await_args_list]
        tool_completed = next(
            event for event in appended_events if event.type == "execution.tool.completed"
        )

        assert result.success is True
        assert tool_completed.data["tool_result_text"] == "[AC_COMPLETE: 1] Done!"
        assert tool_completed.data["tool_result"]["text_content"] == "[AC_COMPLETE: 1] Done!"

    @pytest.mark.asyncio
    async def test_restarted_executor_skips_invalid_event_and_resumes_from_valid_one(
        self,
    ) -> None:
        """When an invalid persisted event precedes a valid one, resume from the valid event."""

        class _StubResumeAfterInvalidRuntime:
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
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=resume_handle,
                )

        valid_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-valid",
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
                "server_session_id": "server-valid",
            },
        )
        event_store = AsyncMock()
        event_store.replay = AsyncMock(
            return_value=[
                # First event: valid handle
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
                        "runtime": valid_handle.to_dict(),
                    },
                ),
                # Second event: invalid handle (no backend/provider)
                BaseEvent(
                    type="execution.session.resumed",
                    aggregate_type="execution",
                    aggregate_id="orch_123_ac_1",
                    data={
                        "retry_attempt": 0,
                        "session_state_path": (
                            "execution.workflows.orch_123.acceptance_criteria."
                            "ac_1.implementation_session"
                        ),
                        "runtime": {
                            "kind": "implementation_session",
                            "cwd": "/tmp/project",
                            "metadata": {},
                        },
                    },
                ),
            ]
        )
        event_store.append = AsyncMock()
        runtime = _StubResumeAfterInvalidRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Resume after skipping invalid persisted event",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )

        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        # Should have resumed from the valid (first) event, not the invalid (second) one
        assert resume_handle.native_session_id == "opencode-session-valid"
        assert resume_handle.metadata["server_session_id"] == "server-valid"
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id == resume_handle.native_session_id
        assert result.runtime_handle.metadata == resume_handle.metadata
