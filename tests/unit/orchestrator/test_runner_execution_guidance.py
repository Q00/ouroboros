"""Runner wiring for declared project execution guidance."""

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.config import get_default_config
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.core.types import Result
from ouroboros.orchestrator.adapter import FULL_CAPABILITIES, AgentMessage, ParamSupport
from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog
from ouroboros.orchestrator.runner import (
    EXECUTION_CONTRACT_PROGRESS_KEY,
    OrchestratorError,
    OrchestratorResult,
    OrchestratorRunner,
    build_system_prompt,
)
from ouroboros.orchestrator.session import SessionStatus, SessionTracker


def _seed() -> Seed:
    return Seed(
        goal="Apply declared project guidance",
        acceptance_criteria=("Guidance is applied",),
        ontology_schema=OntologySchema(name="Guidance", description="Execution guidance"),
        metadata=SeedMetadata(seed_id="seed-guidance"),
    )


def _write_guidance(root: Path, guidance_id: str, text: str) -> Path:
    path = root / ".ouroboros" / "guidance" / guidance_id / "GUIDANCE.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _runner(root: Path, guidance_ids: tuple[str, ...] = ()) -> tuple[OrchestratorRunner, AsyncMock]:
    adapter = MagicMock()
    adapter.runtime_backend = "opencode"
    adapter.llm_backend = "test_llm"
    adapter.working_directory = str(root)
    adapter.permission_mode = "acceptEdits"
    adapter._model = "test-model"
    adapter.capabilities = FULL_CAPABILITIES

    event_store = AsyncMock()
    event_store.append = AsyncMock()
    event_store.replay = AsyncMock(return_value=[])
    config = get_default_config()
    execution = config.execution.model_copy(update={"project_guidance": guidance_ids})
    config = config.model_copy(update={"execution": execution})
    with patch("ouroboros.config.load_config", return_value=config):
        return OrchestratorRunner(adapter, event_store, MagicMock()), event_store


def test_empty_guidance_preserves_prompt_bytes() -> None:
    seed = _seed()
    baseline = build_system_prompt(seed)

    assert build_system_prompt(seed, guidance_fragment="") == baseline


def test_execution_contract_persists_declared_guidance(tmp_path: Path) -> None:
    _write_guidance(tmp_path, "team", "Use the project conventions.\n")
    runner, _store = _runner(tmp_path, ("team",))

    contract = runner._build_execution_contract(seed=_seed())

    guidance = contract["guidance"]
    assert guidance["mode"] == "declared"
    assert guidance["provenance_scope"] == "ouroboros_declared_guidance_only"
    assert guidance["items"][0]["stable_id"] == "guidance:project:team"
    assert guidance["items"][0]["source"] == "project"
    assert guidance["items"][0]["stage"] == "execute"
    assert guidance["items"][0]["role"] == "implementation"


