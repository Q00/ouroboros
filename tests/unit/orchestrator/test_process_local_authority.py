"""Foundation A process-local authority lifecycle regressions."""

from __future__ import annotations

import asyncio
import os
import pickle
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from ouroboros.cli.commands.cancel import _cancel_session
from ouroboros.core.errors import PersistenceError
from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    InvestmentSpec,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.core.types import Result
from ouroboros.core.worktree import TaskWorkspace
from ouroboros.mcp.job_manager import JobLinks, JobManager
from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler
from ouroboros.mcp.tools.job_handlers import CancelExecutionHandler
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator import heartbeat
from ouroboros.orchestrator.adapter import FULL_CAPABILITIES, AgentMessage
from ouroboros.orchestrator.events import create_session_completed_event
from ouroboros.orchestrator.execution_authority import (
    _PROCESS_LOCAL_AUTHORITY_REGISTRY,
    ProcessLocalCancellationDisposition,
    _ProcessLocalAuthorityLifecycleState,
    _register_process_local_authority_terminal_finalizer,
    _retire_process_local_authority_after_terminal_persistence,
    request_process_local_cancellation,
)
from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog
from ouroboros.orchestrator.parallel_executor import ACExecutionResult, ParallelExecutionResult
from ouroboros.orchestrator.runner import (
    EXECUTION_CONTRACT_PROGRESS_KEY,
    OrchestratorError,
    OrchestratorResult,
    OrchestratorRunner,
    clear_cancellation,
    get_cancellation_request,
    get_pending_cancellations,
    is_cancellation_requested,
    request_cancellation,
)
from ouroboros.orchestrator.session import SessionRepository, SessionStatus, SessionTracker
from ouroboros.persistence.checkpoint import CheckpointStore
from ouroboros.persistence.event_store import EventStore
from ouroboros.persistence.uow import UnitOfWork


class _CountingRuntime:
    """Runtime double that records forbidden resume-provider lookups."""

    runtime_backend = "process-local-test"
    llm_backend = "test-llm"
    permission_mode = "bypassPermissions"
    capabilities = FULL_CAPABILITIES
    working_directory = "/tmp"
    _model = "test-model"

    def __init__(self) -> None:
        self.identity_provider_calls = 0
        self.resume_selector_calls = 0
        self.execute_calls = 0

    def execution_identity_contract(self) -> dict[str, object]:
        self.identity_provider_calls += 1
        raise AssertionError("process-local resume must not ask a runtime identity provider")

    def resume_handle_execution_identity_contract(self, _: object) -> dict[str, object]:
        self.resume_selector_calls += 1
        raise AssertionError("process-local resume must not ask a resume selector provider")

    async def execute_task(self, **_: object):
        self.execute_calls += 1
        if False:  # pragma: no cover - process-local guard must stop first
            yield AgentMessage(type="result", content="unreachable")


class _SuccessfulRuntime(_CountingRuntime):
    """Runtime double that completes one sequential execution successfully."""

    runtime_backend = "codex_cli"

    async def execute_task(self, **_: object):
        self.execute_calls += 1
        yield AgentMessage(type="result", content="completed", data={"subtype": "success"})


class _RecoverablePauseRuntime(_CountingRuntime):
    """Runtime double that requests a durable usage-limit pause."""

    runtime_backend = "codex_cli"

    async def execute_task(self, **_: object):
        self.execute_calls += 1
        yield AgentMessage(
            type="result",
            content="Usage limit reached. Please try again in 5 hours.",
            data={"subtype": "error", "error_type": "CodexCliError"},
        )


class _FailedRuntime(_CountingRuntime):
    """Runtime double that produces a non-recoverable failure."""

    runtime_backend = "codex_cli"

    async def execute_task(self, **_: object):
        self.execute_calls += 1
        yield AgentMessage(
            type="result",
            content="Permanent runtime failure",
            data={"subtype": "error", "error_type": "RuntimeError"},
        )


def _seed() -> Seed:
    return Seed(
        goal="Keep authority process-local",
        acceptance_criteria=("Do not reuse a lost runtime capability",),
        ontology_schema=OntologySchema(name="Authority", description="Process-local authority"),
        metadata=SeedMetadata(seed_id="seed-process-local-authority"),
    )


def _runner(runtime: _CountingRuntime | None = None) -> OrchestratorRunner:
    return OrchestratorRunner(runtime or _CountingRuntime(), AsyncMock(), MagicMock())


async def _prepare(
    runner: OrchestratorRunner,
    *,
    session_id: str,
    execution_id: str,
) -> SessionTracker:
    tracker = SessionTracker.create(
        execution_id,
        _seed().metadata.seed_id,
        session_id=session_id,
    )
    with (
        patch.object(
            runner._session_repo,
            "create_session",
            AsyncMock(return_value=Result.ok(tracker)),
        ),
        patch.object(
            runner._session_repo,
            "track_progress",
            AsyncMock(return_value=Result.ok(None)),
        ),
    ):
        prepared = await runner.prepare_session(
            _seed(),
            execution_id=execution_id,
            session_id=session_id,
        )
    assert prepared.is_ok
    return prepared.value


def _paused(tracker: SessionTracker) -> SessionTracker:
    return tracker.with_status(SessionStatus.PAUSED)


class _HandlerEventStore:
    """Minimal handler store double for process-local resume routing."""

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_prepare_session_registers_an_opaque_live_generation() -> None:
    runner = _runner()
    tracker = await _prepare(
        runner,
        session_id="session-prepared-local",
        execution_id="exec-prepared-local",
    )
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    generation = runner._process_local_authorities[(tracker.session_id, tracker.execution_id)]

    try:
        assert runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        with pytest.raises(TypeError, match="cannot be serialized"):
            pickle.dumps(generation)
        with pytest.raises(TypeError, match="registry-minted"):
            type(generation)(object(), generation.correlation_id)
    finally:
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )

    assert not runner._has_live_process_local_authority(
        tracker.session_id,
        tracker.execution_id,
        contract,
    )


@pytest.mark.asyncio
async def test_pause_persistence_failure_keeps_durable_running_owner(tmp_path) -> None:
    """A failed PAUSED write cannot publish success or orphan durable RUNNING."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'pause-pending.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_RecoverablePauseRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-pause-pending",
        session_id="session-pause-pending",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]

    try:
        with patch.object(
            runner._session_repo,
            "mark_paused",
            AsyncMock(return_value=Result.err(PersistenceError("pause write unavailable"))),
        ):
            result = await runner.execute_precreated_session(
                seed=_seed(),
                tracker=tracker,
                parallel=False,
            )

        assert result.is_err
        assert result.error.details["resume_blocked"] == "pause_persistence_pending"
        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok
        assert durable.value.status == SessionStatus.RUNNING
        assert runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert heartbeat.is_holder_alive(tracker.session_id)
        assert tracker.execution_id not in runner.active_sessions
        session_events = await event_store.replay("session", tracker.session_id)
        assert "orchestrator.session.paused" not in [event.type for event in session_events]

        retried = await runner.resume_session(tracker.session_id, _seed())
        assert retried.is_ok
        assert retried.value.summary["replayed_pending_lifecycle"] == "paused"
        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok and durable.value.status == SessionStatus.PAUSED
        assert runner._adapter.execute_calls == 1
        assert tracker.session_id not in runner._pending_lifecycle_intents
        assert runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert heartbeat.is_holder_alive(tracker.session_id)
    finally:
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        runner._unregister_session(tracker.execution_id, tracker.session_id)
        await event_store.close()


@pytest.mark.asyncio
async def test_paused_projection_failure_keeps_durable_paused_owner(tmp_path) -> None:
    """Auxiliary projection failure cannot convert durable PAUSED into FAILED."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'paused-projection.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_RecoverablePauseRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-paused-projection",
        session_id="session-paused-projection",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    append = event_store.append

    async def fail_execution_projection(event, **kwargs):
        if event.type == "execution.terminal":
            raise PersistenceError("execution projection unavailable")
        return await append(event, **kwargs)

    try:
        with patch.object(event_store, "append", fail_execution_projection):
            result = await runner.execute_precreated_session(
                seed=_seed(),
                tracker=tracker,
                parallel=False,
            )

        assert result.is_ok
        assert result.value.success is False
        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok
        assert durable.value.status == SessionStatus.PAUSED
        assert runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert heartbeat.is_holder_alive(tracker.session_id)
        session_events = await event_store.replay("session", tracker.session_id)
        event_types = [event.type for event in session_events]
        assert event_types.count("orchestrator.session.paused") == 1
        assert "orchestrator.session.failed" not in event_types
    finally:
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        runner._unregister_session(tracker.execution_id, tracker.session_id)
        await event_store.close()


@pytest.mark.asyncio
async def test_sequential_pause_reconciles_preexisting_terminal_winner(tmp_path) -> None:
    """A terminal winner immediately before PAUSED retires all live ownership."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'pause-terminal-sequential.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_RecoverablePauseRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-pause-terminal-sequential",
        session_id="session-pause-terminal-sequential",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    original_mark_paused = runner._session_repo.mark_paused

    async def terminal_then_pause(*args: object, **kwargs: object):
        cancelled = await runner._session_repo.mark_cancelled(
            tracker.session_id,
            reason="terminal wins before pause",
        )
        assert cancelled.is_ok and cancelled.value is True
        return await original_mark_paused(*args, **kwargs)

    try:
        with patch.object(runner._session_repo, "mark_paused", terminal_then_pause):
            result = await runner.execute_precreated_session(
                seed=_seed(),
                tracker=tracker,
                parallel=False,
            )

        assert result.is_ok
        assert result.value.success is False
        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok and durable.value.status == SessionStatus.CANCELLED
        events = await event_store.replay("session", tracker.session_id)
        assert "orchestrator.session.paused" not in [event.type for event in events]
        assert not runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert not heartbeat.is_holder_alive(tracker.session_id)
    finally:
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        runner._unregister_session(tracker.execution_id, tracker.session_id)
        await event_store.close()


@pytest.mark.asyncio
async def test_parallel_pause_reconciles_preexisting_terminal_winner(tmp_path) -> None:
    """The parallel pause path projects and cleans up the durable terminal winner."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'pause-terminal-parallel.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_RecoverablePauseRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-pause-terminal-parallel",
        session_id="session-pause-terminal-parallel",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    generation, already_claimed = runner._claim_process_local_authority_generation(
        tracker.session_id,
        tracker.execution_id,
        contract,
    )
    assert generation is not None and already_claimed is False
    runner._register_session(tracker.execution_id, tracker.session_id)
    message = AgentMessage(
        type="result",
        content="Usage limit reached. Please try again in 5 hours.",
        data={"subtype": "error", "error_type": "CodexCliError"},
    )
    parallel_result = ParallelExecutionResult(
        results=(
            ACExecutionResult(
                ac_index=0,
                ac_content=_seed().acceptance_criteria[0],
                success=False,
                messages=(message,),
                final_message=message.content,
            ),
        ),
        success_count=0,
        failure_count=1,
        total_messages=1,
    )
    original_mark_paused = runner._session_repo.mark_paused

    async def terminal_then_pause(*args: object, **kwargs: object):
        cancelled = await runner._session_repo.mark_cancelled(
            tracker.session_id,
            reason="terminal wins before parallel pause",
        )
        assert cancelled.is_ok and cancelled.value is True
        return await original_mark_paused(*args, **kwargs)

    try:
        with (
            patch.object(runner, "_check_cancellation", AsyncMock(return_value=False)),
            patch(
                "ouroboros.orchestrator.parallel_executor.ParallelACExecutor.execute_parallel",
                AsyncMock(return_value=parallel_result),
            ),
            patch.object(runner._session_repo, "mark_paused", terminal_then_pause),
        ):
            result = await runner._execute_parallel(
                seed=_seed(),
                exec_id=tracker.execution_id,
                tracker=tracker,
                merged_tools=["Read"],
                tool_catalog=assemble_session_tool_catalog(["Read"]),
                system_prompt="system",
                start_time=tracker.start_time,
                force_sequential_levels=True,
            )

        assert result.is_ok
        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok and durable.value.status == SessionStatus.CANCELLED
        events = await event_store.replay("session", tracker.session_id)
        assert "orchestrator.session.paused" not in [event.type for event in events]
        assert not runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert not heartbeat.is_holder_alive(tracker.session_id)
    finally:
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        runner._unregister_session(tracker.execution_id, tracker.session_id)
        await event_store.close()


