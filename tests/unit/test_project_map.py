"""Project Map is a complete read-only projection over session history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import subprocess
from unittest.mock import AsyncMock

from pydantic import ValidationError
import pytest

from ouroboros.core.errors import PersistenceError
from ouroboros.core.project_identity import ProjectIdentity
from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.events import create_session_started_event
from ouroboros.orchestrator.session import SessionRepository, SessionStatus
from ouroboros.persistence.event_store import EventStore
from ouroboros.project_map import (
    ProjectIdentityConflictError,
    ProjectMapBuilder,
    ProjectProjectionError,
    ProjectRecord,
    ProjectRunLimitError,
    ProjectRunSummary,
)


@pytest.fixture
async def event_store(tmp_path: Path):
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'project-map.db'}")
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture
def project(tmp_path: Path) -> ProjectIdentity:
    root = tmp_path / "project\nroot "
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return ProjectIdentity.from_root(root)


def _contract(identity: ProjectIdentity, *, workspace_path: str | None = None) -> dict:
    return {
        "frugality_proof": {
            "project_root": identity.project_root,
            "workspace_path": workspace_path or identity.workspace_path,
        }
    }


async def _create_current_session(
    store: EventStore,
    project: ProjectIdentity,
    *,
    session_id: str,
    execution_id: str,
    seed_id: str,
    status: SessionStatus = SessionStatus.RUNNING,
    workspace_path: str | None = None,
    gate_forced: bool | None = None,
) -> None:
    scoped = ProjectIdentity(
        project_id=project.project_id,
        project_root=project.project_root,
        workspace_path=workspace_path or project.workspace_path,
    )
    actual_workspace = Path(scoped.project_root) / scoped.workspace_path
    actual_workspace.mkdir(parents=True, exist_ok=True)
    sessions = SessionRepository(store)
    created = await sessions.create_session(
        execution_id,
        seed_id,
        session_id=session_id,
        seed_goal=f"goal for {session_id}",
        runtime_backend="codex_cli",
        llm_backend="openai",
        gate_forced=gate_forced,
        execution_contract=_contract(scoped),
        project_identity=scoped,
        project_workspace=str(actual_workspace),
    )
    assert created.is_ok
    if status is SessionStatus.COMPLETED:
        assert (await sessions.mark_completed(session_id)).is_ok
    elif status is SessionStatus.FAILED:
        assert (await sessions.mark_failed(session_id, "failed")).is_ok
    elif status is SessionStatus.CANCELLED:
        assert (await sessions.mark_cancelled(session_id, "cancelled")).is_ok
    elif status is SessionStatus.PAUSED:
        assert (await sessions.mark_paused(session_id, "paused")).is_ok


def _legacy_start(
    project: ProjectIdentity,
    *,
    session_id: str,
    execution_id: str,
    seed_id: str,
    workspace_path: str | None = None,
    timestamp: datetime | None = None,
) -> BaseEvent:
    return BaseEvent(
        id=f"event-{session_id}",
        type="orchestrator.session.started",
        timestamp=timestamp or datetime.now(UTC),
        aggregate_type="session",
        aggregate_id=session_id,
        data={
            "execution_id": execution_id,
            "seed_id": seed_id,
            "execution_contract": _contract(project, workspace_path=workspace_path),
        },
    )


@pytest.mark.asyncio
async def test_build_reuses_session_repository_status_and_is_read_only(
    event_store: EventStore,
    project: ProjectIdentity,
) -> None:
    expected = {
        "session-running": SessionStatus.RUNNING,
        "session-completed": SessionStatus.COMPLETED,
        "session-failed": SessionStatus.FAILED,
        "session-paused": SessionStatus.PAUSED,
        "session-cancelled": SessionStatus.CANCELLED,
    }
    for index, (session_id, status) in enumerate(expected.items()):
        await _create_current_session(
            event_store,
            project,
            session_id=session_id,
            execution_id=f"execution-{index}",
            seed_id=f"seed-{index}",
            status=status,
        )
    rowid_before = await event_store.get_current_rowid()

    record = await ProjectMapBuilder(event_store).build(project, limit=len(expected))

    assert record.run_count == len(expected)
    assert {run.session_id: run.status for run in record.runs} == expected
    assert {run.identity_source for run in record.runs} == {"session_start"}
    assert await event_store.get_current_rowid() == rowid_before
    for run in record.runs:
        reconstructed = await SessionRepository(event_store).reconstruct_session(run.session_id)
        assert reconstructed.is_ok
        assert run.status is reconstructed.value.status


@pytest.mark.asyncio
async def test_build_reports_ambiguity_gate_statistics(
    event_store: EventStore,
    project: ProjectIdentity,
) -> None:
    await _create_current_session(
        event_store,
        project,
        session_id="gated",
        execution_id="execution-gated",
        seed_id="seed-gated",
        gate_forced=False,
    )
    await _create_current_session(
        event_store,
        project,
        session_id="forced",
        execution_id="execution-forced",
        seed_id="seed-forced",
        gate_forced=True,
    )
    await _create_current_session(
        event_store,
        project,
        session_id="unknown",
        execution_id="execution-unknown",
        seed_id="seed-unknown",
    )

    record = await ProjectMapBuilder(event_store).build(project)

    assert record.gated_seed_count == 1
    assert record.forced_seed_count == 1
    assert record.unknown_seed_count == 1
    assert record.override_rate == 0.5
    assert {run.session_id: run.gate_forced for run in record.runs} == {
        "gated": False,
        "forced": True,
        "unknown": None,
    }


@pytest.mark.asyncio
async def test_build_returns_unknown_rate_when_no_gate_decisions(
    event_store: EventStore,
    project: ProjectIdentity,
) -> None:
    record = await ProjectMapBuilder(event_store).build(project)

    assert record.gated_seed_count == 0
    assert record.forced_seed_count == 0
    assert record.unknown_seed_count == 0
    assert record.override_rate is None


@pytest.mark.asyncio
async def test_nested_only_historical_start_is_labeled_execution_contract(
    event_store: EventStore,
    project: ProjectIdentity,
) -> None:
    await event_store.append(
        _legacy_start(
            project,
            session_id="legacy-session",
            execution_id="legacy-execution",
            seed_id="legacy-seed",
        )
    )

    record = await ProjectMapBuilder(event_store).build(project)

    assert record.run_count == 1
    assert record.runs[0].identity_source == "execution_contract"
    assert record.runs[0].project_root == project.project_root


@pytest.mark.asyncio
async def test_top_level_only_public_start_event_is_attributed(
    event_store: EventStore,
    project: ProjectIdentity,
) -> None:
    event = create_session_started_event(
        session_id="top-level-session",
        execution_id="top-level-execution",
        seed_id="top-level-seed",
        seed_goal="Project a public producer event",
        project_identity=project,
    )
    assert "execution_contract" not in event.data
    await event_store.append(event)

    record = await ProjectMapBuilder(event_store).build(project)

    assert record.run_count == 1
    assert record.runs[0].session_id == "top-level-session"
    assert record.runs[0].identity_source == "session_start"
    assert record.runs[0].project_id == project.project_id


@pytest.mark.asyncio
async def test_identity_conflict_aborts_the_complete_project_query(
    event_store: EventStore,
    project: ProjectIdentity,
) -> None:
    conflicting = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id="conflicting-session",
        data={
            "execution_id": "conflicting-execution",
            "seed_id": "conflicting-seed",
            **project.to_event_data(),
            "execution_contract": _contract(project, workspace_path="packages/other"),
        },
    )
    await event_store.append(conflicting)

    with pytest.raises(ProjectIdentityConflictError, match="conflicting"):
        await ProjectMapBuilder(event_store).build(project)


@pytest.mark.asyncio
async def test_partial_candidate_identity_is_a_typed_conflict(
    event_store: EventStore,
    project: ProjectIdentity,
) -> None:
    await event_store.append(
        BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id="partial-session",
            data={
                "execution_id": "partial-execution",
                "seed_id": "partial-seed",
                "project_id": project.project_id,
                "project_root": project.project_root,
                "execution_contract": _contract(project),
            },
        )
    )

    with pytest.raises(ProjectIdentityConflictError, match="partial"):
        await ProjectMapBuilder(event_store).build(project)


@pytest.mark.asyncio
async def test_duplicate_start_identity_fails_before_reconstruction(
    project: ProjectIdentity,
) -> None:
    first = _legacy_start(
        project,
        session_id="duplicate-session",
        execution_id="duplicate-execution",
        seed_id="duplicate-seed",
    )
    second = first.model_copy(update={"id": "duplicate-start-2"})
    store = AsyncMock()
    store.get_all_sessions.return_value = [first, second]
    builder = ProjectMapBuilder(store)
    builder._sessions.reconstruct_session = AsyncMock()

    with pytest.raises(ProjectIdentityConflictError, match="duplicate"):
        await builder.build(project)

    builder._sessions.reconstruct_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_workspace_filter_cannot_hide_project_identity_conflict(
    event_store: EventStore,
    project: ProjectIdentity,
) -> None:
    await _create_current_session(
        event_store,
        project,
        session_id="workspace-session",
        execution_id="workspace-execution",
        seed_id="workspace-seed",
        workspace_path="packages/app",
    )
    conflicting = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id="other-workspace-conflict",
        data={
            "execution_id": "other-execution",
            "seed_id": "other-seed",
            **ProjectIdentity(
                project_id=project.project_id,
                project_root=project.project_root,
                workspace_path="packages/other",
            ).to_event_data(),
            "execution_contract": _contract(project, workspace_path="packages/conflict"),
        },
    )
    await event_store.append(conflicting)

    with pytest.raises(ProjectIdentityConflictError):
        await ProjectMapBuilder(event_store).build(project, workspace_path="packages/app")


@pytest.mark.asyncio
async def test_limit_rejects_unmarked_partial_result(
    event_store: EventStore,
    project: ProjectIdentity,
) -> None:
    for index in range(2):
        await _create_current_session(
            event_store,
            project,
            session_id=f"session-{index}",
            execution_id=f"execution-{index}",
            seed_id=f"seed-{index}",
        )

    with pytest.raises(ProjectRunLimitError) as exc_info:
        await ProjectMapBuilder(event_store).build(project, limit=1)

    assert exc_info.value.actual_runs == 2
    assert exc_info.value.limit == 1
    assert (await ProjectMapBuilder(event_store).build(project, limit=2)).run_count == 2


@pytest.mark.asyncio
async def test_malformed_attributable_run_fails_the_whole_projection_with_typed_error(
    event_store: EventStore,
    project: ProjectIdentity,
) -> None:
    malformed = _legacy_start(
        project,
        session_id="malformed-session",
        execution_id="malformed-execution",
        seed_id="malformed-seed",
    )
    await event_store.append(
        malformed.model_copy(update={"data": {**malformed.data, "execution_id": " "}})
    )

    with pytest.raises(ProjectProjectionError, match="represent"):
        await ProjectMapBuilder(event_store).build(project)


@pytest.mark.asyncio
async def test_reconstruction_failure_never_returns_a_partial_record(
    event_store: EventStore,
    project: ProjectIdentity,
) -> None:
    await _create_current_session(
        event_store,
        project,
        session_id="unavailable-session",
        execution_id="unavailable-execution",
        seed_id="unavailable-seed",
    )
    builder = ProjectMapBuilder(event_store)
    builder._sessions.reconstruct_session = AsyncMock(
        return_value=Result.err(PersistenceError("unavailable"))
    )

    with pytest.raises(ProjectProjectionError, match="reconstruct"):
        await builder.build(project)

    builder._sessions.reconstruct_session.side_effect = RuntimeError("unavailable")
    with pytest.raises(ProjectProjectionError, match="reconstruct"):
        await builder.build(project)


@pytest.mark.asyncio
async def test_related_lifecycle_read_failure_aborts_the_projection(
    event_store: EventStore,
    project: ProjectIdentity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _create_current_session(
        event_store,
        project,
        session_id="related-read-session",
        execution_id="related-read-execution",
        seed_id="related-read-seed",
    )
    query_related = AsyncMock(side_effect=PersistenceError("related history unavailable"))
    monkeypatch.setattr(event_store, "query_session_related_events", query_related)

    with pytest.raises(ProjectProjectionError, match="reconstruct"):
        await ProjectMapBuilder(event_store).build(project)

    query_related.assert_awaited_once_with(
        session_id="related-read-session",
        execution_id="related-read-execution",
        limit=None,
    )


@pytest.mark.asyncio
async def test_workspace_filter_counts_only_the_requested_complete_population(
    event_store: EventStore,
    project: ProjectIdentity,
) -> None:
    for workspace in ("packages/app", "packages/other"):
        await _create_current_session(
            event_store,
            project,
            session_id=f"session-{workspace.rsplit('/', 1)[-1]}",
            execution_id=f"execution-{workspace.rsplit('/', 1)[-1]}",
            seed_id=f"seed-{workspace.rsplit('/', 1)[-1]}",
            workspace_path=workspace,
        )

    record = await ProjectMapBuilder(event_store).build(
        project,
        workspace_path="packages/app",
        limit=1,
    )

    assert record.run_count == 1
    assert record.workspace_path == "packages/app"
    assert record.runs[0].workspace_path == "packages/app"


@pytest.mark.asyncio
async def test_unrelated_and_identity_free_sessions_are_not_attributed(
    event_store: EventStore,
    project: ProjectIdentity,
    tmp_path: Path,
) -> None:
    other_root = tmp_path / "other"
    other_root.mkdir()
    subprocess.run(["git", "init", "-q", str(other_root)], check=True)
    other = ProjectIdentity.from_root(other_root)
    await _create_current_session(
        event_store,
        other,
        session_id="other-session",
        execution_id="other-execution",
        seed_id="other-seed",
    )
    created = await SessionRepository(event_store).create_session(
        "utility-execution",
        "utility-seed",
        session_id="utility-session",
    )
    assert created.is_ok

    record = await ProjectMapBuilder(event_store).build(project)

    assert record.run_count == 0


@pytest.mark.asyncio
async def test_run_order_is_deterministic_for_equal_timestamps(
    event_store: EventStore,
    project: ProjectIdentity,
) -> None:
    timestamp = datetime(2026, 7, 28, tzinfo=UTC)
    later = _legacy_start(
        project,
        session_id="session-b",
        execution_id="execution-b",
        seed_id="seed-b",
        timestamp=timestamp,
    )
    earlier = _legacy_start(
        project,
        session_id="session-a",
        execution_id="execution-a",
        seed_id="seed-a",
        timestamp=timestamp,
    )
    await event_store.append(later)
    await event_store.append(earlier)

    first = await ProjectMapBuilder(event_store).build(project)
    second = await ProjectMapBuilder(event_store).build(project)

    assert first == second
    assert first.model_dump_json() == second.model_dump_json()
    assert ProjectRecord.model_validate_json(first.model_dump_json()) == first
    assert [run.session_id for run in first.runs] == ["session-a", "session-b"]


@pytest.mark.asyncio
async def test_full_history_is_not_replaced_by_a_recent_window(
    event_store: EventStore,
    project: ProjectIdentity,
) -> None:
    base = datetime(2020, 1, 1, tzinfo=UTC)
    for index in range(105):
        await event_store.append(
            _legacy_start(
                project,
                session_id=f"historical-{index:03d}",
                execution_id=f"execution-{index:03d}",
                seed_id=f"seed-{index:03d}",
                timestamp=base + timedelta(days=index),
            )
        )

    record = await ProjectMapBuilder(event_store).build(project, limit=105)

    assert record.run_count == 105
    assert record.runs[0].session_id == "historical-000"
    assert record.runs[-1].session_id == "historical-104"


def test_project_records_are_frozen_and_population_checked(project: ProjectIdentity) -> None:
    record = ProjectRecord(
        project_id=project.project_id,
        project_root=project.project_root,
        run_count=0,
        runs=(),
    )

    assert record.run_count == 0
    with pytest.raises(ValidationError):
        record.run_count = 1  # type: ignore[misc]
    foreign_run = ProjectRunSummary.model_construct(
        project_id="other-project",
        project_root=project.project_root,
        workspace_path=project.workspace_path,
    )
    with pytest.raises(ValidationError):
        ProjectRecord(
            project_id=project.project_id,
            project_root=project.project_root,
            run_count=1,
            runs=(foreign_run,),
        )