def test_resume_uses_persisted_ids_not_current_config(tmp_path: Path) -> None:
    _write_guidance(tmp_path, "team", "Use the project conventions.\n")
    original, _store = _runner(tmp_path, ("team",))
    persisted = original._build_execution_contract(seed=_seed())

    resumed, _store = _runner(tmp_path, ())
    changed = resumed._restore_execution_contract(
        {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
        seed=_seed(),
    )

    assert changed is False
    assert resumed._execution_guidance is not None
    assert [ref.guidance_id for ref in resumed._execution_guidance.refs] == ["team"]


def test_resume_rejects_changed_guidance_content(tmp_path: Path) -> None:
    path = _write_guidance(tmp_path, "team", "Use the project conventions.\n")
    original, _store = _runner(tmp_path, ("team",))
    persisted = original._build_execution_contract(seed=_seed())
    path.write_text("Changed conventions.\n", encoding="utf-8")

    resumed, _store = _runner(tmp_path, ())
    with pytest.raises(OrchestratorError, match="project guidance changed"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_proof_cohort_identity_includes_guidance_hash(tmp_path: Path) -> None:
    _write_guidance(tmp_path, "team", "Use the project conventions.\n")
    guided, _store = _runner(tmp_path, ("team",))
    unguided, _store = _runner(tmp_path, ())

    guided_identity = guided._proof_cohort_identity(
        {
            "seed_id": _seed().metadata.seed_id,
            EXECUTION_CONTRACT_PROGRESS_KEY: guided._build_execution_contract(seed=_seed()),
        }
    )
    unguided_identity = unguided._proof_cohort_identity(
        {
            "seed_id": _seed().metadata.seed_id,
            EXECUTION_CONTRACT_PROGRESS_KEY: unguided._build_execution_contract(seed=_seed()),
        }
    )

    assert guided_identity is not None
    assert unguided_identity is not None
    assert guided_identity != unguided_identity
    assert guided_identity[-1] != unguided_identity[-1]


@pytest.mark.asyncio
async def test_each_new_run_reloads_declared_guidance(tmp_path: Path) -> None:
    path = _write_guidance(tmp_path, "team", "First version.\n")
    runner, _store = _runner(tmp_path, ("team",))
    trackers = [
        SessionTracker.create("exec-one", _seed().metadata.seed_id, session_id="sess-one"),
        SessionTracker.create("exec-two", _seed().metadata.seed_id, session_id="sess-two"),
    ]

    with (
        patch.object(
            runner._session_repo,
            "create_session",
            AsyncMock(side_effect=[Result.ok(trackers[0]), Result.ok(trackers[1])]),
        ),
        patch.object(
            runner._session_repo,
            "track_progress",
            AsyncMock(return_value=Result.ok(None)),
        ),
    ):
        first = await runner.prepare_session(_seed(), execution_id="exec-one")
        assert first.is_ok
        assert runner._execution_contract is not None
        first_hash = runner._execution_contract["guidance"]["rendered_fragment_hash"]

        path.write_text("Second version.\n", encoding="utf-8")
        second = await runner.prepare_session(_seed(), execution_id="exec-two")
        assert second.is_ok
        assert runner._execution_contract is not None
        second_hash = runner._execution_contract["guidance"]["rendered_fragment_hash"]

    assert first_hash != second_hash


def test_legacy_contract_resumes_without_newly_configured_guidance(tmp_path: Path) -> None:
    _write_guidance(tmp_path, "team", "Use the project conventions.\n")
    runner, _store = _runner(tmp_path, ("team",))

    changed = runner._restore_execution_contract({}, seed=_seed())

    assert changed is True
    assert runner._execution_guidance is not None
    assert runner._execution_guidance.refs == ()


@pytest.mark.asyncio
async def test_records_bounded_guidance_injection_event(tmp_path: Path) -> None:
    _write_guidance(tmp_path, "team", "Use the project conventions.\n")
    runner, event_store = _runner(tmp_path, ("team",))
    runner._build_execution_contract(seed=_seed())

    await runner._record_execution_guidance_injection(
        session_id="sess-guidance",
        execution_id="exec-guidance",
    )

    event = event_store.append.await_args.args[0]
    assert event.type == "orchestrator.guidance.injected"
    assert event.data["delivery_mode"] == "native"
    assert event.data["injection_key"] == "start"
    assert event.data["guidance_refs"][0]["stable_id"] == "guidance:project:team"
    assert "content" not in event.data["guidance_refs"][0]


@pytest.mark.asyncio
async def test_guidance_injection_replay_deduplicates_same_attempt(tmp_path: Path) -> None:
    _write_guidance(tmp_path, "team", "Use the project conventions.\n")
    runner, event_store = _runner(tmp_path, ("team",))
    runner._build_execution_contract(seed=_seed())

    await runner._record_execution_guidance_injection(
        session_id="sess-guidance",
        execution_id="exec-guidance",
        injection_key="resume:3",
    )
    persisted = event_store.append.await_args.args[0]
    event_store.append.reset_mock()
    event_store.replay.return_value = [persisted]

    await runner._record_execution_guidance_injection(
        session_id="sess-guidance",
        execution_id="exec-guidance",
        injection_key="resume:3",
    )

    event_store.append.assert_not_awaited()


@pytest.mark.asyncio
async def test_guidance_provenance_persistence_is_fail_closed(tmp_path: Path) -> None:
    _write_guidance(tmp_path, "team", "Use the project conventions.\n")
    runner, event_store = _runner(tmp_path, ("team",))
    runner._build_execution_contract(seed=_seed())
    event_store.append.side_effect = RuntimeError("store unavailable")

    with pytest.raises(OrchestratorError, match="persist.*guidance provenance"):
        await runner._record_execution_guidance_injection(
            session_id="sess-guidance",
            execution_id="exec-guidance",
        )


@pytest.mark.asyncio
async def test_execute_seed_does_not_call_runtime_that_ignores_guidance(tmp_path: Path) -> None:
    _write_guidance(tmp_path, "team", "Use the project conventions.\n")
    runner, event_store = _runner(tmp_path, ("team",))
    runner._adapter.capabilities = replace(
        FULL_CAPABILITIES,
        system_prompt_support=ParamSupport.IGNORED,
    )
    execute_task = MagicMock()
    runner._adapter.execute_task = execute_task
    create_session = AsyncMock()

    with patch.object(runner._session_repo, "create_session", create_session):
        result = await runner.execute_seed(_seed(), parallel=False)

    assert result.is_err
    assert "cannot deliver declared project execution guidance" in str(result.error).lower()
    create_session.assert_not_called()
    execute_task.assert_not_called()


@pytest.mark.asyncio
async def test_execute_seed_delivers_guidance_to_adapter_system_prompt(tmp_path: Path) -> None:
    sentinel = "GUIDANCE_SENTINEL_FRESH"
    _write_guidance(tmp_path, "team", f"Follow project conventions. {sentinel}\n")
    runner, event_store = _runner(tmp_path, ("team",))
    tracker = SessionTracker.create(
        "exec-guidance-fresh",
        _seed().metadata.seed_id,
        session_id="sess-guidance-fresh",
    )
    captured: dict[str, Any] = {}
    call_order: list[str] = []

    async def append_event(event: Any) -> None:
        call_order.append(event.type)

    event_store.append.side_effect = append_event

    async def execute_task(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
        del args
        call_order.append("runtime.execute_task")
        captured.update(kwargs)
        yield AgentMessage(
            type="result",
            content="[TASK_COMPLETE]",
            data={"subtype": "success"},
        )

    runner._adapter.execute_task = execute_task
    tool_catalog = assemble_session_tool_catalog(["Read"])
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
        patch.object(
            runner._session_repo,
            "mark_completed",
            AsyncMock(return_value=Result.ok(None)),
        ),
        patch.object(runner, "_check_startup_cancellation", AsyncMock(return_value=False)),
        patch.object(
            runner,
            "_get_merged_tools",
            AsyncMock(return_value=(["Read"], None, tool_catalog)),
        ),
        patch.object(runner, "_evaluate_frugality_proof", AsyncMock()),
    ):
        result = await runner.execute_seed(_seed(), parallel=False)

    assert result.is_ok
    assert call_order.index("orchestrator.guidance.injected") < call_order.index(
        "runtime.execute_task"
    )
    assert sentinel in captured["system_prompt"]
    assert "The Seed and its Acceptance Criteria take precedence" in captured["system_prompt"]


@pytest.mark.asyncio
async def test_parallel_execution_receives_declared_guidance(tmp_path: Path) -> None:
    sentinel = "GUIDANCE_SENTINEL_PARALLEL"
    _write_guidance(tmp_path, "team", f"Parallel conventions. {sentinel}\n")
    runner, _store = _runner(tmp_path, ("team",))
    seed = _seed().model_copy(
        update={"acceptance_criteria": ("First criterion", "Second criterion")}
    )
    tracker = SessionTracker.create(
        "exec-guidance-parallel",
        seed.metadata.seed_id,
        session_id="sess-guidance-parallel",
    )
    expected = Result.ok(
        OrchestratorResult(
            success=True,
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
    )
    tool_catalog = assemble_session_tool_catalog(["Read"])

    with (
        patch.object(runner, "_check_startup_cancellation", AsyncMock(return_value=False)),
        patch.object(
            runner,
            "_get_merged_tools",
            AsyncMock(return_value=(["Read"], None, tool_catalog)),
        ),
        patch.object(runner, "_execute_parallel", AsyncMock(return_value=expected)) as execute,
    ):
        result = await runner.execute_precreated_session(seed, tracker, parallel=True)

    assert result is expected
    assert sentinel in execute.await_args.kwargs["system_prompt"]


@pytest.mark.asyncio
async def test_fresh_runner_executes_precreated_session_with_persisted_guidance(
    tmp_path: Path,
) -> None:
    sentinel = "GUIDANCE_SENTINEL_PRECREATED"
    _write_guidance(tmp_path, "team", f"Pre-created conventions. {sentinel}\n")
    preparing_runner, _store = _runner(tmp_path, ("team",))
    seed = _seed()
    prepared_tracker = SessionTracker.create(
        "exec-guidance-precreated",
        seed.metadata.seed_id,
        session_id="sess-guidance-precreated",
    )

    with (
        patch.object(
            preparing_runner._session_repo,
            "create_session",
            AsyncMock(return_value=Result.ok(prepared_tracker)),
        ),
        patch.object(
            preparing_runner._session_repo,
            "track_progress",
            AsyncMock(return_value=Result.ok(None)),
        ),
    ):
        prepared = await preparing_runner.prepare_session(
            seed,
            execution_id=prepared_tracker.execution_id,
            session_id=prepared_tracker.session_id,
        )

    assert prepared.is_ok

    executing_runner, _store = _runner(tmp_path, ())
    captured: dict[str, Any] = {}

    async def execute_task(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
        del args
        captured.update(kwargs)
        yield AgentMessage(
            type="result",
            content="[TASK_COMPLETE]",
            data={"subtype": "success"},
        )

    executing_runner._adapter.execute_task = execute_task
    tool_catalog = assemble_session_tool_catalog(["Read"])
    with (
        patch.object(
            executing_runner._session_repo,
            "track_progress",
            AsyncMock(return_value=Result.ok(None)),
        ),
        patch.object(
            executing_runner._session_repo,
            "mark_completed",
            AsyncMock(return_value=Result.ok(None)),
        ),
        patch.object(
            executing_runner,
            "_check_startup_cancellation",
            AsyncMock(return_value=False),
        ),
        patch.object(
            executing_runner,
            "_get_merged_tools",
            AsyncMock(return_value=(["Read"], None, tool_catalog)),
        ),
        patch.object(executing_runner, "_evaluate_frugality_proof", AsyncMock()),
    ):
        result = await executing_runner.execute_precreated_session(
            seed,
            prepared.value,
            parallel=False,
        )

    assert result.is_ok
    assert executing_runner._project_guidance_ids == ()
    assert sentinel in captured["system_prompt"]


@pytest.mark.asyncio
async def test_resume_delivers_persisted_guidance_to_adapter_system_prompt(tmp_path: Path) -> None:
    sentinel = "GUIDANCE_SENTINEL_RESUME"
    _write_guidance(tmp_path, "team", f"Preserve project conventions. {sentinel}\n")
    original, _store = _runner(tmp_path, ("team",))
    seed = _seed()
    persisted_contract = original._build_execution_contract(seed=seed)

    resumed, event_store = _runner(tmp_path, ())
    event_store.replay = AsyncMock(return_value=[])
    tracker = SessionTracker.create(
        "exec-guidance-resume",
        seed.metadata.seed_id,
        session_id="sess-guidance-resume",
    ).with_status(SessionStatus.PAUSED)
    tracker = tracker.with_progress(
        {
            EXECUTION_CONTRACT_PROGRESS_KEY: persisted_contract,
            "messages_processed": 0,
        }
    )
    captured: dict[str, Any] = {}

    async def execute_task(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
        del args
        captured.update(kwargs)
        yield AgentMessage(
            type="result",
            content="[TASK_COMPLETE]",
            data={"subtype": "success"},
        )

    resumed._adapter.execute_task = execute_task
    tool_catalog = assemble_session_tool_catalog(["Read"])
    with (
        patch.object(
            resumed._session_repo,
            "reconstruct_session",
            AsyncMock(return_value=Result.ok(tracker)),
        ),
        patch.object(
            resumed._session_repo,
            "track_progress",
            AsyncMock(return_value=Result.ok(None)),
        ),
        patch.object(
            resumed._session_repo,
            "mark_completed",
            AsyncMock(return_value=Result.ok(None)),
        ),
        patch.object(
            resumed,
            "_get_merged_tools",
            AsyncMock(return_value=(["Read"], None, tool_catalog)),
        ),
        patch.object(resumed, "_evaluate_frugality_proof", AsyncMock()),
    ):
        result = await resumed.resume_session(tracker.session_id, seed)

    assert result.is_ok
    assert resumed._project_guidance_ids == ()
    assert sentinel in captured["system_prompt"]
    guidance_events = [
        call.args[0]
        for call in event_store.append.await_args_list
        if call.args[0].type == "orchestrator.guidance.injected"
    ]
    assert len(guidance_events) == 1
    assert guidance_events[0].data["injection_key"] == "resume:0"