@pytest.mark.asyncio
async def test_resume_pause_reconciles_preexisting_terminal_winner(tmp_path) -> None:
    """A resumed recoverable failure cannot preserve authority beside CANCELLED."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'pause-terminal-resume.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_RecoverablePauseRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-pause-terminal-resume",
        session_id="session-pause-terminal-resume",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    initial_pause = await runner._session_repo.mark_paused(
        tracker.session_id,
        reason="initial resumable pause",
    )
    assert initial_pause.is_ok and initial_pause.value is True
    original_mark_paused = runner._session_repo.mark_paused

    async def terminal_then_pause(*args: object, **kwargs: object):
        cancelled = await runner._session_repo.mark_cancelled(
            tracker.session_id,
            reason="terminal wins before resumed pause",
        )
        assert cancelled.is_ok and cancelled.value is True
        return await original_mark_paused(*args, **kwargs)

    try:
        with patch.object(runner._session_repo, "mark_paused", terminal_then_pause):
            result = await runner.resume_session(tracker.session_id, _seed())

        assert result.is_ok
        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok and durable.value.status == SessionStatus.CANCELLED
        events = await event_store.replay("session", tracker.session_id)
        assert [event.type for event in events].count("orchestrator.session.paused") == 1
        assert not runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert not heartbeat.is_holder_alive(tracker.session_id)
    finally:
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        runner._unregister_session(tracker.execution_id, tracker.session_id)
        await event_store.close()


@pytest.mark.asyncio
async def test_registry_encodes_claim_and_terminalization_as_one_state() -> None:
    """One lifecycle entry cannot be claimed and terminalizing simultaneously."""
    runner = _runner()
    tracker = await _prepare(
        runner,
        session_id="session-one-lifecycle-state",
        execution_id="exec-one-lifecycle-state",
    )
    authority = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]["foundation_a_authority"]
    lifecycle = _PROCESS_LOCAL_AUTHORITY_REGISTRY._lifecycles[tracker.session_id]

    try:
        assert lifecycle.state is _ProcessLocalAuthorityLifecycleState.REGISTERED

        generation, already_owned = _PROCESS_LOCAL_AUTHORITY_REGISTRY.claim(
            tracker.session_id,
            tracker.execution_id,
            authority,
            runner._adapter,
        )
        assert generation is not None
        assert already_owned is False
        assert lifecycle.state is _ProcessLocalAuthorityLifecycleState.CLAIMED

        reserved, active_owner = _PROCESS_LOCAL_AUTHORITY_REGISTRY.begin_terminalization(
            tracker.session_id,
            tracker.execution_id,
            authority,
        )
        assert reserved is False
        assert active_owner is True
        assert lifecycle.state is _ProcessLocalAuthorityLifecycleState.CLAIMED

        _PROCESS_LOCAL_AUTHORITY_REGISTRY.release(
            tracker.session_id,
            tracker.execution_id,
            runner._adapter,
        )
        reserved, active_owner = _PROCESS_LOCAL_AUTHORITY_REGISTRY.begin_terminalization(
            tracker.session_id,
            tracker.execution_id,
            authority,
        )
        assert reserved is True
        assert active_owner is False
        assert lifecycle.state is _ProcessLocalAuthorityLifecycleState.TERMINALIZING

        generation, already_owned = _PROCESS_LOCAL_AUTHORITY_REGISTRY.claim(
            tracker.session_id,
            tracker.execution_id,
            authority,
            runner._adapter,
        )
        assert generation is None
        assert already_owned is True
        assert lifecycle.state is _ProcessLocalAuthorityLifecycleState.TERMINALIZING

        _PROCESS_LOCAL_AUTHORITY_REGISTRY.abort_terminalization(
            tracker.session_id,
            tracker.execution_id,
            authority,
        )
        assert lifecycle.state is _ProcessLocalAuthorityLifecycleState.REGISTERED
    finally:
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )


@pytest.mark.asyncio
async def test_forged_correlation_cannot_register_or_resume_in_a_fresh_runner() -> None:
    original = _runner()
    tracker = await _prepare(
        original,
        session_id="session-forged-local",
        execution_id="exec-forged-local",
    )
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    restarted_runtime = _CountingRuntime()
    restarted = _runner(restarted_runtime)
    forged = restarted._begin_process_local_authority_generation()
    # Simulate a caller that has persisted diagnostics and tampers with a new
    # locally minted object.  The registry's mint record still retains the
    # original random correlation, so this cannot become a live authority.
    object.__setattr__(
        forged,
        "_correlation_id",
        contract["foundation_a_authority"]["correlation_id"],
    )

    try:
        with pytest.raises(OrchestratorError, match="Cannot register"):
            restarted._register_process_local_authority(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
                execution_contract=contract,
                generation=forged,
            )

        paused = _paused(tracker)
        restarted._session_repo.reconstruct_session = AsyncMock(return_value=Result.ok(paused))
        restarted._session_repo.mark_failed = AsyncMock(return_value=Result.ok(None))
        restore = MagicMock(side_effect=AssertionError("restore must not run"))
        restarted._restore_execution_contract = restore

        result = await restarted.resume_session(paused.session_id, _seed())

        assert result.is_err
        assert result.error.details["resume_blocked"] == "process_local_authority_held_elsewhere"
        assert restarted_runtime.identity_provider_calls == 0
        assert restarted_runtime.resume_selector_calls == 0
        assert restarted_runtime.execute_calls == 0
        restore.assert_not_called()
    finally:
        restarted._discard_process_local_authority(forged)
        original._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )


@pytest.mark.asyncio
async def test_forked_child_cannot_use_parent_process_local_authority() -> None:
    if not hasattr(os, "fork"):
        pytest.skip("fork is unavailable on this platform")
    runner = _runner()
    tracker = await _prepare(
        runner,
        session_id="session-fork-local",
        execution_id="exec-fork-local",
    )
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    read_fd, write_fd = os.pipe()

    try:
        child_pid = os.fork()
        if child_pid == 0:  # pragma: no cover - executed in an isolated child
            try:
                os.close(read_fd)
                live = runner._has_live_process_local_authority(
                    tracker.session_id,
                    tracker.execution_id,
                    contract,
                )
                os.write(write_fd, b"1" if live else b"0")
            finally:
                os.close(write_fd)
                os._exit(0)
        os.close(write_fd)
        observed = os.read(read_fd, 1)
        _, status = os.waitpid(child_pid, 0)
        assert os.WIFEXITED(status)
        assert observed == b"0"
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )


@pytest.mark.asyncio
async def test_concurrent_preparations_get_distinct_live_generations() -> None:
    runner = _runner()

    async def create_session(**kwargs: object) -> Result[SessionTracker, object]:
        return Result.ok(
            SessionTracker.create(
                str(kwargs["execution_id"]),
                str(kwargs["seed_id"]),
                session_id=str(kwargs["session_id"]),
            )
        )

    with (
        patch.object(runner._session_repo, "create_session", create_session),
        patch.object(
            runner._session_repo,
            "track_progress",
            AsyncMock(return_value=Result.ok(None)),
        ),
    ):
        first_result, second_result = await asyncio.gather(
            runner.prepare_session(
                _seed(),
                session_id="session-concurrent-one",
                execution_id="exec-concurrent-one",
            ),
            runner.prepare_session(
                _seed(),
                session_id="session-concurrent-two",
                execution_id="exec-concurrent-two",
            ),
        )
    assert first_result.is_ok
    assert second_result.is_ok
    first = first_result.value
    second = second_result.value
    first_contract = first.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    second_contract = second.progress[EXECUTION_CONTRACT_PROGRESS_KEY]

    try:
        assert (
            first_contract["foundation_a_authority"]["correlation_id"]
            != second_contract["foundation_a_authority"]["correlation_id"]
        )
        assert runner._has_live_process_local_authority(
            first.session_id,
            first.execution_id,
            first_contract,
        )
        assert runner._has_live_process_local_authority(
            second.session_id,
            second.execution_id,
            second_contract,
        )
    finally:
        for tracker in (first, second):
            runner._retire_process_local_authority(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
            )


@pytest.mark.asyncio
async def test_legacy_precreated_tracker_fails_before_tool_setup() -> None:
    runtime = _CountingRuntime()
    runner = _runner(runtime)
    tracker = SessionTracker.create(
        "exec-legacy-local",
        _seed().metadata.seed_id,
        session_id="session-legacy-local",
    )
    get_tools = AsyncMock(side_effect=AssertionError("tool setup must not run"))
    runner._get_merged_tools = get_tools

    result = await runner.execute_precreated_session(_seed(), tracker, parallel=False)

    assert result.is_err
    assert result.error.details["resume_blocked"] == "process_local_resume_unavailable"
    assert runtime.identity_provider_calls == 0
    assert runtime.resume_selector_calls == 0
    assert runtime.execute_calls == 0
    get_tools.assert_not_called()


@pytest.mark.asyncio
async def test_stale_running_tracker_after_process_loss_terminally_fails_closed() -> None:
    original = _runner()
    tracker = await _prepare(
        original,
        session_id="session-stale-running-local",
        execution_id="exec-stale-running-local",
    )
    restarted = _runner(_CountingRuntime())
    restarted._session_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))
    restarted._session_repo.mark_failed_if_active = AsyncMock(return_value=Result.ok(True))

    # Simulate the creating process exiting: both its registry entry and its
    # early liveness lease disappear before another process observes RUNNING.
    original._retire_process_local_authority(
        session_id=tracker.session_id,
        execution_id=tracker.execution_id,
    )
    result = await restarted.resume_session(tracker.session_id, _seed())

    assert result.is_err
    assert result.error.details["resume_blocked"] == "process_local_resume_unavailable"
    restarted._session_repo.mark_failed_if_active.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy_gate", ["fat_harness", "investment"])
async def test_lost_paused_authority_precedes_current_policy_gate(policy_gate: str) -> None:
    """Current policy cannot mask a persisted paused owner's disappearance."""
    original = _runner()
    tracker = await _prepare(
        original,
        session_id=f"session-lost-paused-{policy_gate}",
        execution_id=f"exec-lost-paused-{policy_gate}",
    )
    paused = tracker.with_status(SessionStatus.PAUSED)
    original._retire_process_local_authority(
        session_id=tracker.session_id,
        execution_id=tracker.execution_id,
    )
    resumed_seed = _seed()
    if policy_gate == "investment":
        resumed_seed = resumed_seed.model_copy(
            update={
                "acceptance_criteria": (
                    AcceptanceCriterionSpec(
                        description="Protect authority ordering",
                        investment=InvestmentSpec(
                            difficulty="medium",
                            stakes="high",
                            provenance="declared",
                            confidence="high",
                        ),
                    ),
                )
            }
        )
    restarted = OrchestratorRunner(
        _CountingRuntime(),
        AsyncMock(),
        MagicMock(),
        fat_harness_mode=policy_gate == "fat_harness",
    )
    restarted._session_repo.reconstruct_session = AsyncMock(return_value=Result.ok(paused))
    restarted._session_repo.mark_failed_if_active = AsyncMock(return_value=Result.ok(True))

    result = await restarted.resume_session(paused.session_id, resumed_seed)

    assert result.is_err
    assert result.error.details["resume_blocked"] == "process_local_resume_unavailable"
    restarted._session_repo.mark_failed_if_active.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_running_tracker_is_not_terminalized_by_another_runner() -> None:
    original = _runner()
    tracker = await _prepare(
        original,
        session_id="session-live-running-local",
        execution_id="exec-live-running-local",
    )
    observer = _runner(_CountingRuntime())
    observer._session_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))
    observer._session_repo.mark_failed = AsyncMock(return_value=Result.ok(None))

    try:
        result = await observer.resume_session(tracker.session_id, _seed())
    finally:
        original._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )

    assert result.is_err
    assert result.error.details["resume_blocked"] == "process_local_authority_held_elsewhere"
    observer._session_repo.mark_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_owner_running_resume_preserves_its_worktree_and_claim() -> None:
    """A concurrent resume must not release an active owner's workspace."""
    owner = _runner()
    tracker = await _prepare(
        owner,
        session_id="session-live-running-owner",
        execution_id="exec-live-running-owner",
    )
    owner._task_workspace = SimpleNamespace(lock_path="/tmp/process-local-running.lock")
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    generation, already_claimed = owner._claim_process_local_authority_generation(
        tracker.session_id,
        tracker.execution_id,
        contract,
    )
    assert generation is not None
    assert already_claimed is False
    owner._session_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))
    owner._session_repo.mark_failed = AsyncMock(return_value=Result.ok(None))

    try:
        with patch("ouroboros.orchestrator.runner.release_lock") as release_lock_mock:
            result = await owner.resume_session(tracker.session_id, _seed())

        assert result.is_err
        assert result.error.details["resume_blocked"] == "process_local_execution_in_progress"
        release_lock_mock.assert_not_called()
        owner._session_repo.mark_failed.assert_not_awaited()
        assert owner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
    finally:
        owner._release_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        owner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )


@pytest.mark.asyncio
async def test_terminal_precreated_tracker_retires_a_stale_live_authority() -> None:
    runner = _runner()
    prepared = await _prepare(
        runner,
        session_id="session-terminal-local",
        execution_id="exec-terminal-local",
    )
    contract = prepared.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    terminal = prepared.with_status(SessionStatus.COMPLETED)
    get_tools = AsyncMock(side_effect=AssertionError("tool setup must not run"))
    runner._get_merged_tools = get_tools
    runner._session_repo.reconstruct_session = AsyncMock(return_value=Result.ok(terminal))

    result = await runner.execute_precreated_session(_seed(), terminal, parallel=False)

    assert result.is_err
    assert not runner._has_live_process_local_authority(
        prepared.session_id,
        prepared.execution_id,
        contract,
    )
    get_tools.assert_not_called()


