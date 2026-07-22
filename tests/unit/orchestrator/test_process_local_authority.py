"""Foundation A process-local authority lifecycle regressions."""

from __future__ import annotations

import asyncio
import os
import pickle
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.core.types import Result
from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler
from ouroboros.orchestrator import heartbeat
from ouroboros.orchestrator.adapter import FULL_CAPABILITIES, AgentMessage
from ouroboros.orchestrator.execution_authority import _PROCESS_LOCAL_AUTHORITY_REGISTRY
from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog
from ouroboros.orchestrator.runner import (
    EXECUTION_CONTRACT_PROGRESS_KEY,
    OrchestratorError,
    OrchestratorResult,
    OrchestratorRunner,
)
from ouroboros.orchestrator.session import SessionStatus, SessionTracker


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
    restarted._session_repo.mark_failed = AsyncMock(return_value=Result.ok(None))

    # Simulate the creating process exiting: both its registry entry and its
    # early liveness lease disappear before another process observes RUNNING.
    original._retire_process_local_authority(
        session_id=tracker.session_id,
        execution_id=tracker.execution_id,
    )
    result = await restarted.resume_session(tracker.session_id, _seed())

    assert result.is_err
    assert result.error.details["resume_blocked"] == "process_local_resume_unavailable"
    restarted._session_repo.mark_failed.assert_awaited_once()


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

    assert not runner._has_live_process_local_authority(
        tracker.session_id,
        tracker.execution_id,
        tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY],
    )


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

    with patch.object(
        runner._session_repo,
        "create_session",
        AsyncMock(return_value=Result.ok(returned)),
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
