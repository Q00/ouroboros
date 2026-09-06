"""Read-only MCP contract for complete Project Map status."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from ouroboros.core.project_identity import ProjectIdentity, resolve_project_identity
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.tools.project_status_handler import ProjectStatusHandler
from ouroboros.persistence.event_store import EventStore, sqlite_database_url


def _git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _project(root: Path) -> ProjectIdentity:
    root.mkdir()
    _git("init", "-q", str(root))
    return resolve_project_identity(root)


def _start(
    project: ProjectIdentity,
    *,
    session_id: str,
    workspace_path: str = ".",
    nested_workspace_path: str | None = None,
    gate_forced: bool | None = None,
) -> BaseEvent:
    scoped = ProjectIdentity(
        project_id=project.project_id,
        project_root=project.project_root,
        workspace_path=workspace_path,
    )
    data: dict[str, object] = {
        "execution_id": f"execution-{session_id}",
        "seed_id": f"seed-{session_id}",
        "seed_goal": f"goal-{session_id}",
        **scoped.to_event_data(),
    }
    if nested_workspace_path is not None:
        data["execution_contract"] = {
            "frugality_proof": {
                "project_root": project.project_root,
                "workspace_path": nested_workspace_path,
            }
        }
    if gate_forced is not None:
        data["gate_forced"] = gate_forced
    return BaseEvent(
        id=f"event-{session_id}",
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id=session_id,
        data=data,
    )


@pytest.mark.asyncio
async def test_handler_returns_structured_record_without_writing(tmp_path: Path) -> None:
    project = _project(tmp_path / "project")
    store = EventStore(sqlite_database_url(tmp_path / "events.db"))
    await store.initialize()
    await store.append(_start(project, session_id="one"))
    rowid_before = await store.get_current_rowid()

    result = await ProjectStatusHandler(event_store=store).handle(
        {"project_dir": project.project_root, "limit": 1}
    )

    assert result.is_ok
    payload = result.value.structured_content
    assert isinstance(payload, dict)
    assert payload["project_id"] == project.project_id
    assert payload["run_count"] == 1
    assert payload["gated_seed_count"] == 0
    assert payload["forced_seed_count"] == 0
    assert payload["unknown_seed_count"] == 1
    assert payload["override_rate"] is None
    assert payload["runs"][0]["status"] == "running"
    assert result.value.meta == {
        "read_only": True,
        "project_id": project.project_id,
        "run_count": 1,
    }
    assert await store.get_current_rowid() == rowid_before
    assert result.value.text_content.startswith("Project Status\n")
    await store.close()


@pytest.mark.asyncio
async def test_handler_formats_ambiguity_gate_statistics(tmp_path: Path) -> None:
    project = _project(tmp_path / "project")
    store = EventStore(sqlite_database_url(tmp_path / "events.db"))
    await store.initialize()
    await store.append(_start(project, session_id="gated", gate_forced=False))
    await store.append(_start(project, session_id="forced", gate_forced=True))

    result = await ProjectStatusHandler(event_store=store).handle(
        {"project_dir": project.project_root, "limit": 10}
    )

    assert result.is_ok
    text = result.value.text_content
    assert "Gated Seeds: 1" in text
    assert "Forced Seeds: 1" in text
    assert "Unknown Seeds: 0" in text
    assert "Override rate: 50.0%" in text
    await store.close()


@pytest.mark.asyncio
async def test_workspace_filter_and_empty_project_are_complete(tmp_path: Path) -> None:
    project = _project(tmp_path / "project")
    store = EventStore(sqlite_database_url(tmp_path / "events.db"))
    await store.initialize()
    await store.append(_start(project, session_id="app", workspace_path="packages/app"))
    await store.append(_start(project, session_id="other", workspace_path="packages/other"))
    handler = ProjectStatusHandler(event_store=store)

    filtered = await handler.handle(
        {
            "project_dir": project.project_root,
            "workspace": "packages/app",
            "limit": 1,
        }
    )
    empty = await handler.handle(
        {
            "project_dir": project.project_root,
            "workspace": "packages/missing",
            "limit": 1,
        }
    )

    assert filtered.is_ok
    assert filtered.value.structured_content["workspace_path"] == "packages/app"
    assert [run["session_id"] for run in filtered.value.structured_content["runs"]] == ["app"]
    assert empty.is_ok
    assert empty.value.structured_content["run_count"] == 0
    assert empty.value.structured_content["runs"] == []
    await store.close()


@pytest.mark.asyncio
async def test_conflict_and_undersized_limit_return_no_partial_record(tmp_path: Path) -> None:
    project = _project(tmp_path / "project")
    store = EventStore(sqlite_database_url(tmp_path / "events.db"))
    await store.initialize()
    await store.append(_start(project, session_id="one"))
    await store.append(_start(project, session_id="two"))
    handler = ProjectStatusHandler(event_store=store)

    limited = await handler.handle({"project_dir": project.project_root, "limit": 1})

    assert limited.is_err
    assert "exceeding" in str(limited.error)

    await store.append(
        _start(
            project,
            session_id="conflict",
            workspace_path="packages/app",
            nested_workspace_path="packages/other",
        )
    )
    conflicted = await handler.handle(
        {
            "project_dir": project.project_root,
            "workspace": "packages/missing",
            "limit": 100,
        }
    )

    assert conflicted.is_err
    assert "conflict" in str(conflicted.error).lower()
    await store.close()


@pytest.mark.asyncio
async def test_missing_schema_is_not_created_by_read_only_handler(tmp_path: Path) -> None:
    project = _project(tmp_path / "project")
    database = tmp_path / "missing-schema.db"
    database.touch()
    size_before = database.stat().st_size
    store = EventStore(sqlite_database_url(database), read_only=True)

    result = await ProjectStatusHandler(event_store=store).handle(
        {"project_dir": project.project_root}
    )

    assert result.is_err
    assert database.stat().st_size == size_before
    await store.close()


@pytest.mark.asyncio
async def test_linked_worktree_resolves_to_the_source_project(tmp_path: Path) -> None:
    project = _project(tmp_path / "source")
    source = Path(project.project_root)
    _git("config", "user.name", "Ouroboros Test", cwd=source)
    _git("config", "user.email", "ouroboros@example.invalid", cwd=source)
    (source / "tracked.txt").write_text("tracked\n")
    _git("add", "tracked.txt", cwd=source)
    _git("commit", "-q", "-m", "initial", cwd=source)
    worktree = tmp_path / "linked"
    _git("worktree", "add", "-q", "-b", "linked-test", str(worktree), cwd=source)

    store = EventStore(sqlite_database_url(tmp_path / "events.db"))
    await store.initialize()
    await store.append(_start(project, session_id="source"))

    result = await ProjectStatusHandler(event_store=store).handle({"project_dir": str(worktree)})

    assert result.is_ok
    assert result.value.structured_content["project_id"] == project.project_id
    assert result.value.structured_content["project_root"] == project.project_root
    assert result.value.structured_content["run_count"] == 1
    await store.close()


def test_definition_declares_read_only_structured_contract() -> None:
    definition = ProjectStatusHandler().definition

    assert definition.name == "ouroboros_project_status"
    assert definition.annotations == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    assert definition.output_schema is not None
    assert definition.output_schema["title"] == "ProjectRecord"
    assert json.loads(json.dumps(definition.output_schema))["type"] == "object"