@pytest.mark.asyncio
async def test_precreated_execution_claim_allows_only_one_effectful_caller() -> None:
    runner = _runner()
    tracker = await _prepare(
        runner,
        session_id="session-exclusive-local",
        execution_id="exec-exclusive-local",
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    tool_catalog = assemble_session_tool_catalog(["Read"])

    async def block_tool_setup(**_: object):
        entered.set()
        await release.wait()
        return ["Read"], None, tool_catalog

    runner._get_merged_tools = block_tool_setup
    first = asyncio.create_task(runner.execute_precreated_session(_seed(), tracker, parallel=False))
    await asyncio.wait_for(entered.wait(), timeout=1)

    second = await runner.execute_precreated_session(_seed(), tracker, parallel=False)
    assert second.is_err
    assert second.error.details["resume_blocked"] == "process_local_execution_in_progress"

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()

    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    assert runner._has_live_process_local_authority(
        tracker.session_id,
        tracker.execution_id,
        contract,
    )
    assert heartbeat.is_holder_alive(tracker.session_id)
    generation, already_claimed = runner._claim_process_local_authority_generation(
        tracker.session_id,
        tracker.execution_id,
        contract,
    )
    assert generation is not None
    assert already_claimed is False
    runner._release_process_local_authority(
        session_id=tracker.session_id,
        execution_id=tracker.execution_id,
    )
    runner._retire_process_local_authority(
        session_id=tracker.session_id,
        execution_id=tracker.execution_id,
    )


@pytest.mark.asyncio
async def test_precreated_setup_cancellation_releases_its_authority_claim() -> None:
    """A raw cancellation during post-claim setup cannot permanently block resume."""
    runner = _runner()
    tracker = await _prepare(
        runner,
        session_id="session-precreated-setup-cancel",
        execution_id="exec-precreated-setup-cancel",
    )

    with (
        patch(
            "ouroboros.orchestrator.runner.asyncio.to_thread",
            AsyncMock(side_effect=asyncio.CancelledError),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await runner.execute_precreated_session(_seed(), tracker, parallel=False)

    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    assert runner._has_live_process_local_authority(
        tracker.session_id,
        tracker.execution_id,
        contract,
    )
    assert heartbeat.is_holder_alive(tracker.session_id)
    generation, already_claimed = runner._claim_process_local_authority_generation(
        tracker.session_id,
        tracker.execution_id,
        contract,
    )
    assert generation is not None
    assert already_claimed is False
    runner._release_process_local_authority(
        session_id=tracker.session_id,
        execution_id=tracker.execution_id,
    )
    runner._retire_process_local_authority(
        session_id=tracker.session_id,
        execution_id=tracker.execution_id,
    )


@pytest.mark.asyncio
async def test_resume_setup_cancellation_releases_its_authority_claim() -> None:
    """A raw cancellation during resume restoration cannot leave a claimed lifecycle."""
    runner = _runner()
    tracker = await _prepare(
        runner,
        session_id="session-resume-setup-cancel",
        execution_id="exec-resume-setup-cancel",
    )
    paused = _paused(tracker)
    runner._session_repo.reconstruct_session = AsyncMock(return_value=Result.ok(paused))

    with (
        patch(
            "ouroboros.orchestrator.runner.asyncio.to_thread",
            AsyncMock(side_effect=asyncio.CancelledError),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await runner.resume_session(paused.session_id, _seed())

    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    assert runner._has_live_process_local_authority(
        tracker.session_id,
        tracker.execution_id,
        contract,
    )
    assert heartbeat.is_holder_alive(tracker.session_id)
    generation, already_claimed = runner._claim_process_local_authority_generation(
        tracker.session_id,
        tracker.execution_id,
        contract,
    )
    assert generation is not None
    assert already_claimed is False
    runner._release_process_local_authority(
        session_id=tracker.session_id,
        execution_id=tracker.execution_id,
    )
    runner._retire_process_local_authority(
        session_id=tracker.session_id,
        execution_id=tracker.execution_id,
    )


@pytest.mark.asyncio
async def test_terminal_tracker_copy_cannot_retire_durable_running_owner(tmp_path) -> None:
    """Caller status is not durable proof for authority retirement."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'stale-terminal-copy.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(
        _CountingRuntime(),
        event_store,
        MagicMock(),
        fat_harness_mode=False,
    )
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-stale-terminal-copy",
        session_id="session-stale-terminal-copy",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    generation, already_claimed = runner._claim_process_local_authority_generation(
        tracker.session_id,
        tracker.execution_id,
        contract,
    )
    assert generation is not None
    assert already_claimed is False

    try:
        result = await runner.execute_precreated_session(
            _seed(),
            tracker.with_status(SessionStatus.COMPLETED),
            parallel=False,
        )

        assert result.is_err
        assert result.error.details["resume_blocked"] == "process_local_execution_in_progress"
        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok
        assert durable.value.status == SessionStatus.RUNNING
        assert runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert heartbeat.is_holder_alive(tracker.session_id)
    finally:
        runner._release_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_setup_failure_persistence_error_preserves_retryable_owner(tmp_path) -> None:
    """A post-claim setup failure cannot retire authority before FAILED persists."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'setup-pending.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(
        _CountingRuntime(),
        event_store,
        MagicMock(),
        fat_harness_mode=False,
    )
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-setup-persistence-pending",
        session_id="session-setup-persistence-pending",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    runner._get_merged_tools = AsyncMock(side_effect=RuntimeError("tool setup exploded"))
    runner._session_repo.mark_failed = AsyncMock(
        return_value=Result.err(PersistenceError("terminal store unavailable"))
    )

    try:
        result = await runner.execute_precreated_session(_seed(), tracker, parallel=False)

        assert result.is_err
        assert result.error.details["resume_blocked"] == "terminal_persistence_pending"
        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok
        assert durable.value.status == SessionStatus.RUNNING
        assert runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert heartbeat.is_holder_alive(tracker.session_id)
        assert tracker.execution_id not in runner.active_sessions
        generation, already_claimed = runner._claim_process_local_authority_generation(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert generation is not None
        assert already_claimed is False
        runner._release_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
    finally:
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_execution_failure_persistence_error_preserves_retryable_owner(tmp_path) -> None:
    """An execution exception keeps its lease and releases only the effect claim."""

    class _ExplodingRuntime(_CountingRuntime):
        async def execute_task(self, **_: object):
            raise RuntimeError("runtime stream exploded")
            if False:  # pragma: no cover - makes this an async generator
                yield AgentMessage(type="result", content="unreachable")

    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'execute-pending.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(
        _ExplodingRuntime(),
        event_store,
        MagicMock(),
        fat_harness_mode=False,
    )
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-runtime-persistence-pending",
        session_id="session-runtime-persistence-pending",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    runner._session_repo.mark_failed = AsyncMock(
        return_value=Result.err(PersistenceError("terminal store unavailable"))
    )

    try:
        result = await runner.execute_precreated_session(_seed(), tracker, parallel=False)

        assert result.is_err
        assert result.error.details["resume_blocked"] == "terminal_persistence_pending"
        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok
        assert durable.value.status == SessionStatus.RUNNING
        assert runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert heartbeat.is_holder_alive(tracker.session_id)
        assert tracker.execution_id not in runner.active_sessions
        generation, already_claimed = runner._claim_process_local_authority_generation(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert generation is not None
        assert already_claimed is False
        runner._release_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
    finally:
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_completed_terminal_write_retries_without_changing_to_failed(tmp_path) -> None:
    """A transient COMPLETED write failure keeps its original terminal intent."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'complete-retry.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(
        _SuccessfulRuntime(),
        event_store,
        MagicMock(),
        fat_harness_mode=False,
    )
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-complete-terminal-retry",
        session_id="session-complete-terminal-retry",
    )
    assert prepared.is_ok
    tracker = prepared.value
    original_mark_completed = runner._session_repo.mark_completed
    completion_attempts = 0

    async def _mark_completed_with_retry(*args: object, **kwargs: object):
        nonlocal completion_attempts
        completion_attempts += 1
        if completion_attempts == 1:
            return Result.err(PersistenceError("transient completion failure"))
        return await original_mark_completed(*args, **kwargs)

    runner._session_repo.mark_completed = AsyncMock(side_effect=_mark_completed_with_retry)
    try:
        result = await runner.execute_precreated_session(_seed(), tracker, parallel=False)

        assert result.is_ok
        assert result.value.success is True
        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok
        assert durable.value.status == SessionStatus.COMPLETED
        assert completion_attempts == 2
        assert not heartbeat.is_holder_alive(tracker.session_id)
    finally:
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_completed_terminal_persistence_error_preserves_retryable_owner(tmp_path) -> None:
    """Persistent COMPLETED failure cannot be rewritten as FAILED."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'complete-pending.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(
        _SuccessfulRuntime(),
        event_store,
        MagicMock(),
        fat_harness_mode=False,
    )
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-complete-terminal-pending",
        session_id="session-complete-terminal-pending",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    original_mark_completed = runner._session_repo.mark_completed
    runner._session_repo.mark_completed = AsyncMock(
        return_value=Result.err(PersistenceError("completion store unavailable"))
    )
    mark_failed = AsyncMock(return_value=Result.ok(None))
    runner._session_repo.mark_failed = mark_failed

    try:
        result = await runner.execute_precreated_session(_seed(), tracker, parallel=False)

        assert result.is_err
        assert result.error.details["resume_blocked"] == "terminal_persistence_pending"
        assert result.error.details["requested_status"] == SessionStatus.COMPLETED.value
        mark_failed.assert_not_awaited()
        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok
        assert durable.value.status == SessionStatus.RUNNING
        assert runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert heartbeat.is_holder_alive(tracker.session_id)

        runner._session_repo.mark_completed = original_mark_completed
        retried = await runner.resume_session(tracker.session_id, _seed())
        assert retried.is_ok
        assert retried.value.success is True
        assert retried.value.summary["replayed_pending_lifecycle"] == "completed"
        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok and durable.value.status == SessionStatus.COMPLETED
        assert runner._adapter.execute_calls == 1
        assert tracker.session_id not in runner._pending_lifecycle_intents
        assert not runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert not heartbeat.is_holder_alive(tracker.session_id)
    finally:
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_failed_terminal_persistence_intent_replays_before_resume(tmp_path) -> None:
    """A retained FAILED intent terminalizes before any resumed runtime effect."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'failed-pending-retry.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_FailedRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-failed-terminal-pending",
        session_id="session-failed-terminal-pending",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    original_mark_failed = runner._session_repo.mark_failed
    runner._session_repo.mark_failed = AsyncMock(
        return_value=Result.err(PersistenceError("failure store unavailable"))
    )

    try:
        result = await runner.execute_precreated_session(_seed(), tracker, parallel=False)
        assert result.is_err
        assert result.error.details["requested_status"] == SessionStatus.FAILED.value
        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok and durable.value.status == SessionStatus.RUNNING
        execute_calls_after_failure = runner._adapter.execute_calls

        runner._session_repo.mark_failed = original_mark_failed
        retried = await runner.resume_session(tracker.session_id, _seed())

        assert retried.is_ok
        assert retried.value.success is False
        assert retried.value.summary["replayed_pending_lifecycle"] == "failed"
        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok and durable.value.status == SessionStatus.FAILED
        assert runner._adapter.execute_calls == execute_calls_after_failure
        assert tracker.session_id not in runner._pending_lifecycle_intents
        assert not runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert not heartbeat.is_holder_alive(tracker.session_id)
    finally:
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_pending_completed_commit_then_cancel_reconciles_owner(tmp_path) -> None:
    """A replay cancellation after the COMPLETED CAS cannot preserve authority."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'complete-post-cas.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(
        _SuccessfulRuntime(),
        event_store,
        MagicMock(),
        fat_harness_mode=False,
    )
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-complete-post-cas",
        session_id="session-complete-post-cas",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    original_mark_completed = runner._session_repo.mark_completed
    runner._session_repo.mark_completed = AsyncMock(
        return_value=Result.err(PersistenceError("completion store unavailable"))
    )

    try:
        first = await runner.execute_precreated_session(_seed(), tracker, parallel=False)
        assert first.is_err

        async def _commit_then_cancel(*args: object, **kwargs: object):
            committed = await original_mark_completed(*args, **kwargs)
            assert committed.is_ok
            raise asyncio.CancelledError

        runner._session_repo.mark_completed = AsyncMock(side_effect=_commit_then_cancel)
        with pytest.raises(asyncio.CancelledError):
            await runner.resume_session(tracker.session_id, _seed())

        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok and durable.value.status == SessionStatus.COMPLETED
        assert tracker.session_id not in runner._pending_lifecycle_intents
        assert not runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert not heartbeat.is_holder_alive(tracker.session_id)
        assert tracker.execution_id not in runner.active_sessions
    finally:
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_pending_failed_commit_then_cancel_reconciles_owner(tmp_path) -> None:
    """A replay cancellation after the FAILED CAS cannot preserve authority."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'failed-post-cas.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_FailedRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-failed-post-cas",
        session_id="session-failed-post-cas",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    original_mark_failed = runner._session_repo.mark_failed
    runner._session_repo.mark_failed = AsyncMock(
        return_value=Result.err(PersistenceError("failure store unavailable"))
    )

    try:
        first = await runner.execute_precreated_session(_seed(), tracker, parallel=False)
        assert first.is_err

        async def _commit_then_cancel(*args: object, **kwargs: object):
            committed = await original_mark_failed(*args, **kwargs)
            assert committed.is_ok
            raise asyncio.CancelledError

        runner._session_repo.mark_failed = AsyncMock(side_effect=_commit_then_cancel)
        with pytest.raises(asyncio.CancelledError):
            await runner.resume_session(tracker.session_id, _seed())

        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok and durable.value.status == SessionStatus.FAILED
        assert tracker.session_id not in runner._pending_lifecycle_intents
        assert not runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert not heartbeat.is_holder_alive(tracker.session_id)
        assert tracker.execution_id not in runner.active_sessions
    finally:
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_uow_rejects_terminal_write_for_live_process_local_owner(tmp_path) -> None:
    """UnitOfWork cannot bypass lifecycle cleanup for a retained paused owner."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'uow-live-owner.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-uow-live-owner",
        session_id="session-uow-live-owner",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    paused = await runner._session_repo.mark_paused(tracker.session_id, reason="retained pause")
    assert paused.is_ok and paused.value is True
    checkpoint_store = CheckpointStore(base_path=tmp_path / "uow-live-owner-checkpoints")
    checkpoint_store.initialize()
    uow = UnitOfWork(event_store, checkpoint_store)
    uow.add_event(
        create_session_completed_event(
            tracker.session_id,
            summary={"result": "must use owner"},
            messages_processed=1,
        )
    )

    try:
        committed = await uow.commit()

        assert committed.is_err
        assert committed.error.details["process_local_authority_live"] is True
        assert uow.pending_event_count == 1
        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok and durable.value.status == SessionStatus.PAUSED
        assert runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert heartbeat.is_holder_alive(tracker.session_id)
    finally:
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_post_cas_task_cancellation_retires_durable_terminal_owner(tmp_path) -> None:
    """Cancellation during projection cannot leave COMPLETED with live authority."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'post-cas-cancel.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(
        _SuccessfulRuntime(),
        event_store,
        MagicMock(),
        fat_harness_mode=False,
    )
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-post-cas-cancel",
        session_id="session-post-cas-cancel",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    original_append = event_store.append

    async def _cancel_terminal_projection(event: object):
        if getattr(event, "type", None) == "execution.terminal":
            raise asyncio.CancelledError
        return await original_append(event)  # type: ignore[arg-type]

    try:
        with patch.object(
            event_store, "append", AsyncMock(side_effect=_cancel_terminal_projection)
        ):
            with pytest.raises(asyncio.CancelledError):
                await runner.execute_precreated_session(_seed(), tracker, parallel=False)

        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok
        assert durable.value.status == SessionStatus.COMPLETED
        assert not runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert not heartbeat.is_holder_alive(tracker.session_id)
        assert tracker.execution_id not in runner.active_sessions
    finally:
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_repeated_post_cas_cancellation_drains_terminal_cleanup(tmp_path) -> None:
    """A second task cancellation cannot interrupt terminal reconciliation."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'post-cas-double-cancel.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(
        _SuccessfulRuntime(),
        event_store,
        MagicMock(),
        fat_harness_mode=False,
    )
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-post-cas-double-cancel",
        session_id="session-post-cas-double-cancel",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    original_append = event_store.append
    original_reconstruct = runner._session_repo.reconstruct_session
    reconciliation_started = asyncio.Event()
    allow_reconciliation = asyncio.Event()

    async def _cancel_terminal_projection(event: object):
        if getattr(event, "type", None) == "execution.terminal":
            raise asyncio.CancelledError
        return await original_append(event)  # type: ignore[arg-type]

    async def _delayed_reconstruct(session_id: str):
        reconciliation_started.set()
        await allow_reconciliation.wait()
        return await original_reconstruct(session_id)

    try:
        with (
            patch.object(event_store, "append", AsyncMock(side_effect=_cancel_terminal_projection)),
            patch.object(
                runner._session_repo,
                "reconstruct_session",
                AsyncMock(side_effect=_delayed_reconstruct),
            ),
        ):
            execution = asyncio.create_task(
                runner.execute_precreated_session(_seed(), tracker, parallel=False)
            )
            await reconciliation_started.wait()
            execution.cancel()
            allow_reconciliation.set()
            with pytest.raises(asyncio.CancelledError):
                await execution

        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok
        assert durable.value.status == SessionStatus.COMPLETED
        assert not runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert not heartbeat.is_holder_alive(tracker.session_id)
        assert tracker.execution_id not in runner.active_sessions
    finally:
        allow_reconciliation.set()
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_prepare_terminal_persistence_retries_before_retiring_authority(tmp_path) -> None:
    """Preparation uses bounded recovery before withdrawing its published lease."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'prepare-retry.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    original_mark_failed = runner._session_repo.mark_failed
    runner._session_repo.track_progress = AsyncMock(
        return_value=Result.err(PersistenceError("initial progress unavailable"))
    )
    terminal_attempts = 0

    async def _mark_failed_with_retry(*args: object, **kwargs: object):
        nonlocal terminal_attempts
        terminal_attempts += 1
        if terminal_attempts == 1:
            return Result.err(PersistenceError("transient terminal failure"))
        return await original_mark_failed(*args, **kwargs)

    runner._session_repo.mark_failed = AsyncMock(side_effect=_mark_failed_with_retry)
    try:
        result = await runner.prepare_session(
            _seed(),
            execution_id="exec-prepare-terminal-retry",
            session_id="session-prepare-terminal-retry",
        )

        assert result.is_err
        durable = await SessionRepository(event_store).reconstruct_session(
            "session-prepare-terminal-retry"
        )
        assert durable.is_ok
        assert durable.value.status == SessionStatus.FAILED
        assert terminal_attempts == 2
        assert not heartbeat.is_holder_alive("session-prepare-terminal-retry")
    finally:
        runner._retire_process_local_authority(
            session_id="session-prepare-terminal-retry",
            execution_id="exec-prepare-terminal-retry",
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_prepare_rejects_reused_terminal_session_id(tmp_path) -> None:
    """A caller-supplied session ID has one immutable start identity."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'session-id-conflict.db'}")
    await event_store.initialize()
    original_runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    colliding_runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    session_id = "session-immutable-start"
    original_execution_id = "exec-immutable-original"
    collision_execution_id = "exec-immutable-collision"
    original = await original_runner.prepare_session(
        _seed(),
        execution_id=original_execution_id,
        session_id=session_id,
    )
    assert original.is_ok
    completed = await original_runner._session_repo.mark_completed(session_id, {"done": True})
    assert completed.is_ok
    original_runner._retire_process_local_authority(
        session_id=session_id,
        execution_id=original_execution_id,
    )

    try:
        collision = await colliding_runner.prepare_session(
            _seed(),
            execution_id=collision_execution_id,
            session_id=session_id,
        )

        assert collision.is_err
        assert collision.error.details["resume_blocked"] == "session_id_conflict"
        durable = await SessionRepository(event_store).reconstruct_session(session_id)
        assert durable.is_ok
        assert durable.value.execution_id == original_execution_id
        assert durable.value.status == SessionStatus.COMPLETED
        events = await event_store.replay("session", session_id)
        assert [event.type for event in events].count("orchestrator.session.started") == 1
        assert (session_id, collision_execution_id) not in (
            colliding_runner._process_local_authorities
        )
        assert not heartbeat.is_holder_alive(session_id)
    finally:
        original_runner._retire_process_local_authority(
            session_id=session_id,
            execution_id=original_execution_id,
        )
        colliding_runner._retire_process_local_authority(
            session_id=session_id,
            execution_id=collision_execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_prepare_collision_preserves_existing_owner_workspace_lock(tmp_path) -> None:
    """A rejected preparation cannot release another live owner's workspace."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'live-owner-collision.db'}")
    await event_store.initialize()
    original_runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    colliding_runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    workspace = TaskWorkspace(
        durable_id="live-owner-collision",
        repo_root="/tmp/repo",
        repo_name="repo",
        original_cwd="/tmp/repo",
        effective_cwd="/tmp/repo",
        worktree_path="/tmp/repo",
        branch="test/live-owner-collision",
        lock_path="/tmp/live-owner-collision.lock",
    )
    original_runner._task_workspace = workspace
    colliding_runner._task_workspace = workspace
    session_id = "session-live-owner-collision"
    original_execution_id = "exec-live-owner-original"
    collision_execution_id = "exec-live-owner-collision"
    original = await original_runner.prepare_session(
        _seed(),
        execution_id=original_execution_id,
        session_id=session_id,
    )
    assert original.is_ok

    try:
        with patch("ouroboros.orchestrator.runner.release_lock") as release_lock_mock:
            collision = await colliding_runner.prepare_session(
                _seed(),
                execution_id=collision_execution_id,
                session_id=session_id,
            )

        assert collision.is_err
        assert (session_id, original_execution_id) in original_runner._process_local_authorities
        assert (session_id, collision_execution_id) not in (
            colliding_runner._process_local_authorities
        )
        assert heartbeat.is_holder_alive(session_id)
        release_lock_mock.assert_not_called()
    finally:
        original_runner._retire_process_local_authority(
            session_id=session_id,
            execution_id=original_execution_id,
        )
        colliding_runner._retire_process_local_authority(
            session_id=session_id,
            execution_id=collision_execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_exact_identity_prepare_collision_preserves_original_generation(tmp_path) -> None:
    """A failed duplicate generation cannot retire the existing exact owner."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'exact-owner-collision.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    runner._task_workspace = TaskWorkspace(
        durable_id="exact-owner-collision",
        repo_root="/tmp/repo",
        repo_name="repo",
        original_cwd="/tmp/repo",
        effective_cwd="/tmp/repo",
        worktree_path="/tmp/repo",
        branch="test/exact-owner-collision",
        lock_path="/tmp/exact-owner-collision.lock",
    )
    session_id = "session-exact-owner-collision"
    execution_id = "exec-exact-owner-collision"
    original = await runner.prepare_session(
        _seed(),
        execution_id=execution_id,
        session_id=session_id,
    )
    assert original.is_ok
    original_generation = runner._process_local_authorities[(session_id, execution_id)]

    try:
        with patch("ouroboros.orchestrator.runner.release_lock") as release_lock_mock:
            collision = await runner.prepare_session(
                _seed(),
                execution_id=execution_id,
                session_id=session_id,
            )

        assert collision.is_err
        assert runner._process_local_authorities[(session_id, execution_id)] is original_generation
        assert runner._task_workspace_users == {(session_id, execution_id)}
        assert runner._task_workspace_lock_held is True
        assert heartbeat.is_holder_alive(session_id)
        release_lock_mock.assert_not_called()
    finally:
        runner._retire_process_local_authority(
            session_id=session_id,
            execution_id=execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_workspace_lock_releases_after_last_runner_session_owner(tmp_path) -> None:
    """One terminal session cannot release a workspace used by another session."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'workspace-users.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    runner._task_workspace = TaskWorkspace(
        durable_id="workspace-users",
        repo_root="/tmp/repo",
        repo_name="repo",
        original_cwd="/tmp/repo",
        effective_cwd="/tmp/repo",
        worktree_path="/tmp/repo",
        branch="test/workspace-users",
        lock_path="/tmp/workspace-users.lock",
    )
    first = await runner.prepare_session(
        _seed(),
        execution_id="exec-workspace-first",
        session_id="session-workspace-first",
    )
    second = await runner.prepare_session(
        _seed(),
        execution_id="exec-workspace-second",
        session_id="session-workspace-second",
    )
    assert first.is_ok
    assert second.is_ok

    try:
        with patch("ouroboros.orchestrator.runner.release_lock") as release_lock_mock:
            runner._cleanup_pre_execution_state(
                "exec-workspace-first",
                "session-workspace-first",
                session_registered=False,
            )
            release_lock_mock.assert_not_called()
            assert runner._task_workspace_users == {
                ("session-workspace-second", "exec-workspace-second")
            }

            runner._cleanup_pre_execution_state(
                "exec-workspace-second",
                "session-workspace-second",
                session_registered=False,
            )
            release_lock_mock.assert_called_once_with("/tmp/workspace-users.lock")
            assert not runner._task_workspace_users

            runner._cleanup_pre_execution_state(
                "exec-workspace-second",
                "session-workspace-second",
                session_registered=False,
            )
            release_lock_mock.assert_called_once_with("/tmp/workspace-users.lock")
    finally:
        runner._retire_process_local_authority(
            session_id="session-workspace-first",
            execution_id="exec-workspace-first",
        )
        runner._retire_process_local_authority(
            session_id="session-workspace-second",
            execution_id="exec-workspace-second",
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_inflight_preparation_reserves_workspace_before_registration(tmp_path) -> None:
    """A session finishing during another contract build cannot release its lock."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'workspace-prepare-race.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    runner._task_workspace = TaskWorkspace(
        durable_id="workspace-prepare-race",
        repo_root="/tmp/repo",
        repo_name="repo",
        original_cwd="/tmp/repo",
        effective_cwd="/tmp/repo",
        worktree_path="/tmp/repo",
        branch="test/workspace-prepare-race",
        lock_path="/tmp/workspace-prepare-race.lock",
    )
    first = await runner.prepare_session(
        _seed(),
        execution_id="exec-workspace-prepare-first",
        session_id="session-workspace-prepare-first",
    )
    assert first.is_ok
    entered_build = Event()
    release_build = Event()
    original_build = runner._build_execution_contract

    def _blocked_build(**kwargs: object) -> dict[str, object]:
        entered_build.set()
        assert release_build.wait(timeout=2)
        return original_build(**kwargs)

    try:
        with (
            patch.object(runner, "_build_execution_contract", _blocked_build),
            patch("ouroboros.orchestrator.runner.release_lock") as release_lock_mock,
        ):
            second_task = asyncio.create_task(
                runner.prepare_session(
                    _seed(),
                    execution_id="exec-workspace-prepare-second",
                    session_id="session-workspace-prepare-second",
                )
            )
            assert await asyncio.to_thread(entered_build.wait, 1)

            runner._cleanup_pre_execution_state(
                "exec-workspace-prepare-first",
                "session-workspace-prepare-first",
                session_registered=False,
            )
            release_lock_mock.assert_not_called()
            assert runner._task_workspace_reservations

            release_build.set()
            second = await asyncio.wait_for(second_task, timeout=2)
            assert second.is_ok
            assert not runner._task_workspace_reservations

            runner._cleanup_pre_execution_state(
                "exec-workspace-prepare-second",
                "session-workspace-prepare-second",
                session_registered=False,
            )
            release_lock_mock.assert_called_once_with("/tmp/workspace-prepare-race.lock")
    finally:
        release_build.set()
        runner._retire_process_local_authority(
            session_id="session-workspace-prepare-first",
            execution_id="exec-workspace-prepare-first",
        )
        runner._retire_process_local_authority(
            session_id="session-workspace-prepare-second",
            execution_id="exec-workspace-prepare-second",
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_resume_handoff_reserves_workspace_before_reclaim(tmp_path) -> None:
    """Another session cannot release the lock during retained-workspace restore."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'workspace-resume-race.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    workspace = TaskWorkspace(
        durable_id="workspace-resume-race",
        repo_root="/tmp/repo",
        repo_name="repo",
        original_cwd="/tmp/repo",
        effective_cwd="/tmp/repo",
        worktree_path="/tmp/repo",
        branch="test/workspace-resume-race",
        lock_path="/tmp/workspace-resume-race.lock",
    )
    runner._task_workspace = workspace
    first = await runner.prepare_session(
        _seed(),
        execution_id="exec-workspace-resume-first",
        session_id="session-workspace-resume-first",
    )
    second = await runner.prepare_session(
        _seed(),
        execution_id="exec-workspace-resume-second",
        session_id="session-workspace-resume-second",
    )
    assert first.is_ok
    assert second.is_ok
    runner._release_task_workspace_for_identity(
        session_id=first.value.session_id,
        execution_id=first.value.execution_id,
    )
    reservation = runner._reserve_task_workspace()
    assert reservation is not None

    try:
        with patch("ouroboros.orchestrator.runner.release_lock") as release_lock_mock:
            runner._cleanup_pre_execution_state(
                second.value.execution_id,
                second.value.session_id,
                session_registered=False,
            )
            release_lock_mock.assert_not_called()

            runner._task_workspace = workspace
            contract = first.value.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
            generation, already_claimed = runner._claim_process_local_authority_generation(
                first.value.session_id,
                first.value.execution_id,
                contract,
            )
            assert generation is not None
            assert already_claimed is False
            runner._release_task_workspace_reservation(reservation)
            release_lock_mock.assert_not_called()

            runner._cleanup_pre_execution_state(
                first.value.execution_id,
                first.value.session_id,
                session_registered=False,
            )
            release_lock_mock.assert_called_once_with("/tmp/workspace-resume-race.lock")
    finally:
        runner._release_task_workspace_reservation(reservation)
        runner._retire_process_local_authority(
            session_id=first.value.session_id,
            execution_id=first.value.execution_id,
        )
        runner._retire_process_local_authority(
            session_id=second.value.session_id,
            execution_id=second.value.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_direct_cancel_of_already_terminal_owner_releases_workspace(tmp_path) -> None:
    """Terminal reconciliation through direct cancellation drains workspace use."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'terminal-cancel-workspace.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    runner._task_workspace = TaskWorkspace(
        durable_id="terminal-cancel-workspace",
        repo_root="/tmp/repo",
        repo_name="repo",
        original_cwd="/tmp/repo",
        effective_cwd="/tmp/repo",
        worktree_path="/tmp/repo",
        branch="test/terminal-cancel-workspace",
        lock_path="/tmp/terminal-cancel-workspace.lock",
    )
    execution_id = "exec-terminal-cancel-workspace"
    session_id = "session-terminal-cancel-workspace"
    prepared = await runner.prepare_session(
        _seed(),
        execution_id=execution_id,
        session_id=session_id,
    )
    assert prepared.is_ok
    terminal = await runner._session_repo.mark_completed(session_id, {"done": True})
    assert terminal.is_ok

    try:
        with patch("ouroboros.orchestrator.runner.release_lock") as release_lock_mock:
            cancelled = await runner.cancel_execution(execution_id)

        assert cancelled.is_ok
        assert cancelled.value["status"] == "already_terminal"
        assert (session_id, execution_id) not in runner._process_local_authorities
        assert not runner._task_workspace_users
        assert runner._task_workspace_lock_held is False
        release_lock_mock.assert_called_once_with("/tmp/terminal-cancel-workspace.lock")
    finally:
        runner._retire_process_local_authority(
            session_id=session_id,
            execution_id=execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_prepare_rejects_terminal_only_session_id(tmp_path) -> None:
    """Terminal-only legacy history cannot acquire a new live execution owner."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'terminal-only-conflict.db'}")
    await event_store.initialize()
    session_id = "session-terminal-only-conflict"
    runtime = _CountingRuntime()
    runner = OrchestratorRunner(runtime, event_store, MagicMock())
    await event_store.append(
        create_session_completed_event(
            session_id,
            summary={"legacy": "terminal-only"},
            messages_processed=0,
        )
    )

    try:
        collision = await runner.prepare_session(
            _seed(),
            execution_id="exec-terminal-only-collision",
            session_id=session_id,
        )

        assert collision.is_err
        assert collision.error.details["resume_blocked"] == "session_id_conflict"
        assert runtime.execute_calls == 0
        events = await event_store.replay("session", session_id)
        assert [event.type for event in events] == ["orchestrator.session.completed"]
        assert not heartbeat.is_holder_alive(session_id)
    finally:
        runner._retire_process_local_authority(
            session_id=session_id,
            execution_id="exec-terminal-only-collision",
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_failure_cleanup_commit_then_cancel_reconciles_owner(tmp_path) -> None:
    """Generic failure persistence drains ownership after an interrupted CAS response."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'failure-cleanup-cas.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-failure-cleanup-cas",
        session_id="session-failure-cleanup-cas",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    runner._register_session(tracker.execution_id, tracker.session_id)
    original_mark_failed = runner._session_repo.mark_failed

    async def _commit_then_cancel(*args: object, **kwargs: object):
        committed = await original_mark_failed(*args, **kwargs)
        assert committed.is_ok
        raise asyncio.CancelledError

    try:
        runner._session_repo.mark_failed = AsyncMock(side_effect=_commit_then_cancel)
        with pytest.raises(asyncio.CancelledError):
            await runner._persist_failure_and_cleanup(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
                error=RuntimeError("runtime failed"),
            )

        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok and durable.value.status == SessionStatus.FAILED
        assert not runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert not heartbeat.is_holder_alive(tracker.session_id)
        assert tracker.execution_id not in runner.active_sessions
    finally:
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        runner._unregister_session(tracker.execution_id, tracker.session_id)
        await event_store.close()


@pytest.mark.asyncio
async def test_lost_authority_commit_then_cancel_reconciles_owner(tmp_path) -> None:
    """Lost-authority terminalization cannot leave a committed FAILED owner live."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'lost-authority-cas.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-lost-authority-cas",
        session_id="session-lost-authority-cas",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    runner._register_session(tracker.execution_id, tracker.session_id)
    original_mark_failed = runner._session_repo.mark_failed_if_active

    async def _commit_then_cancel(*args: object, **kwargs: object):
        committed = await original_mark_failed(*args, **kwargs)
        assert committed.is_ok and committed.value is True
        raise asyncio.CancelledError

    try:
        runner._session_repo.mark_failed_if_active = AsyncMock(side_effect=_commit_then_cancel)
        with pytest.raises(asyncio.CancelledError):
            await runner._mark_process_local_resume_unavailable(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
            )

        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok and durable.value.status == SessionStatus.FAILED
        assert not runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert not heartbeat.is_holder_alive(tracker.session_id)
        assert tracker.execution_id not in runner.active_sessions
    finally:
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        runner._unregister_session(tracker.execution_id, tracker.session_id)
        await event_store.close()


@pytest.mark.asyncio
async def test_preparation_failure_commit_then_cancel_reconciles_owner(tmp_path) -> None:
    """Preparation cleanup reconciles FAILED before propagating cancellation."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'prepare-failure-cas.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    runner._session_repo.track_progress = AsyncMock(
        return_value=Result.err(PersistenceError("initial progress unavailable"))
    )
    original_mark_failed = runner._session_repo.mark_failed

    async def _commit_then_cancel(*args: object, **kwargs: object):
        committed = await original_mark_failed(*args, **kwargs)
        assert committed.is_ok
        raise asyncio.CancelledError

    runner._session_repo.mark_failed = AsyncMock(side_effect=_commit_then_cancel)
    session_id = "session-prepare-failure-cas"
    execution_id = "exec-prepare-failure-cas"
    try:
        with pytest.raises(asyncio.CancelledError):
            await runner.prepare_session(
                _seed(),
                execution_id=execution_id,
                session_id=session_id,
            )

        durable = await SessionRepository(event_store).reconstruct_session(session_id)
        assert durable.is_ok and durable.value.status == SessionStatus.FAILED
        assert (session_id, execution_id) not in runner._process_local_authorities
        assert not heartbeat.is_holder_alive(session_id)
        assert execution_id not in runner.active_sessions
    finally:
        runner._retire_process_local_authority(
            session_id=session_id,
            execution_id=execution_id,
        )
        runner._unregister_session(execution_id, session_id)
        await event_store.close()


@pytest.mark.asyncio
async def test_cooperative_cancellation_persists_terminal_before_retiring_authority() -> None:
    """The live owner remains observable until durable cancellation succeeds."""
    runner = _runner()
    tracker = await _prepare(
        runner,
        session_id="session-cancel-terminal-order",
        execution_id="exec-cancel-terminal-order",
    )
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    runner._register_session(tracker.execution_id, tracker.session_id)
    runner._session_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))
    runner._report_frugality_retrospective = AsyncMock()

    async def mark_cancelled(
        _: str,
        *,
        reason: str,
        cancelled_by: str,
    ) -> Result[None, object]:
        assert runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert heartbeat.is_holder_alive(tracker.session_id)
        assert reason == "CLI requested a careful stop"
        assert cancelled_by == "user"
        return Result.ok(None)

    runner._session_repo.mark_cancelled = mark_cancelled
    await request_cancellation(
        tracker.session_id,
        reason="CLI requested a careful stop",
        cancelled_by="user",
    )

    result = await runner._handle_cancellation(
        session_id=tracker.session_id,
        execution_id=tracker.execution_id,
        messages_processed=0,
        start_time=tracker.start_time,
    )

    assert result.is_ok
    assert not runner._has_live_process_local_authority(
        tracker.session_id,
        tracker.execution_id,
        contract,
    )
    assert not heartbeat.is_holder_alive(tracker.session_id)


@pytest.mark.asyncio
async def test_repeated_cancellation_after_cancel_cas_drains_owner_cleanup() -> None:
    """Repeated cancellation cannot interrupt post-CAS owner reconciliation."""
    runner = _runner()
    tracker = await _prepare(
        runner,
        session_id="session-cancel-post-cas-shield",
        execution_id="exec-cancel-post-cas-shield",
    )
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    runner._register_session(tracker.execution_id, tracker.session_id)
    runner._session_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))
    runner._session_repo.mark_cancelled = AsyncMock(return_value=Result.ok(True))
    runner._report_frugality_retrospective = AsyncMock()
    clear_started = asyncio.Event()
    allow_clear = asyncio.Event()
    original_clear = clear_cancellation

    async def _blocked_clear(session_id: str) -> None:
        clear_started.set()
        await allow_clear.wait()
        await original_clear(session_id)

    await request_cancellation(tracker.session_id)
    try:
        with patch("ouroboros.orchestrator.runner.clear_cancellation", _blocked_clear):
            cancellation = asyncio.create_task(
                runner._handle_cancellation(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                    messages_processed=0,
                    start_time=tracker.start_time,
                )
            )
            await clear_started.wait()
            cancellation.cancel()
            cancellation.cancel()
            allow_clear.set()
            result = await asyncio.wait_for(cancellation, timeout=2)

        assert result.is_ok
        assert not runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert not heartbeat.is_holder_alive(tracker.session_id)
        assert tracker.execution_id not in runner.active_sessions
        assert not await is_cancellation_requested(tracker.session_id)
    finally:
        allow_clear.set()
        await clear_cancellation(tracker.session_id)
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        runner._unregister_session(tracker.execution_id, tracker.session_id)


@pytest.mark.asyncio
async def test_active_runner_cancel_commit_then_cancel_reconciles_owner(tmp_path) -> None:
    """Cancellation at the owning runner CAS cannot skip terminal cleanup."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'runner-cancel-cas.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-runner-cancel-cas",
        session_id="session-runner-cancel-cas",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    generation, already_claimed = runner._claim_process_local_authority_generation(
        tracker.session_id,
        tracker.execution_id,
        contract,
    )
    assert generation is not None and already_claimed is False
    runner._register_session(tracker.execution_id, tracker.session_id)
    original_mark_cancelled = runner._session_repo.mark_cancelled

    async def _commit_then_cancel(*args: object, **kwargs: object):
        committed = await original_mark_cancelled(*args, **kwargs)
        assert committed.is_ok
        raise asyncio.CancelledError

    await request_cancellation(
        tracker.session_id,
        reason="cancel at owner CAS",
        cancelled_by="mcp_tool",
    )
    try:
        runner._session_repo.mark_cancelled = AsyncMock(side_effect=_commit_then_cancel)
        with pytest.raises(asyncio.CancelledError):
            await runner._handle_cancellation(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
                messages_processed=0,
                start_time=tracker.start_time,
            )

        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok and durable.value.status == SessionStatus.CANCELLED
        assert not runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert not heartbeat.is_holder_alive(tracker.session_id)
        assert tracker.execution_id not in runner.active_sessions
        assert not await is_cancellation_requested(tracker.session_id)
    finally:
        await clear_cancellation(tracker.session_id)
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        runner._unregister_session(tracker.execution_id, tracker.session_id)
        await event_store.close()


@pytest.mark.asyncio
async def test_cooperative_cancellation_retains_authority_when_terminal_write_fails() -> None:
    """A failed durable cancellation keeps authority but releases its dead route."""
    runner = _runner()
    tracker = await _prepare(
        runner,
        session_id="session-cancel-terminal-failure",
        execution_id="exec-cancel-terminal-failure",
    )
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    runner._register_session(tracker.execution_id, tracker.session_id)
    runner._session_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))
    runner._session_repo.mark_cancelled = AsyncMock(
        return_value=Result.err(PersistenceError("durable cancellation unavailable"))
    )
    generation, already_claimed = runner._claim_process_local_authority_generation(
        tracker.session_id,
        tracker.execution_id,
        contract,
    )
    assert generation is not None
    assert already_claimed is False
    await request_cancellation(tracker.session_id)

    try:
        result = await runner._handle_cancellation(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
            messages_processed=0,
            start_time=tracker.start_time,
        )

        assert result.is_err
        assert result.error.message == (
            "Failed to persist cancellation; process-local authority remains live"
        )
        assert runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert heartbeat.is_holder_alive(tracker.session_id)
        assert tracker.execution_id not in runner.active_sessions
        assert await is_cancellation_requested(tracker.session_id)
        assert result.error.details["resume_blocked"] == "cancellation_persistence_pending"
        retry_generation, retry_already_claimed = runner._claim_process_local_authority_generation(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert retry_generation is not None
        assert retry_already_claimed is False
        runner._release_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
    finally:
        await clear_cancellation(tracker.session_id)
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        runner._unregister_session(tracker.execution_id, tracker.session_id)


@pytest.mark.asyncio
async def test_external_terminalization_retires_retained_owner_and_store() -> None:
    """A terminal record from another MCP surface drains the local owner atomically."""
    runner = _runner()
    tracker = await _prepare(
        runner,
        session_id="session-external-terminal-cleanup",
        execution_id="exec-external-terminal-cleanup",
    )
    paused_tracker = _paused(tracker)
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    handler = ExecuteSeedHandler(event_store=_HandlerEventStore())
    handler._remember_process_local_owner(
        paused_tracker,
        runner,
        owned_event_store=runner._event_store,
    )

    try:
        retired, claimed = await _retire_process_local_authority_after_terminal_persistence(
            tracker.session_id,
            tracker.execution_id,
            contract["foundation_a_authority"],
        )

        assert retired is True
        assert claimed is False
        assert not runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert not heartbeat.is_holder_alive(tracker.session_id)
        assert (tracker.session_id, tracker.execution_id) not in runner._process_local_authorities
        assert tracker.session_id not in handler._process_local_resume_owners
        assert tracker.session_id not in handler._process_local_owned_event_stores
        runner._event_store.close.assert_awaited_once()
    finally:
        handler._process_local_resume_owners.clear()
        handler._process_local_owned_event_stores.clear()
        handler._process_local_resume_handoffs.clear()
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )


@pytest.mark.asyncio
async def test_unstarted_background_cleanup_terminalizes_and_drains_process_local_owner(
    tmp_path,
) -> None:
    """The done-callback cleanup leaves no live owner when a task never starts."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'unstarted-background.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-unstarted-background",
        session_id="session-unstarted-background",
    )
    assert prepared.is_ok
    tracker = prepared.value
    handler = ExecuteSeedHandler(event_store=event_store)
    handler._remember_process_local_owner(tracker, runner)

    try:
        await handler._cleanup_unstarted_process_local_background_task(
            tracker=tracker,
            runner=runner,
            workspace=None,
            owned_event_store=None,
            retained_resume_handoff=False,
        )

        terminal = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert terminal.is_ok
        assert terminal.value.status == SessionStatus.FAILED
        assert not runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY],
        )
        assert not heartbeat.is_holder_alive(tracker.session_id)
        assert tracker.session_id not in handler._process_local_resume_owners
        assert tracker.session_id not in handler._process_local_owned_event_stores
    finally:
        handler._process_local_resume_owners.clear()
        handler._process_local_owned_event_stores.clear()
        handler._process_local_resume_handoffs.clear()
        await clear_cancellation(tracker.session_id)
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_unstarted_background_terminal_failure_retains_owner_and_store(tmp_path) -> None:
    """Unstarted cleanup cannot evict a RUNNING owner when FAILED will not persist."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'unstarted-pending.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-unstarted-pending",
        session_id="session-unstarted-pending",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    handler = ExecuteSeedHandler(event_store=event_store)
    handler._remember_process_local_owner(
        tracker,
        runner,
        owned_event_store=event_store,
    )
    runner._session_repo.mark_failed_if_active = AsyncMock(
        return_value=Result.err(PersistenceError("terminal store unavailable"))
    )

    try:
        await handler._cleanup_unstarted_process_local_background_task(
            tracker=tracker,
            runner=runner,
            workspace=None,
            owned_event_store=event_store,
            retained_resume_handoff=False,
        )

        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok
        assert durable.value.status == SessionStatus.RUNNING
        assert runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert heartbeat.is_holder_alive(tracker.session_id)
        assert handler._process_local_resume_owners[tracker.session_id] is runner
        assert handler._process_local_owned_event_stores[tracker.session_id] is event_store
        assert runner._session_repo.mark_failed_if_active.await_count == 3
    finally:
        handler._process_local_resume_owners.clear()
        handler._process_local_owned_event_stores.clear()
        handler._process_local_resume_handoffs.clear()
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_prepare_terminal_pending_keeps_handler_owned_store_open(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The outer MCP finally cannot close a store retained for exact-owner retry."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'prepare-owned-store.db'}")
    await event_store.initialize()
    handler = ExecuteSeedHandler(agent_runtime_backend="process-local-test")
    original_prepare = OrchestratorRunner.prepare_session
    close_mock = AsyncMock()

    async def _prepare_with_terminal_failure(
        runner: OrchestratorRunner,
        *args: object,
        **kwargs: object,
    ):
        runner._session_repo.track_progress = AsyncMock(
            return_value=Result.err(PersistenceError("initial progress unavailable"))
        )
        runner._session_repo.mark_failed = AsyncMock(
            return_value=Result.err(PersistenceError("terminal store unavailable"))
        )
        return await original_prepare(runner, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "ouroboros.mcp.tools.execution_handlers.create_agent_runtime",
        lambda **_kwargs: _CountingRuntime(),
    )
    monkeypatch.setattr(
        "ouroboros.mcp.tools.execution_handlers.EventStore",
        lambda: event_store,
    )

    retained_runner: OrchestratorRunner | None = None
    session_id: str | None = None
    try:
        with (
            patch.object(OrchestratorRunner, "prepare_session", _prepare_with_terminal_failure),
            patch.object(event_store, "close", close_mock),
        ):
            result = await handler.handle(
                {
                    "seed_content": yaml.safe_dump(_seed().to_dict()),
                    "skip_qa": True,
                    "use_worktree": False,
                }
            )

            assert result.is_err
            assert result.error.details["resume_blocked"] == "terminal_persistence_pending"
            session_id = result.error.details["session_id"]
            retained_runner = handler._process_local_resume_owners[session_id]
            assert handler._process_local_owned_event_stores[session_id] is event_store
            close_mock.assert_not_awaited()
            durable = await SessionRepository(event_store).reconstruct_session(session_id)
            assert durable.is_ok
            assert durable.value.status == SessionStatus.RUNNING
            assert heartbeat.is_holder_alive(session_id)
    finally:
        if retained_runner is not None and session_id is not None:
            retained_runner._retire_process_local_authority(
                session_id=session_id,
                execution_id=next(
                    execution_id
                    for stored_session_id, execution_id in retained_runner._process_local_authorities
                    if stored_session_id == session_id
                ),
            )
        handler._process_local_resume_owners.clear()
        handler._process_local_owned_event_stores.clear()
        handler._process_local_resume_handoffs.clear()
        await event_store.close()


@pytest.mark.asyncio
async def test_prepare_terminal_pending_retains_owner_when_reconstruction_is_unreadable(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ambiguous preparation keeps a callable owner/store without a durable read."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'prepare-unreadable.db'}")
    await event_store.initialize()
    handler = ExecuteSeedHandler(agent_runtime_backend="process-local-test")
    original_prepare = OrchestratorRunner.prepare_session
    close_mock = AsyncMock()

    async def _prepare_with_terminal_failure(
        runner: OrchestratorRunner,
        *args: object,
        **kwargs: object,
    ):
        runner._session_repo.track_progress = AsyncMock(
            return_value=Result.err(PersistenceError("initial progress unavailable"))
        )
        runner._session_repo.mark_failed = AsyncMock(
            return_value=Result.err(PersistenceError("terminal store unavailable"))
        )
        return await original_prepare(runner, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "ouroboros.mcp.tools.execution_handlers.create_agent_runtime",
        lambda **_kwargs: _CountingRuntime(),
    )
    monkeypatch.setattr(
        "ouroboros.mcp.tools.execution_handlers.EventStore",
        lambda: event_store,
    )

    retained_runner: OrchestratorRunner | None = None
    session_id: str | None = None
    try:
        with (
            patch.object(OrchestratorRunner, "prepare_session", _prepare_with_terminal_failure),
            patch.object(event_store, "close", close_mock),
            patch.object(
                SessionRepository,
                "reconstruct_session",
                AsyncMock(side_effect=PersistenceError("session reconstruction unavailable")),
            ) as reconstruct_mock,
        ):
            result = await handler.handle(
                {
                    "seed_content": yaml.safe_dump(_seed().to_dict()),
                    "skip_qa": True,
                    "use_worktree": False,
                }
            )

            assert result.is_err
            assert result.error.details["resume_blocked"] == "terminal_persistence_pending"
            session_id = result.error.details["session_id"]
            retained_runner = handler._process_local_resume_owners[session_id]
            assert handler._process_local_owned_event_stores[session_id] is event_store
            close_mock.assert_not_awaited()
            reconstruct_mock.assert_not_awaited()
            events = await event_store.replay("session", session_id)
            assert [event.type for event in events] == ["orchestrator.session.started"]
            assert heartbeat.is_holder_alive(session_id)
    finally:
        if retained_runner is not None and session_id is not None:
            retained_runner._retire_process_local_authority(
                session_id=session_id,
                execution_id=next(
                    execution_id
                    for stored_session_id, execution_id in retained_runner._process_local_authorities
                    if stored_session_id == session_id
                ),
            )
        handler._process_local_resume_owners.clear()
        handler._process_local_owned_event_stores.clear()
        handler._process_local_resume_handoffs.clear()
        await event_store.close()


@pytest.mark.asyncio
async def test_cancelled_before_first_background_turn_runs_process_local_cleanup(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The direct execute-seed done callback covers a never-entered coroutine."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'cancel-before-start.db'}")
    await event_store.initialize()
    handler = ExecuteSeedHandler(
        event_store=event_store,
        agent_runtime_backend="process-local-test",
    )
    original_create_task = asyncio.create_task
    scheduled_tasks: list[asyncio.Task[object]] = []

    class _ExecuteHandlerAsyncio:
        def __getattr__(self, name: str) -> object:
            return getattr(asyncio, name)

        def create_task(
            self,
            coroutine: object,
            *args: object,
            **kwargs: object,
        ) -> asyncio.Task[object]:
            task = original_create_task(coroutine, *args, **kwargs)  # type: ignore[arg-type]
            scheduled_tasks.append(task)
            if len(scheduled_tasks) == 1:
                task.cancel()
            return task

    monkeypatch.setattr(
        "ouroboros.mcp.tools.execution_handlers.create_agent_runtime",
        lambda **_kwargs: _CountingRuntime(),
    )
    monkeypatch.setattr(
        "ouroboros.mcp.tools.execution_handlers.asyncio",
        _ExecuteHandlerAsyncio(),
    )

    try:
        launched = await handler.handle(
            {
                "seed_content": yaml.safe_dump(_seed().to_dict()),
                "skip_qa": True,
                "use_worktree": False,
            }
        )
        assert launched.is_ok
        session_id = launched.value.meta["session_id"]

        for _ in range(5):
            pending = tuple(handler._background_tasks)
            if not pending:
                break
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=2,
            )
            await asyncio.sleep(0)

        terminal = await SessionRepository(event_store).reconstruct_session(session_id)
        assert terminal.is_ok
        assert terminal.value.status == SessionStatus.FAILED
        assert not heartbeat.is_holder_alive(session_id)
        assert session_id not in handler._process_local_resume_owners
        assert session_id not in handler._process_local_owned_event_stores
    finally:
        for task in tuple(handler._background_tasks):
            task.cancel()
        if handler._background_tasks:
            await asyncio.gather(*handler._background_tasks, return_exceptions=True)
        await event_store.close()


@pytest.mark.asyncio
async def test_pending_cancellation_retries_before_resuming_effects() -> None:
    """A retained runner retries the durable cancellation instead of resuming RUNNING work."""
    runner = _runner()
    tracker = await _prepare(
        runner,
        session_id="session-cancellation-retry",
        execution_id="exec-cancellation-retry",
    )
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    runner._register_session(tracker.execution_id, tracker.session_id)
    runner._session_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))
    runner._session_repo.mark_cancelled = AsyncMock(
        side_effect=[
            Result.err(PersistenceError("first cancellation write fails")),
            Result.ok(None),
        ]
    )
    runner._report_frugality_retrospective = AsyncMock()
    generation, already_claimed = runner._claim_process_local_authority_generation(
        tracker.session_id,
        tracker.execution_id,
        contract,
    )
    assert generation is not None
    assert already_claimed is False
    await request_cancellation(tracker.session_id)

    try:
        first = await runner._handle_cancellation(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
            messages_processed=0,
            start_time=tracker.start_time,
        )
        assert first.is_err

        retried = await runner.resume_session(tracker.session_id, _seed())

        assert retried.is_ok
        assert runner._session_repo.mark_cancelled.await_count == 2
        assert runner._adapter.execute_calls == 0
        assert not runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert not heartbeat.is_holder_alive(tracker.session_id)
        assert not await is_cancellation_requested(tracker.session_id)
    finally:
        await clear_cancellation(tracker.session_id)
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        runner._unregister_session(tracker.execution_id, tracker.session_id)


@pytest.mark.asyncio
async def test_public_paused_cancellation_retires_retained_process_local_owner(
    tmp_path,
) -> None:
    """Public cancellation must not leave a paused retained runner live forever."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'paused-terminal.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-public-paused-terminal",
        session_id="session-public-paused-terminal",
    )
    assert prepared.is_ok
    tracker = prepared.value
    paused = await runner._session_repo.mark_paused(tracker.session_id, reason="test pause")
    assert paused.is_ok
    paused_tracker = (await runner._session_repo.reconstruct_session(tracker.session_id)).value
    handler = ExecuteSeedHandler(event_store=event_store)
    handler._remember_process_local_owner(paused_tracker, runner)

    try:
        result = await CancelExecutionHandler(event_store=event_store).handle(
            {
                "execution_id": tracker.execution_id,
                "reason": "public paused cancellation",
            }
        )

        assert result.is_ok
        assert result.value.meta["new_status"] == SessionStatus.CANCELLED.value
        terminal = await runner._session_repo.reconstruct_session(tracker.session_id)
        assert terminal.is_ok
        assert terminal.value.status == SessionStatus.CANCELLED
        assert not runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY],
        )
        assert not heartbeat.is_holder_alive(tracker.session_id)
        assert tracker.session_id not in handler._process_local_resume_owners
        assert tracker.session_id not in handler._process_local_owned_event_stores
    finally:
        handler._process_local_resume_owners.clear()
        handler._process_local_owned_event_stores.clear()
        handler._process_local_resume_handoffs.clear()
        await clear_cancellation(tracker.session_id)
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_stale_paused_resume_cannot_overwrite_public_cancellation_terminal(tmp_path) -> None:
    """A stale PAUSED snapshot must not turn a durable cancellation into FAILED."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'stale-terminal-race.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-stale-paused-terminal",
        session_id="session-stale-paused-terminal",
    )
    assert prepared.is_ok
    tracker = prepared.value
    paused = await runner._session_repo.mark_paused(tracker.session_id, reason="test pause")
    assert paused.is_ok
    stale_paused = (
        await SessionRepository(event_store).reconstruct_session(tracker.session_id)
    ).value

    try:
        cancellation = await CancelExecutionHandler(event_store=event_store).handle(
            {
                "execution_id": tracker.execution_id,
                "reason": "public cancellation wins the terminal race",
            }
        )
        assert cancellation.is_ok

        # Model a resume caller that reconstructed PAUSED before public
        # cancellation committed, then reaches authority recovery only after
        # the registry has retired the local generation.
        runner._session_repo.reconstruct_session = AsyncMock(return_value=Result.ok(stale_paused))
        resumed = await runner.resume_session(tracker.session_id, _seed())

        assert resumed.is_err
        assert resumed.error.details["resume_blocked"] == "process_local_resume_unavailable"
        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok
        assert durable.value.status == SessionStatus.CANCELLED
        session_events = await event_store.replay("session", tracker.session_id)
        assert [event.type for event in session_events].count("orchestrator.session.cancelled") == 1
        assert "orchestrator.session.failed" not in [event.type for event in session_events]
    finally:
        await clear_cancellation(tracker.session_id)
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_concurrent_conditional_terminal_writes_have_one_durable_winner(tmp_path) -> None:
    """The EventStore serializes competing terminal lifecycle transitions."""
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'terminal-cas-race.db'}"
    first_store = EventStore(database_url)
    second_store = EventStore(database_url)
    await first_store.initialize()
    await second_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), first_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-terminal-cas-race",
        session_id="session-terminal-cas-race",
    )
    assert prepared.is_ok
    tracker = prepared.value

    try:
        failed, cancelled = await asyncio.gather(
            SessionRepository(first_store).mark_failed_if_active(
                tracker.session_id,
                "concurrent failure",
            ),
            SessionRepository(second_store).mark_cancelled_if_active(
                tracker.session_id,
                "concurrent cancellation",
            ),
        )

        assert failed.is_ok
        assert cancelled.is_ok
        assert int(failed.value) + int(cancelled.value) == 1
        terminal_events = [
            event
            for event in await first_store.replay("session", tracker.session_id)
            if event.type
            in {
                "orchestrator.session.completed",
                "orchestrator.session.failed",
                "orchestrator.session.cancelled",
            }
        ]
        assert len(terminal_events) == 1
    finally:
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        await second_store.close()
        await first_store.close()


@pytest.mark.asyncio
async def test_raw_pre_effect_resume_cancellation_persists_before_authority_cleanup(
    tmp_path,
) -> None:
    """A raw task cancellation cannot convert a pending public cancel to FAILED."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'raw-pre-effect-cancel.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-raw-pre-effect-cancel",
        session_id="session-raw-pre-effect-cancel",
    )
    assert prepared.is_ok
    tracker = prepared.value
    paused = await runner._session_repo.mark_paused(tracker.session_id, reason="test pause")
    assert paused.is_ok
    entered_restore = Event()
    release_restore = Event()

    def _blocked_restore(*_: object, **__: object) -> bool:
        entered_restore.set()
        assert release_restore.wait(timeout=2)
        return False

    try:
        with patch.object(runner, "_restore_execution_contract", _blocked_restore):
            resume = asyncio.create_task(runner.resume_session(tracker.session_id, _seed()))
            assert await asyncio.to_thread(entered_restore.wait, 1)
            await request_cancellation(tracker.session_id)
            resume.cancel()
            result = await asyncio.wait_for(resume, timeout=2)

        assert result.is_ok
        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok
        assert durable.value.status == SessionStatus.CANCELLED
        assert not runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY],
        )
    finally:
        release_restore.set()
        await clear_cancellation(tracker.session_id)
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_public_running_cancellation_signals_claimed_process_local_owner(tmp_path) -> None:
    """A public cancel cannot terminalize underneath a worker with the effect claim."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'running-cancel.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-public-running-cancel",
        session_id="session-public-running-cancel",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    runner._register_session(tracker.execution_id, tracker.session_id)
    generation, already_claimed = runner._claim_process_local_authority_generation(
        tracker.session_id,
        tracker.execution_id,
        contract,
    )
    assert generation is not None
    assert already_claimed is False

    try:
        result = await CancelExecutionHandler(event_store=event_store).handle(
            {
                "execution_id": tracker.execution_id,
                "reason": "public running cancellation",
            }
        )

        assert result.is_ok
        assert result.value.meta["new_status"] == "cancellation_requested"
        assert result.value.meta["in_flight"] is True
        current = await runner._session_repo.reconstruct_session(tracker.session_id)
        assert current.is_ok
        assert current.value.status == SessionStatus.RUNNING
        assert runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert await is_cancellation_requested(tracker.session_id)
        request = await get_cancellation_request(tracker.session_id)
        assert request is not None
        assert request.reason == "public running cancellation"
        assert request.cancelled_by == "mcp_tool"
    finally:
        await clear_cancellation(tracker.session_id)
        runner._release_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        runner._unregister_session(tracker.execution_id, tracker.session_id)
        await event_store.close()


@pytest.mark.asyncio
async def test_claimed_owner_completion_racing_cancel_publication_clears_signal(
    tmp_path,
) -> None:
    """A late local signal cannot survive the owner's terminal winner."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'local-cancel-race.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-local-cancel-race",
        session_id="session-local-cancel-race",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    generation, already_claimed = runner._claim_process_local_authority_generation(
        tracker.session_id,
        tracker.execution_id,
        contract,
    )
    assert generation is not None
    assert already_claimed is False
    original_request_cancellation = request_cancellation

    async def _publish_after_completion(
        session_id: str,
        *,
        reason: str,
        cancelled_by: str,
    ) -> None:
        completed = await runner._session_repo.mark_completed(session_id, {"done": True})
        assert completed.is_ok
        runner._retire_process_local_authority(
            session_id=session_id,
            execution_id=tracker.execution_id,
        )
        await original_request_cancellation(
            session_id,
            reason=reason,
            cancelled_by=cancelled_by,
        )

    try:
        with patch(
            "ouroboros.orchestrator.runner.request_cancellation",
            _publish_after_completion,
        ):
            outcome = await request_process_local_cancellation(
                tracker,
                runner._session_repo,
                reason="racing local cancellation",
                cancelled_by="test",
            )

        assert outcome is not None
        assert outcome.disposition == ProcessLocalCancellationDisposition.ALREADY_TERMINAL
        assert not await is_cancellation_requested(tracker.session_id)
        assert not heartbeat.is_holder_alive(tracker.session_id)
        assert not runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
    finally:
        await clear_cancellation(tracker.session_id)
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_job_manager_cancel_does_not_terminalize_a_claimed_process_local_owner(
    tmp_path,
) -> None:
    """The job surface uses the same signal-only path below live effects."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'job-manager-running-cancel.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-job-manager-running-cancel",
        session_id="session-job-manager-running-cancel",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    generation, already_claimed = runner._claim_process_local_authority_generation(
        tracker.session_id,
        tracker.execution_id,
        contract,
    )
    assert generation is not None
    assert already_claimed is False
    manager = JobManager(event_store)
    started = asyncio.Event()
    runner_cancelled = asyncio.Event()

    async def _job_runner() -> MCPToolResult:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            runner_cancelled.set()
            raise
        return MCPToolResult(content=(MCPContentItem(type=ContentType.TEXT, text="late"),))

    try:
        job = await manager.start_job(
            job_type="process-local-cancel",
            initial_message="running",
            runner=_job_runner(),
            links=JobLinks(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
            ),
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        await manager.cancel_job(job.job_id)
        await asyncio.wait_for(runner_cancelled.wait(), timeout=1)

        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok
        assert durable.value.status == SessionStatus.RUNNING
        assert await is_cancellation_requested(tracker.session_id)
        assert runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert heartbeat.is_holder_alive(tracker.session_id)
        assert not await event_store.query_events(
            aggregate_id=tracker.execution_id,
            event_type="execution.terminal",
        )
    finally:
        await clear_cancellation(tracker.session_id)
        runner._release_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        tasks = [
            *manager._tasks.values(),
            *manager._runner_tasks.values(),
            *manager._monitors.values(),
        ]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await event_store.close()


@pytest.mark.asyncio
async def test_direct_runner_cancel_does_not_terminalize_a_claimed_process_local_owner(
    tmp_path,
) -> None:
    """The direct runner fallback cannot write beneath its own effect claim."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'direct-running-cancel.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-direct-running-cancel",
        session_id="session-direct-running-cancel",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    generation, already_claimed = runner._claim_process_local_authority_generation(
        tracker.session_id,
        tracker.execution_id,
        contract,
    )
    assert generation is not None
    assert already_claimed is False

    try:
        result = await runner.cancel_execution(tracker.execution_id)

        assert result.is_ok
        assert result.value["status"] == "cancellation_requested"
        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok
        assert durable.value.status == SessionStatus.RUNNING
        assert await is_cancellation_requested(tracker.session_id)
        assert runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
    finally:
        await clear_cancellation(tracker.session_id)
        runner._release_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_cli_cancel_does_not_terminalize_a_claimed_process_local_owner(tmp_path) -> None:
    """The CLI cancellation surface delegates live ownership to the runner."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'cli-running-cancel.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-cli-running-cancel",
        session_id="session-cli-running-cancel",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    generation, already_claimed = runner._claim_process_local_authority_generation(
        tracker.session_id,
        tracker.execution_id,
        contract,
    )
    assert generation is not None
    assert already_claimed is False

    try:
        assert (
            await _cancel_session(event_store, tracker.session_id)
            == ProcessLocalCancellationDisposition.CANCELLATION_REQUESTED
        )

        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok
        assert durable.value.status == SessionStatus.RUNNING
        assert await is_cancellation_requested(tracker.session_id)
        assert runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
    finally:
        await clear_cancellation(tracker.session_id)
        runner._release_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_public_cancellation_reservation_blocks_an_interleaved_resume_claim(tmp_path) -> None:
    """A paused owner cannot claim effects while public cancellation persists terminal state."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'cancel-race.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-cancel-reservation-race",
        session_id="session-cancel-reservation-race",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    paused = await runner._session_repo.mark_paused(tracker.session_id, reason="test pause")
    assert paused.is_ok
    cancel_handler = CancelExecutionHandler(event_store=event_store)
    original_mark_cancelled = cancel_handler._session_repo.mark_cancelled_if_active
    persistence_started = asyncio.Event()
    allow_persistence = asyncio.Event()

    async def _blocked_mark_cancelled(*args: object, **kwargs: object):
        persistence_started.set()
        await allow_persistence.wait()
        return await original_mark_cancelled(*args, **kwargs)

    try:
        with patch.object(
            cancel_handler._session_repo,
            "mark_cancelled_if_active",
            _blocked_mark_cancelled,
        ):
            cancellation = asyncio.create_task(
                cancel_handler.handle(
                    {
                        "execution_id": tracker.execution_id,
                        "reason": "reservation race regression",
                    }
                )
            )
            await persistence_started.wait()

            generation, already_claimed = runner._claim_process_local_authority_generation(
                tracker.session_id,
                tracker.execution_id,
                contract,
            )
            assert generation is None
            assert already_claimed is True

            allow_persistence.set()
            result = await cancellation

        assert result.is_ok
        assert not runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
    finally:
        await clear_cancellation(tracker.session_id)
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_cancelled_public_terminalization_releases_its_reservation(tmp_path) -> None:
    """Cancelling the cancel request cannot leave a paused owner permanently claimed."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'cancel-task-race.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-cancelled-public-terminalization",
        session_id="session-cancelled-public-terminalization",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    cancel_handler = CancelExecutionHandler(event_store=event_store)
    persistence_started = asyncio.Event()
    never_finish = asyncio.Event()

    async def _never_return(*_: object, **__: object) -> Result[None, PersistenceError]:
        persistence_started.set()
        await never_finish.wait()
        return Result.ok(None)

    try:
        with patch.object(
            cancel_handler._session_repo,
            "mark_cancelled_if_active",
            _never_return,
        ):
            cancellation = asyncio.create_task(
                cancel_handler.handle(
                    {
                        "execution_id": tracker.execution_id,
                        "reason": "cancel the canceller",
                    }
                )
            )
            await persistence_started.wait()
            cancellation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancellation

        generation, already_claimed = runner._claim_process_local_authority_generation(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert generation is not None
        assert already_claimed is False
        runner._release_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
    finally:
        await clear_cancellation(tracker.session_id)
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_public_cancel_signals_a_live_foreign_process_owner(tmp_path) -> None:
    """A non-owning process publishes a request without terminalizing the owner."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'foreign-cancel.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-foreign-public-cancel",
        session_id="session-foreign-public-cancel",
    )
    assert prepared.is_ok
    tracker = prepared.value
    cancel_handler = CancelExecutionHandler(event_store=event_store)
    runner._retire_process_local_authority(
        session_id=tracker.session_id,
        execution_id=tracker.execution_id,
    )
    mark_cancelled = AsyncMock(return_value=Result.ok(True))

    try:
        with (
            patch(
                "ouroboros.orchestrator.heartbeat.is_holder_alive",
                return_value=True,
            ),
            patch.object(cancel_handler._session_repo, "mark_cancelled_if_active", mark_cancelled),
        ):
            result = await cancel_handler.handle(
                {
                    "execution_id": tracker.execution_id,
                    "reason": "foreign owner safety",
                }
            )

        assert result.is_ok
        assert result.value.meta["new_status"] == "cancellation_requested"
        assert result.value.meta["in_flight"] is True
        assert await is_cancellation_requested(tracker.session_id)
        request = heartbeat.read_cancellation_request(tracker.session_id)
        assert request is not None
        assert request.reason == "foreign owner safety"
        assert request.cancelled_by == "mcp_tool"
        mark_cancelled.assert_not_awaited()
    finally:
        await clear_cancellation(tracker.session_id)
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_foreign_owner_terminal_racing_cancel_publication_clears_signal(tmp_path) -> None:
    """A stale foreign liveness observation cannot leave a late file marker."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'foreign-cancel-race.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-foreign-cancel-race",
        session_id="session-foreign-cancel-race",
    )
    assert prepared.is_ok
    stale_tracker = prepared.value
    runner._retire_process_local_authority(
        session_id=stale_tracker.session_id,
        execution_id=stale_tracker.execution_id,
    )
    completed = await runner._session_repo.mark_completed(
        stale_tracker.session_id,
        {"done": True},
    )
    assert completed.is_ok

    try:
        with patch(
            "ouroboros.orchestrator.heartbeat.is_holder_alive",
            return_value=True,
        ):
            outcome = await request_process_local_cancellation(
                stale_tracker,
                runner._session_repo,
                reason="racing foreign cancellation",
                cancelled_by="test",
            )

        assert outcome is not None
        assert outcome.disposition == ProcessLocalCancellationDisposition.ALREADY_TERMINAL
        assert not heartbeat.has_cancellation_request(stale_tracker.session_id)
        assert not await is_cancellation_requested(stale_tracker.session_id)
    finally:
        await clear_cancellation(stale_tracker.session_id)
        runner._retire_process_local_authority(
            session_id=stale_tracker.session_id,
            execution_id=stale_tracker.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_public_cancel_interruption_reconciles_committed_terminal_state() -> None:
    """Cancellation after the public CAS cannot restore a terminal owner."""
    runner = _runner()
    tracker = await _prepare(
        runner,
        session_id="session-public-post-cas-cancel",
        execution_id="exec-public-post-cas-cancel",
    )
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    cancelled_tracker = tracker.with_status(SessionStatus.CANCELLED)
    session_repo = MagicMock()
    session_repo.mark_cancelled_if_active = AsyncMock(side_effect=asyncio.CancelledError)
    session_repo.reconstruct_session = AsyncMock(return_value=Result.ok(cancelled_tracker))

    try:
        with pytest.raises(asyncio.CancelledError):
            await request_process_local_cancellation(
                tracker,
                session_repo,
                reason="cancel after commit",
                cancelled_by="test",
            )

        assert not runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert not heartbeat.is_holder_alive(tracker.session_id)
        assert not await is_cancellation_requested(tracker.session_id)
    finally:
        await clear_cancellation(tracker.session_id)
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )


@pytest.mark.asyncio
async def test_public_cancel_unreadable_post_cas_stays_non_effectful_until_retry(
    tmp_path,
) -> None:
    """An ambiguous post-CAS read cannot restore terminalizing authority."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'public-cancel-retry.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-public-cancel-retry",
        session_id="session-public-cancel-retry",
    )
    assert prepared.is_ok
    tracker = prepared.value
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    public_repo = SessionRepository(event_store)
    original_mark_cancelled = public_repo.mark_cancelled_if_active
    original_reconstruct = public_repo.reconstruct_session

    async def _commit_then_cancel(*args: object, **kwargs: object):
        committed = await original_mark_cancelled(*args, **kwargs)
        assert committed.is_ok and committed.value is True
        raise asyncio.CancelledError

    try:
        with (
            patch.object(
                public_repo,
                "mark_cancelled_if_active",
                AsyncMock(side_effect=_commit_then_cancel),
            ),
            patch.object(
                public_repo,
                "reconstruct_session",
                AsyncMock(return_value=Result.err(PersistenceError("winner unreadable"))),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await request_process_local_cancellation(
                tracker,
                public_repo,
                reason="cancel with unreadable winner",
                cancelled_by="mcp_tool",
            )

        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok and durable.value.status == SessionStatus.CANCELLED
        generation, already_claimed = runner._claim_process_local_authority_generation(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert generation is None
        assert already_claimed is True
        assert heartbeat.is_holder_alive(tracker.session_id)

        public_repo.reconstruct_session = original_reconstruct
        retried = await request_process_local_cancellation(
            tracker,
            public_repo,
            reason="retry terminal reconciliation",
            cancelled_by="mcp_tool",
        )

        assert retried is not None
        assert retried.disposition is ProcessLocalCancellationDisposition.ALREADY_TERMINAL
        assert retried.retired is True
        assert not runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert not heartbeat.is_holder_alive(tracker.session_id)
        assert not await is_cancellation_requested(tracker.session_id)
    finally:
        await clear_cancellation(tracker.session_id)
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_public_cancel_retry_reclaims_nonterminal_reservation() -> None:
    """A retryable TERMINALIZING owner retries CAS instead of blocking forever."""
    runner = _runner()
    tracker = await _prepare(
        runner,
        session_id="session-public-nonterminal-retry",
        execution_id="exec-public-nonterminal-retry",
    )
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    session_repo = MagicMock()
    session_repo.mark_cancelled_if_active = AsyncMock(side_effect=asyncio.CancelledError)
    session_repo.reconstruct_session = AsyncMock(
        return_value=Result.err(PersistenceError("winner temporarily unreadable"))
    )

    try:
        with pytest.raises(asyncio.CancelledError):
            await request_process_local_cancellation(
                tracker,
                session_repo,
                reason="first cancellation attempt",
                cancelled_by="mcp_tool",
            )

        generation, already_claimed = runner._claim_process_local_authority_generation(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert generation is None and already_claimed is True

        session_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))
        session_repo.mark_cancelled_if_active = AsyncMock(return_value=Result.ok(True))
        retried = await request_process_local_cancellation(
            tracker,
            session_repo,
            reason="retry cancellation CAS",
            cancelled_by="mcp_tool",
        )

        assert retried is not None
        assert retried.disposition is ProcessLocalCancellationDisposition.CANCELLED
        session_repo.mark_cancelled_if_active.assert_awaited_once()
        assert not runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert not heartbeat.is_holder_alive(tracker.session_id)
    finally:
        await clear_cancellation(tracker.session_id)
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )


@pytest.mark.asyncio
async def test_repeated_public_cancel_interruption_drains_terminal_cleanup() -> None:
    """A second cancellation cannot restore a committed public reservation."""
    runner = _runner()
    tracker = await _prepare(
        runner,
        session_id="session-public-double-cancel",
        execution_id="exec-public-double-cancel",
    )
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    cancelled_tracker = tracker.with_status(SessionStatus.CANCELLED)
    reconciliation_started = asyncio.Event()
    allow_reconciliation = asyncio.Event()
    session_repo = MagicMock()
    session_repo.mark_cancelled_if_active = AsyncMock(side_effect=asyncio.CancelledError)

    async def _delayed_reconstruct(_: str):
        reconciliation_started.set()
        await allow_reconciliation.wait()
        return Result.ok(cancelled_tracker)

    session_repo.reconstruct_session = AsyncMock(side_effect=_delayed_reconstruct)

    try:
        cancellation = asyncio.create_task(
            request_process_local_cancellation(
                tracker,
                session_repo,
                reason="cancel after committed write",
                cancelled_by="test",
            )
        )
        await reconciliation_started.wait()
        cancellation.cancel()
        allow_reconciliation.set()
        with pytest.raises(asyncio.CancelledError):
            await cancellation

        assert not runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert not heartbeat.is_holder_alive(tracker.session_id)
        assert not await is_cancellation_requested(tracker.session_id)
    finally:
        allow_reconciliation.set()
        await clear_cancellation(tracker.session_id)
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )


@pytest.mark.asyncio
async def test_durable_cancellation_request_crosses_process_boundary() -> None:
    """The file-backed request is visible without sharing Python memory."""
    session_id = "session-cross-process-cancel"
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:  # pragma: no cover - assertions run in parent
        os.close(read_fd)
        try:
            heartbeat.publish_cancellation_request(session_id)
            os.write(write_fd, b"1")
        finally:
            os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    try:
        assert os.read(read_fd, 1) == b"1"
        waited_pid, status = os.waitpid(child_pid, 0)
        assert waited_pid == child_pid
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 0
        assert session_id not in await get_pending_cancellations()
        assert await is_cancellation_requested(session_id)
    finally:
        os.close(read_fd)
        await clear_cancellation(session_id)


@pytest.mark.asyncio
async def test_owner_consumes_file_backed_cancellation_before_effects(tmp_path) -> None:
    """A request from another process reaches the owning runner at startup."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'foreign-request.db'}")
    await event_store.initialize()
    runtime = _CountingRuntime()
    runner = OrchestratorRunner(runtime, event_store, MagicMock(), fat_harness_mode=False)
    prepared = await runner.prepare_session(
        _seed(),
        execution_id="exec-consume-foreign-request",
        session_id="session-consume-foreign-request",
    )
    assert prepared.is_ok
    tracker = prepared.value
    heartbeat.publish_cancellation_request(tracker.session_id)

    try:
        result = await runner.execute_precreated_session(_seed(), tracker, parallel=False)

        assert result.is_ok
        assert result.value.success is False
        durable = await SessionRepository(event_store).reconstruct_session(tracker.session_id)
        assert durable.is_ok
        assert durable.value.status == SessionStatus.CANCELLED
        assert runtime.execute_calls == 0
        assert not heartbeat.has_cancellation_request(tracker.session_id)
        assert not heartbeat.is_holder_alive(tracker.session_id)
    finally:
        await clear_cancellation(tracker.session_id)
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_terminal_reconcile_does_not_retire_claimed_pre_route_owner() -> None:
    """Terminal observation must respect a claim acquired before route registration."""
    runner = _runner()
    tracker = await _prepare(
        runner,
        session_id="session-terminal-reconcile-claimed",
        execution_id="exec-terminal-reconcile-claimed",
    )
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    handler = ExecuteSeedHandler(event_store=_HandlerEventStore())
    handler._remember_process_local_owner(_paused(tracker), runner)
    generation, already_claimed = runner._claim_process_local_authority_generation(
        tracker.session_id,
        tracker.execution_id,
        contract,
    )
    assert generation is not None
    assert already_claimed is False

    try:
        await handler._reconcile_terminal_process_local_owner(
            tracker.with_status(SessionStatus.CANCELLED)
        )

        assert runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert await is_cancellation_requested(tracker.session_id)
    finally:
        handler._process_local_resume_owners.clear()
        handler._process_local_owned_event_stores.clear()
        handler._process_local_resume_handoffs.clear()
        await clear_cancellation(tracker.session_id)
        runner._release_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )


@pytest.mark.asyncio
async def test_running_cancellation_retry_claim_is_released_if_probe_task_is_cancelled() -> None:
    """Cancelling a retry probe cannot strand its claimed live generation."""
    runner = _runner()
    tracker = await _prepare(
        runner,
        session_id="session-cancelled-retry-probe",
        execution_id="exec-cancelled-retry-probe",
    )
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    runner._session_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))
    runner._task_workspace = SimpleNamespace(lock_path="/tmp/cancelled-retry-probe.lock")
    entered_check = asyncio.Event()
    never_finish = asyncio.Event()

    async def _blocked_cancellation_check(_: str) -> bool:
        entered_check.set()
        await never_finish.wait()
        return False

    runner._check_startup_cancellation = _blocked_cancellation_check
    try:
        with patch("ouroboros.orchestrator.runner.release_lock") as release_lock_mock:
            retry = asyncio.create_task(runner.resume_session(tracker.session_id, _seed()))
            await entered_check.wait()
            retry.cancel()
            with pytest.raises(asyncio.CancelledError):
                await retry

        release_lock_mock.assert_called_once_with("/tmp/cancelled-retry-probe.lock")

        generation, already_claimed = runner._claim_process_local_authority_generation(
            tracker.session_id,
            tracker.execution_id,
            contract,
        )
        assert generation is not None
        assert already_claimed is False
        runner._release_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
    finally:
        await clear_cancellation(tracker.session_id)
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        runner._unregister_session(tracker.execution_id, tracker.session_id)


@pytest.mark.asyncio
async def test_terminal_finalizers_drain_before_propagating_cancellation() -> None:
    """One cancelled finalizer cannot skip later local resource cleanup."""
    runner = _runner()
    tracker = await _prepare(
        runner,
        session_id="session-terminal-finalizer-cancellation",
        execution_id="exec-terminal-finalizer-cancellation",
    )
    authority = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]["foundation_a_authority"]
    calls: list[str] = []

    async def _self_cancelling_finalizer() -> None:
        calls.append("first")
        asyncio.current_task().cancel()
        await asyncio.sleep(0)

    async def _later_finalizer() -> None:
        calls.append("second")

    assert _register_process_local_authority_terminal_finalizer(
        tracker.session_id,
        tracker.execution_id,
        authority,
        runner._adapter,
        ("test", "self-cancelling"),
        _self_cancelling_finalizer,
    )
    assert _register_process_local_authority_terminal_finalizer(
        tracker.session_id,
        tracker.execution_id,
        authority,
        runner._adapter,
        ("test", "later"),
        _later_finalizer,
    )

    with pytest.raises(asyncio.CancelledError):
        await _retire_process_local_authority_after_terminal_persistence(
            tracker.session_id,
            tracker.execution_id,
            authority,
        )

    assert calls == ["first", "second"]
    assert not heartbeat.is_holder_alive(tracker.session_id)


def test_process_local_contract_is_not_a_cross_run_proof_cohort_key() -> None:
    runner = _runner()
    contract = runner._build_execution_contract(seed=_seed())

    assert (
        runner._proof_cohort_identity(
            {
                "seed_id": _seed().metadata.seed_id,
                EXECUTION_CONTRACT_PROGRESS_KEY: contract,
            }
        )
        is None
    )


@pytest.mark.asyncio
async def test_prepare_publishes_liveness_before_a_running_tracker_is_observable() -> None:
    """An observer interleaved in create_session cannot false-terminalize it."""
    creator = _runner()
    observer = _runner(_CountingRuntime())
    observed_result: Result[object, OrchestratorError] | None = None

    async def create_session(**kwargs: object) -> Result[SessionTracker, object]:
        nonlocal observed_result
        tracker = SessionTracker.create(
            str(kwargs["execution_id"]),
            str(kwargs["seed_id"]),
            session_id=str(kwargs["session_id"]),
        ).with_progress({EXECUTION_CONTRACT_PROGRESS_KEY: dict(kwargs["execution_contract"])})
        observer._session_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))
        observer._session_repo.mark_failed = AsyncMock(return_value=Result.ok(None))
        observed_result = await observer.resume_session(tracker.session_id, _seed())
        return Result.ok(tracker)

    with (
        patch.object(creator._session_repo, "create_session", create_session),
        patch.object(
            creator._session_repo,
            "track_progress",
            AsyncMock(return_value=Result.ok(None)),
        ),
    ):
        prepared = await creator.prepare_session(
            _seed(),
            execution_id="exec-publish-race",
            session_id="session-publish-race",
        )

    try:
        assert prepared.is_ok
        assert observed_result is not None and observed_result.is_err
        assert (
            observed_result.error.details["resume_blocked"]
            == "process_local_authority_held_elsewhere"
        )
        observer._session_repo.mark_failed.assert_not_awaited()
    finally:
        creator._retire_process_local_authority(
            session_id="session-publish-race",
            execution_id="exec-publish-race",
        )


@pytest.mark.asyncio
async def test_prepare_rolls_back_when_heartbeat_acquire_fails() -> None:
    runner = _runner()
    session_id = "session-heartbeat-acquire-failure"
    execution_id = "exec-heartbeat-acquire-failure"

    with patch(
        "ouroboros.orchestrator.heartbeat.acquire",
        side_effect=OSError("lock directory unavailable"),
    ):
        result = await runner.prepare_session(
            _seed(),
            execution_id=execution_id,
            session_id=session_id,
        )

    assert result.is_err
    assert result.error.message == "Cannot establish process-local execution liveness lease"
    assert (session_id, execution_id) not in runner._process_local_authorities
    assert not heartbeat.is_holder_alive(session_id)


@pytest.mark.asyncio
async def test_prepare_cancellation_discards_issuance_and_releases_workspace() -> None:
    """Cancellation before durable publication cannot leak a live generation."""
    runner = _runner()
    workspace = SimpleNamespace(lock_path="/tmp/process-local-prepare-cancel.lock")
    runner._task_workspace = workspace
    issued_before = len(_PROCESS_LOCAL_AUTHORITY_REGISTRY._issued)

    with (
        patch(
            "ouroboros.orchestrator.runner.asyncio.to_thread",
            AsyncMock(side_effect=asyncio.CancelledError),
        ),
        patch("ouroboros.orchestrator.runner.release_lock") as release_lock_mock,
        pytest.raises(asyncio.CancelledError),
    ):
        await runner.prepare_session(
            _seed(),
            execution_id="exec-prepare-cancel",
            session_id="session-prepare-cancel",
        )

    assert len(_PROCESS_LOCAL_AUTHORITY_REGISTRY._issued) == issued_before
    release_lock_mock.assert_called_once_with(workspace.lock_path)


@pytest.mark.asyncio
async def test_prepare_cancellation_with_unknown_publication_retains_lease() -> None:
    """An ambiguous create-session cancellation preserves the exact owner."""
    runner = _runner()
    session_id = "session-prepare-create-cancel"
    execution_id = "exec-prepare-create-cancel"

    with (
        patch.object(
            runner._session_repo,
            "create_session",
            AsyncMock(side_effect=asyncio.CancelledError),
        ),
        patch.object(
            runner._session_repo,
            "reconstruct_session",
            AsyncMock(return_value=Result.err(PersistenceError("publication unknown"))),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await runner.prepare_session(
            _seed(),
            execution_id=execution_id,
            session_id=session_id,
        )

    try:
        assert (session_id, execution_id) in runner._process_local_authorities
        assert heartbeat.is_holder_alive(session_id)
    finally:
        runner._retire_process_local_authority(
            session_id=session_id,
            execution_id=execution_id,
        )


@pytest.mark.asyncio
async def test_prepare_cancellation_after_committed_start_terminalizes_then_retires(
    tmp_path,
) -> None:
    """A committed start event is reconciled even when create_session raises cancellation."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'create-cancel-commit.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    session_id = "session-create-cancel-committed"
    execution_id = "exec-create-cancel-committed"
    original_create_session = runner._session_repo.create_session

    async def _commit_then_cancel(**kwargs: object):
        created = await original_create_session(**kwargs)
        assert created.is_ok
        raise asyncio.CancelledError

    try:
        with (
            patch.object(runner._session_repo, "create_session", _commit_then_cancel),
            pytest.raises(asyncio.CancelledError),
        ):
            await runner.prepare_session(
                _seed(),
                execution_id=execution_id,
                session_id=session_id,
            )

        durable = await SessionRepository(event_store).reconstruct_session(session_id)
        assert durable.is_ok
        assert durable.value.status == SessionStatus.FAILED
        assert (session_id, execution_id) not in runner._process_local_authorities
        assert not heartbeat.is_holder_alive(session_id)
    finally:
        runner._retire_process_local_authority(
            session_id=session_id,
            execution_id=execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_prepare_exception_after_committed_start_terminalizes_then_retires(tmp_path) -> None:
    """A post-commit repository exception uses the same publication reconciliation."""
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'create-error-commit.db'}")
    await event_store.initialize()
    runner = OrchestratorRunner(_CountingRuntime(), event_store, MagicMock())
    session_id = "session-create-error-committed"
    execution_id = "exec-create-error-committed"
    original_create_session = runner._session_repo.create_session

    async def _commit_then_raise(**kwargs: object):
        created = await original_create_session(**kwargs)
        assert created.is_ok
        raise RuntimeError("repository response interrupted after commit")

    try:
        with patch.object(runner._session_repo, "create_session", _commit_then_raise):
            result = await runner.prepare_session(
                _seed(),
                execution_id=execution_id,
                session_id=session_id,
            )

        assert result.is_err
        durable = await SessionRepository(event_store).reconstruct_session(session_id)
        assert durable.is_ok
        assert durable.value.status == SessionStatus.FAILED
        assert (session_id, execution_id) not in runner._process_local_authorities
        assert not heartbeat.is_holder_alive(session_id)
    finally:
        runner._retire_process_local_authority(
            session_id=session_id,
            execution_id=execution_id,
        )
        await event_store.close()


@pytest.mark.asyncio
async def test_prepare_cancellation_after_publication_terminalizes_then_retires() -> None:
    """A cancelled initial-progress write cannot leave RUNNING without an owner."""
    runner = _runner()
    session_id = "session-prepare-progress-cancel"
    execution_id = "exec-prepare-progress-cancel"
    tracker = SessionTracker.create(
        execution_id,
        _seed().metadata.seed_id,
        session_id=session_id,
    )
    mark_failed = AsyncMock(return_value=Result.ok(None))

    with (
        patch.object(
            runner._session_repo,
            "create_session",
            AsyncMock(return_value=Result.ok(tracker)),
        ),
        patch.object(
            runner._session_repo,
            "track_progress",
            AsyncMock(side_effect=asyncio.CancelledError),
        ),
        patch.object(
            runner._session_repo,
            "reconstruct_session",
            AsyncMock(return_value=Result.ok(tracker)),
        ),
        patch.object(runner._session_repo, "mark_failed", mark_failed),
        pytest.raises(asyncio.CancelledError),
    ):
        await runner.prepare_session(
            _seed(),
            execution_id=execution_id,
            session_id=session_id,
        )

    mark_failed.assert_awaited_once()
    assert (session_id, execution_id) not in runner._process_local_authorities
    assert not heartbeat.is_holder_alive(session_id)


@pytest.mark.asyncio
async def test_prepare_unexpected_error_discards_issuance_and_releases_workspace() -> None:
    """Unexpected pre-registration errors follow the same fail-closed cleanup."""
    runner = _runner()
    workspace = SimpleNamespace(lock_path="/tmp/process-local-prepare-error.lock")
    runner._task_workspace = workspace
    issued_before = len(_PROCESS_LOCAL_AUTHORITY_REGISTRY._issued)

    with (
        patch.object(runner, "_build_execution_contract", side_effect=RuntimeError("boom")),
        patch("ouroboros.orchestrator.runner.release_lock") as release_lock_mock,
    ):
        result = await runner.prepare_session(
            _seed(),
            execution_id="exec-prepare-error",
            session_id="session-prepare-error",
        )

    assert result.is_err
    assert result.error.message == "Failed to prepare process-local execution authority"
    assert len(_PROCESS_LOCAL_AUTHORITY_REGISTRY._issued) == issued_before
    release_lock_mock.assert_called_once_with(workspace.lock_path)


@pytest.mark.asyncio
async def test_prepare_progress_exception_terminalizes_then_retires_authority() -> None:
    runner = _runner()
    session_id = "session-progress-exception"
    execution_id = "exec-progress-exception"
    tracker = SessionTracker.create(
        execution_id,
        _seed().metadata.seed_id,
        session_id=session_id,
    )
    mark_failed = AsyncMock(return_value=Result.ok(None))

    with (
        patch.object(
            runner._session_repo, "create_session", AsyncMock(return_value=Result.ok(tracker))
        ),
        patch.object(
            runner._session_repo,
            "track_progress",
            AsyncMock(side_effect=OSError("event store unavailable")),
        ),
        patch.object(runner._session_repo, "mark_failed", mark_failed),
    ):
        result = await runner.prepare_session(
            _seed(),
            execution_id=execution_id,
            session_id=session_id,
        )

    assert result.is_err
    mark_failed.assert_awaited_once()
    assert not runner._has_live_process_local_authority(
        session_id,
        execution_id,
        tracker.progress.get(EXECUTION_CONTRACT_PROGRESS_KEY),
    )
    assert not heartbeat.is_holder_alive(session_id)


@pytest.mark.asyncio
async def test_prepare_rejects_mismatched_repository_tracker_and_retires_lease() -> None:
    runner = _runner()
    returned = SessionTracker.create(
        "exec-other",
        _seed().metadata.seed_id,
        session_id="session-other",
    )
    session_id = "session-expected"
    execution_id = "exec-expected"

    with (
        patch.object(
            runner._session_repo,
            "create_session",
            AsyncMock(return_value=Result.ok(returned)),
        ),
        patch.object(
            runner._session_repo,
            "reconstruct_session",
            AsyncMock(
                return_value=Result.err(
                    PersistenceError(f"No events found for session: {session_id}")
                )
            ),
        ),
    ):
        result = await runner.prepare_session(
            _seed(),
            execution_id=execution_id,
            session_id=session_id,
        )

    assert result.is_err
    assert result.error.message == "Session repository returned an unexpected session identity"
    assert (session_id, execution_id) not in runner._process_local_authorities
    assert not heartbeat.is_holder_alive(session_id)


@pytest.mark.asyncio
async def test_foreign_paused_resume_rejects_without_terminalizing_live_owner() -> None:
    owner = _runner()
    tracker = await _prepare(
        owner,
        session_id="session-foreign-paused",
        execution_id="exec-foreign-paused",
    )
    observer = _runner(_CountingRuntime())
    observer._session_repo.reconstruct_session = AsyncMock(return_value=Result.ok(_paused(tracker)))
    observer._session_repo.mark_failed = AsyncMock(return_value=Result.ok(None))

    try:
        result = await observer.resume_session(tracker.session_id, _seed())

        assert result.is_err
        assert result.error.details["resume_blocked"] == "process_local_authority_held_elsewhere"
        observer._session_repo.mark_failed.assert_not_awaited()
        assert heartbeat.is_holder_alive(tracker.session_id)
        assert owner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY],
        )
    finally:
        owner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )


@pytest.mark.asyncio
async def test_foreign_precreated_running_tracker_rejects_without_revoking_owner() -> None:
    owner = _runner()
    tracker = await _prepare(
        owner,
        session_id="session-foreign-precreated",
        execution_id="exec-foreign-precreated",
    )
    observer = _runner(_CountingRuntime())
    observer._get_merged_tools = AsyncMock(side_effect=AssertionError("tool setup must not run"))

    try:
        result = await observer.execute_precreated_session(_seed(), tracker, parallel=False)

        assert result.is_err
        assert result.error.details["resume_blocked"] == "process_local_authority_held_elsewhere"
        assert heartbeat.is_holder_alive(tracker.session_id)
        assert owner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY],
        )
        observer._get_merged_tools.assert_not_awaited()
    finally:
        owner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )


@pytest.mark.asyncio
async def test_foreign_observer_cannot_terminalize_paused_transition_before_claim_release() -> None:
    owner = _runner()
    tracker = await _prepare(
        owner,
        session_id="session-paused-transition",
        execution_id="exec-paused-transition",
    )
    contract = tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
    generation, claimed = owner._claim_process_local_authority_generation(
        tracker.session_id,
        tracker.execution_id,
        contract,
    )
    assert generation is not None
    assert claimed is False
    observer = _runner(_CountingRuntime())
    observer._session_repo.reconstruct_session = AsyncMock(return_value=Result.ok(_paused(tracker)))
    observer._session_repo.mark_failed = AsyncMock(return_value=Result.ok(None))

    try:
        result = await observer.resume_session(tracker.session_id, _seed())

        assert result.is_err
        assert result.error.details["resume_blocked"] == "process_local_authority_held_elsewhere"
        observer._session_repo.mark_failed.assert_not_awaited()
        assert heartbeat.is_holder_alive(tracker.session_id)
    finally:
        owner._release_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        owner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )


@pytest.mark.asyncio
async def test_paused_unregister_keeps_liveness_lease_until_terminal_retirement() -> None:
    runner = _runner()
    tracker = await _prepare(
        runner,
        session_id="session-paused-lease",
        execution_id="exec-paused-lease",
    )

    try:
        runner._register_session(tracker.execution_id, tracker.session_id)
        runner._release_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        runner._unregister_session(
            tracker.execution_id,
            tracker.session_id,
            release_liveness_lease=False,
        )

        assert tracker.execution_id not in runner.active_sessions
        assert heartbeat.is_holder_alive(tracker.session_id)
        assert runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY],
        )
    finally:
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )

    assert not heartbeat.is_holder_alive(tracker.session_id)


@pytest.mark.asyncio
async def test_execute_handler_resumes_with_the_retained_process_local_runner(
    tmp_path,
) -> None:
    """A same-process MCP resume must reuse its original live capability owner."""
    runner = _runner()
    tracker = await _prepare(
        runner,
        session_id="session-handler-retained",
        execution_id="exec-handler-retained",
    )
    paused_tracker = _paused(tracker)
    handler_store = _HandlerEventStore()
    handler = ExecuteSeedHandler(event_store=handler_store)
    handler._remember_process_local_owner(
        paused_tracker,
        runner,
        owned_event_store=runner._event_store,
    )
    resumed = AsyncMock(
        return_value=Result.ok(
            OrchestratorResult(
                success=False,
                session_id=paused_tracker.session_id,
                execution_id=paused_tracker.execution_id,
                final_message="Still paused",
            )
        )
    )
    observer_repo = MagicMock()
    observer_repo.reconstruct_session = AsyncMock(return_value=Result.ok(paused_tracker))

    try:
        with (
            patch.object(runner, "resume_session", resumed),
            patch.object(
                runner._session_repo,
                "reconstruct_session",
                AsyncMock(return_value=Result.ok(paused_tracker)),
            ),
            patch(
                "ouroboros.mcp.tools.execution_handlers.SessionRepository",
                return_value=observer_repo,
            ),
            patch(
                "ouroboros.mcp.tools.execution_handlers.create_agent_runtime",
                side_effect=AssertionError("resume must not construct a fresh runtime"),
            ) as create_runtime,
            patch(
                "ouroboros.mcp.tools.execution_handlers.resolve_dashboard_run_url",
                AsyncMock(return_value=None),
            ),
        ):
            result = await handler.handle(
                {
                    "session_id": paused_tracker.session_id,
                    "seed_content": yaml.safe_dump(_seed().to_dict()),
                    "cwd": str(tmp_path),
                    "use_worktree": False,
                    "skip_qa": True,
                },
                synchronous=True,
            )

        assert result.is_ok
        assert result.value.meta["status"] == "paused"
        resumed.assert_awaited_once()
        assert resumed.await_args.args[0] == paused_tracker.session_id
        assert resumed.await_args.args[1].metadata.seed_id == _seed().metadata.seed_id
        create_runtime.assert_not_called()
        assert handler._process_local_resume_owners[paused_tracker.session_id] is runner
        assert (
            handler._process_local_owned_event_stores[paused_tracker.session_id]
            is runner._event_store
        )
        runner._event_store.close.assert_not_awaited()
    finally:
        handler._process_local_resume_owners.clear()
        handler._process_local_owned_event_stores.clear()
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )


@pytest.mark.asyncio
async def test_retained_handler_owned_store_closes_after_terminal_resume(tmp_path) -> None:
    """The original internally owned store survives pause and closes at terminal state."""
    runner = _runner()
    tracker = await _prepare(
        runner,
        session_id="session-handler-terminal",
        execution_id="exec-handler-terminal",
    )
    paused_tracker = _paused(tracker)
    completed_tracker = paused_tracker.with_status(SessionStatus.COMPLETED)
    handler = ExecuteSeedHandler(event_store=_HandlerEventStore())
    handler._remember_process_local_owner(
        paused_tracker,
        runner,
        owned_event_store=runner._event_store,
    )
    resumed = AsyncMock(
        return_value=Result.ok(
            OrchestratorResult(
                success=True,
                session_id=paused_tracker.session_id,
                execution_id=paused_tracker.execution_id,
                final_message="Completed",
            )
        )
    )
    observer_repo = MagicMock()
    observer_repo.reconstruct_session = AsyncMock(return_value=Result.ok(paused_tracker))

    try:
        with (
            patch.object(runner, "resume_session", resumed),
            patch.object(
                runner._session_repo,
                "reconstruct_session",
                AsyncMock(
                    side_effect=[
                        Result.ok(completed_tracker),
                        Result.ok(completed_tracker),
                    ]
                ),
            ),
            patch(
                "ouroboros.mcp.tools.execution_handlers.SessionRepository",
                return_value=observer_repo,
            ),
            patch(
                "ouroboros.mcp.tools.execution_handlers.create_agent_runtime",
                side_effect=AssertionError("resume must not construct a fresh runtime"),
            ) as create_runtime,
            patch(
                "ouroboros.mcp.tools.execution_handlers.resolve_dashboard_run_url",
                AsyncMock(return_value=None),
            ),
        ):
            result = await handler.handle(
                {
                    "session_id": paused_tracker.session_id,
                    "seed_content": yaml.safe_dump(_seed().to_dict()),
                    "cwd": str(tmp_path),
                    "use_worktree": False,
                    "skip_qa": True,
                },
                synchronous=True,
            )

        assert result.is_ok
        assert result.value.meta["status"] == "completed"
        create_runtime.assert_not_called()
        runner._event_store.close.assert_awaited_once()
        assert paused_tracker.session_id not in handler._process_local_resume_owners
        assert paused_tracker.session_id not in handler._process_local_owned_event_stores
    finally:
        handler._process_local_resume_owners.clear()
        handler._process_local_owned_event_stores.clear()
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )


@pytest.mark.asyncio
async def test_retained_handler_keeps_concurrent_resume_nonterminal(tmp_path) -> None:
    """A concurrent retained resume must preserve the paused owner and not fail it."""
    runner = _runner()
    tracker = await _prepare(
        runner,
        session_id="session-handler-concurrent",
        execution_id="exec-handler-concurrent",
    )
    paused_tracker = _paused(tracker)
    handler = ExecuteSeedHandler(event_store=_HandlerEventStore())
    handler._remember_process_local_owner(paused_tracker, runner)
    in_progress = OrchestratorError(
        "already claimed",
        details={"resume_blocked": "process_local_execution_in_progress"},
    )
    observer_repo = MagicMock()
    observer_repo.reconstruct_session = AsyncMock(return_value=Result.ok(paused_tracker))
    mark_failed = AsyncMock()

    try:
        with (
            patch.object(
                runner,
                "resume_session",
                AsyncMock(return_value=Result.err(in_progress)),
            ),
            patch.object(
                runner._session_repo,
                "reconstruct_session",
                AsyncMock(return_value=Result.ok(paused_tracker)),
            ),
            patch.object(runner._session_repo, "mark_failed", mark_failed),
            patch(
                "ouroboros.mcp.tools.execution_handlers.SessionRepository",
                return_value=observer_repo,
            ),
            patch(
                "ouroboros.mcp.tools.execution_handlers.create_agent_runtime",
                side_effect=AssertionError("resume must not construct a fresh runtime"),
            ) as create_runtime,
            patch(
                "ouroboros.mcp.tools.execution_handlers.resolve_dashboard_run_url",
                AsyncMock(return_value=None),
            ),
        ):
            result = await handler.handle(
                {
                    "session_id": paused_tracker.session_id,
                    "seed_content": yaml.safe_dump(_seed().to_dict()),
                    "cwd": str(tmp_path),
                    "use_worktree": False,
                    "skip_qa": True,
                },
                synchronous=True,
            )

        assert result.is_ok
        assert result.value.meta["status"] == "paused"
        create_runtime.assert_not_called()
        mark_failed.assert_not_awaited()
        assert handler._process_local_resume_owners[paused_tracker.session_id] is runner
    finally:
        handler._process_local_resume_owners.clear()
        handler._process_local_owned_event_stores.clear()
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )


@pytest.mark.asyncio
async def test_retained_handler_preserves_cancellation_persistence_retry(tmp_path) -> None:
    """The MCP wrapper must not overwrite a retryable cancellation failure."""
    runner = _runner()
    tracker = await _prepare(
        runner,
        session_id="session-handler-cancellation-pending",
        execution_id="exec-handler-cancellation-pending",
    )
    handler = ExecuteSeedHandler(event_store=_HandlerEventStore())
    handler._remember_process_local_owner(tracker, runner)
    pending = OrchestratorError(
        "cancellation terminal write unavailable",
        details={"resume_blocked": "cancellation_persistence_pending"},
    )
    observer_repo = MagicMock()
    observer_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))
    mark_failed = AsyncMock()

    try:
        with (
            patch.object(
                runner,
                "resume_session",
                AsyncMock(return_value=Result.err(pending)),
            ),
            patch.object(
                runner._session_repo,
                "reconstruct_session",
                AsyncMock(return_value=Result.ok(tracker)),
            ),
            patch.object(runner._session_repo, "mark_failed", mark_failed),
            patch(
                "ouroboros.mcp.tools.execution_handlers.SessionRepository",
                return_value=observer_repo,
            ),
            patch(
                "ouroboros.mcp.tools.execution_handlers.create_agent_runtime",
                side_effect=AssertionError("resume must not construct a fresh runtime"),
            ) as create_runtime,
            patch(
                "ouroboros.mcp.tools.execution_handlers.resolve_dashboard_run_url",
                AsyncMock(return_value=None),
            ),
        ):
            result = await handler.handle(
                {
                    "session_id": tracker.session_id,
                    "seed_content": yaml.safe_dump(_seed().to_dict()),
                    "cwd": str(tmp_path),
                    "use_worktree": False,
                    "skip_qa": True,
                },
                synchronous=True,
            )

        assert result.is_ok
        assert result.value.meta["status"] == "unknown"
        create_runtime.assert_not_called()
        mark_failed.assert_not_awaited()
        assert handler._process_local_resume_owners[tracker.session_id] is runner
    finally:
        handler._process_local_resume_owners.clear()
        handler._process_local_owned_event_stores.clear()
        handler._process_local_resume_handoffs.clear()
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )


@pytest.mark.asyncio
async def test_retained_handler_returns_typed_concurrent_block_before_worktree_restore(
    tmp_path,
) -> None:
    """A second same-handler resume must not degrade into a worktree-lock error."""
    runner = _runner()
    tracker = await _prepare(
        runner,
        session_id="session-handler-worktree-concurrent",
        execution_id="exec-handler-worktree-concurrent",
    )
    paused_tracker = _paused(tracker)
    handler = ExecuteSeedHandler(event_store=_HandlerEventStore())
    handler._remember_process_local_owner(paused_tracker, runner)
    workspace = SimpleNamespace(
        effective_cwd=str(tmp_path),
        worktree_path=str(tmp_path),
        branch="ooo/process-local",
        lock_path=str(tmp_path / "task.lock"),
    )
    entered_resume = asyncio.Event()
    release_resume = asyncio.Event()
    observer_repo = MagicMock()
    observer_repo.reconstruct_session = AsyncMock(return_value=Result.ok(paused_tracker))

    async def blocking_resume(*_: object) -> Result:
        entered_resume.set()
        await release_resume.wait()
        return Result.ok(
            OrchestratorResult(
                success=False,
                session_id=paused_tracker.session_id,
                execution_id=paused_tracker.execution_id,
                final_message="Still paused",
            )
        )

    arguments = {
        "session_id": paused_tracker.session_id,
        "seed_content": yaml.safe_dump(_seed().to_dict()),
        "cwd": str(tmp_path),
        "use_worktree": True,
        "skip_qa": True,
    }
    try:
        with (
            patch.object(runner, "resume_session", AsyncMock(side_effect=blocking_resume)),
            patch.object(
                runner._session_repo,
                "reconstruct_session",
                AsyncMock(return_value=Result.ok(paused_tracker)),
            ),
            patch(
                "ouroboros.mcp.tools.execution_handlers.SessionRepository",
                return_value=observer_repo,
            ),
            patch(
                "ouroboros.mcp.tools.execution_handlers.maybe_restore_task_workspace",
                return_value=workspace,
            ) as restore_workspace,
            patch(
                "ouroboros.mcp.tools.execution_handlers.create_agent_runtime",
                side_effect=AssertionError("retained resume must not construct a fresh runtime"),
            ),
            patch(
                "ouroboros.mcp.tools.execution_handlers.resolve_dashboard_run_url",
                AsyncMock(return_value=None),
            ),
        ):
            first = asyncio.create_task(handler.handle(arguments, synchronous=True))
            await asyncio.wait_for(entered_resume.wait(), timeout=1)
            second = await handler.handle(arguments, synchronous=True)

            assert second.is_err
            assert second.error.details["resume_blocked"] == "process_local_execution_in_progress"
            restore_workspace.assert_called_once()

            release_resume.set()
            first_result = await first

        assert first_result.is_ok
        assert paused_tracker.session_id not in handler._process_local_resume_handoffs
    finally:
        release_resume.set()
        handler._process_local_resume_owners.clear()
        handler._process_local_owned_event_stores.clear()
        handler._process_local_resume_handoffs.clear()
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )


@pytest.mark.asyncio
async def test_foreign_handler_returns_typed_block_before_worktree_restore(tmp_path) -> None:
    """A foreign live owner must not be obscured by task-worktree acquisition."""
    owner = _runner()
    tracker = await _prepare(
        owner,
        session_id="session-handler-foreign-worktree",
        execution_id="exec-handler-foreign-worktree",
    )
    paused_tracker = _paused(tracker)
    handler = ExecuteSeedHandler(event_store=_HandlerEventStore())
    observer_repo = MagicMock()
    observer_repo.reconstruct_session = AsyncMock(return_value=Result.ok(paused_tracker))

    try:
        with (
            patch(
                "ouroboros.mcp.tools.execution_handlers.SessionRepository",
                return_value=observer_repo,
            ),
            patch(
                "ouroboros.mcp.tools.execution_handlers.maybe_restore_task_workspace",
                side_effect=AssertionError("foreign authority must block before workspace restore"),
            ) as restore_workspace,
            patch(
                "ouroboros.mcp.tools.execution_handlers.create_agent_runtime",
                side_effect=AssertionError(
                    "foreign authority must block before runtime construction"
                ),
            ) as create_runtime,
        ):
            result = await handler.handle(
                {
                    "session_id": paused_tracker.session_id,
                    "seed_content": yaml.safe_dump(_seed().to_dict()),
                    "cwd": str(tmp_path),
                    "use_worktree": True,
                    "skip_qa": True,
                },
                synchronous=True,
            )

        assert result.is_err
        assert result.error.details["resume_blocked"] == "process_local_authority_held_elsewhere"
        restore_workspace.assert_not_called()
        create_runtime.assert_not_called()
    finally:
        owner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )


@pytest.mark.asyncio
async def test_stale_retained_owner_closes_its_handler_owned_event_store() -> None:
    """Evicting an owner that lost its capability must not leak its store."""
    runner = _runner()
    tracker = await _prepare(
        runner,
        session_id="session-handler-stale-store",
        execution_id="exec-handler-stale-store",
    )
    paused_tracker = _paused(tracker)
    handler = ExecuteSeedHandler(event_store=_HandlerEventStore())
    handler._remember_process_local_owner(
        paused_tracker,
        runner,
        owned_event_store=runner._event_store,
    )

    try:
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )

        retained = await handler._retained_process_local_owner(paused_tracker)

        assert retained is None
        runner._event_store.close.assert_awaited_once()
        assert paused_tracker.session_id not in handler._process_local_resume_owners
        assert paused_tracker.session_id not in handler._process_local_owned_event_stores
    finally:
        handler._process_local_resume_owners.clear()
        handler._process_local_owned_event_stores.clear()
        handler._process_local_resume_handoffs.clear()
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("reconstruction", ("raises", "error", "running"))
async def test_reconcile_retains_live_owner_on_inconclusive_reconstruction(
    reconstruction: str,
) -> None:
    """Read failures and nonterminal snapshots cannot evict a live owner."""
    runner = _runner()
    tracker = await _prepare(
        runner,
        session_id=f"session-handler-inconclusive-{reconstruction}",
        execution_id=f"exec-handler-inconclusive-{reconstruction}",
    )
    handler = ExecuteSeedHandler(event_store=_HandlerEventStore())
    handler._remember_process_local_owner(
        tracker,
        runner,
        owned_event_store=runner._event_store,
    )
    session_repo = MagicMock()
    if reconstruction == "raises":
        session_repo.reconstruct_session = AsyncMock(side_effect=OSError("observer unavailable"))
    elif reconstruction == "error":
        session_repo.reconstruct_session = AsyncMock(
            return_value=Result.err("observer unavailable")
        )
    else:
        session_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))

    try:
        retained, event_store_to_close = await handler._reconcile_process_local_owner(
            tracker=tracker,
            runner=runner,
            session_repo=session_repo,
        )

        assert retained is True
        assert event_store_to_close is None
        assert handler._process_local_resume_owners[tracker.session_id] is runner
        assert handler._process_local_owned_event_stores[tracker.session_id] is runner._event_store
        runner._event_store.close.assert_not_awaited()
    finally:
        handler._process_local_resume_owners.clear()
        handler._process_local_owned_event_stores.clear()
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )


def test_malformed_heartbeat_timestamp_is_unheld_and_never_raises(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(heartbeat, "LOCK_DIR", tmp_path)
    heartbeat.lock_path("malformed-heartbeat").write_text(f"{os.getpid()}:not-a-float")

    assert heartbeat.is_holder_alive("malformed-heartbeat") is False
    assert heartbeat.lock_path("malformed-heartbeat").exists()


def test_heartbeat_observer_never_deletes_a_lease_replaced_during_stale_check(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(heartbeat, "LOCK_DIR", tmp_path)
    session_id = "stale-observer-race"
    path = heartbeat.lock_path(session_id)
    path.write_text("999999:0")
    current_pid, current_start = heartbeat.current_process_identity()
    fresh = f"{current_pid}:{current_start}" if current_start is not None else str(current_pid)

    def replace_with_fresh_lease(_: int, __: float | None) -> bool:
        path.write_text(fresh)
        return False

    monkeypatch.setattr(heartbeat, "is_process_identity_alive", replace_with_fresh_lease)

    assert heartbeat.is_holder_alive(session_id) is False
    assert path.read_text() == fresh


def test_heartbeat_acquire_never_overwrites_an_existing_foreign_lease(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(heartbeat, "LOCK_DIR", tmp_path)
    session_id = "exclusive-lease"
    path = heartbeat.lock_path(session_id)
    path.write_text("999999:0")

    if heartbeat.fcntl is None:
        pytest.skip("advisory lease locks are unavailable on this platform")

    with (
        patch.object(heartbeat.fcntl, "flock", side_effect=BlockingIOError),
        pytest.raises(OSError, match="held"),
    ):
        heartbeat.acquire(session_id)

    assert path.read_text() == "999999:0"


@pytest.mark.parametrize("unsafe_session_id", ("..", "../../outside", "child/name", r"child\name"))
def test_heartbeat_lock_path_is_contained_for_unsafe_legacy_session_ids(
    tmp_path,
    monkeypatch,
    unsafe_session_id: str,
) -> None:
    monkeypatch.setattr(heartbeat, "LOCK_DIR", tmp_path)

    path = heartbeat.lock_path(unsafe_session_id)

    assert path.parent == tmp_path
    assert path.name.startswith("__invalid_session_id__")


def test_heartbeat_fork_cleanup_closes_inherited_lease_descriptors(tmp_path, monkeypatch) -> None:
    """The post-fork hook must not let a child keep the parent's advisory lock."""
    monkeypatch.setattr(heartbeat, "LOCK_DIR", tmp_path)
    session_id = "fork-inherited-lease"
    heartbeat.acquire(session_id)
    inherited_fd = heartbeat._HELD_LEASE_FDS[session_id]

    try:
        heartbeat._clear_held_leases_after_fork()

        assert session_id not in heartbeat._HELD_LEASE_FDS
        with pytest.raises(OSError):
            os.fstat(inherited_fd)
    finally:
        heartbeat.release(session_id)


def test_diagnostic_contract_builds_do_not_leak_registry_issuances() -> None:
    runner = _runner()
    issued_before = len(_PROCESS_LOCAL_AUTHORITY_REGISTRY._issued)

    for _ in range(25):
        contract = runner._build_execution_contract(seed=_seed())
        assert contract["foundation_a_authority"]["scope"] == "process_local"

    assert len(_PROCESS_LOCAL_AUTHORITY_REGISTRY._issued) == issued_before
