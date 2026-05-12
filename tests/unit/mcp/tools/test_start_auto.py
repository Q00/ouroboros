"""Tests for StartAutoHandler — fire-and-forget ``ooo auto`` wrapper.

Mirrors :mod:`test_start_evaluate`. The synchronous ``ouroboros_auto`` tool
routinely exceeds an MCP client's tool-call timeout because the Socratic
interview + repair loops + (optional) Ralph chain run end-to-end. The fire-
and-forget handler must return a ``job_id`` immediately and run the pipeline
under a :class:`JobManager`-backed background task.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import inspect
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.auto.state import AutoPipelineState, AutoStore
from ouroboros.core.types import Result
from ouroboros.mcp.tools.auto_handler import AutoHandler, StartAutoHandler
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.persistence.event_store import EventStore


@pytest.fixture
async def event_store():
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture
def fake_inner_auto():
    """An AutoHandler stub whose ``handle`` returns a canned ok result."""
    inner = MagicMock(spec=AutoHandler)
    inner.handle = AsyncMock(
        return_value=Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="ran"),),
                is_error=False,
                meta={"auto_session_id": "auto_xyz"},
            )
        )
    )
    return inner


class TestDefinition:
    def test_tool_name(self) -> None:
        assert StartAutoHandler().definition.name == "ouroboros_start_auto"

    def test_description_mentions_background(self) -> None:
        description = StartAutoHandler().definition.description.lower()
        assert "background" in description
        assert "auto_session_id + job_id immediately" in description

    def test_parameters_mirror_auto(self) -> None:
        h = StartAutoHandler()
        inner = AutoHandler()
        assert {p.name for p in h.definition.parameters} == {
            p.name for p in inner.definition.parameters
        }


class TestRequiredArguments:
    @pytest.mark.asyncio
    async def test_missing_goal_and_resume_errors(self, event_store) -> None:
        h = StartAutoHandler(event_store=event_store)
        result = await h.handle({})
        assert result.is_err
        assert "goal" in result.error.message

    @pytest.mark.asyncio
    async def test_blank_goal_and_blank_resume_errors(self, event_store) -> None:
        h = StartAutoHandler(event_store=event_store)
        result = await h.handle({"goal": "   ", "resume": "   "})
        assert result.is_err

    @pytest.mark.asyncio
    async def test_missing_resume_session_errors_before_enqueue(
        self, event_store, tmp_path
    ) -> None:
        job_manager = MagicMock()
        job_manager.start_job = AsyncMock()
        h = StartAutoHandler(
            event_store=event_store,
            job_manager=job_manager,
            store=AutoStore(tmp_path),
        )

        result = await h.handle({"resume": "auto_missing123"})

        assert result.is_err
        assert "Auto session not found" in result.error.message
        job_manager.start_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_resume_argument_is_trimmed_for_enqueued_runner(
        self, event_store, tmp_path
    ) -> None:
        store = AutoStore(tmp_path)
        state = AutoPipelineState(goal="build a CLI", cwd=str(tmp_path))
        store.save(state)
        job_manager = MagicMock()
        snapshot = MagicMock()
        snapshot.job_id = "job_auto_resume"
        captured: dict[str, object] = {}

        async def _start_job(*, runner, **_):
            captured["runner"] = runner
            return snapshot

        job_manager.start_job = AsyncMock(side_effect=_start_job)
        h = StartAutoHandler(event_store=event_store, job_manager=job_manager, store=store)
        inner = MagicMock(spec=AutoHandler)
        inner.handle = AsyncMock(
            return_value=Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="ran"),),
                    is_error=False,
                    meta={"auto_session_id": state.auto_session_id},
                )
            )
        )
        h._inner_auto = inner

        result = await h.handle({"resume": f" {state.auto_session_id} "})

        assert result.is_ok
        await captured["runner"]
        inner.handle.assert_awaited_once()
        assert inner.handle.await_args.args[0]["resume"] == state.auto_session_id


class TestBackgroundJobPath:
    @pytest.mark.asyncio
    async def test_returns_job_and_auto_session_id_immediately(
        self, event_store, fake_inner_auto, tmp_path
    ) -> None:
        job_manager = MagicMock()
        snapshot = MagicMock()
        snapshot.job_id = "job_auto_001"
        captured: dict[str, object] = {}

        async def _start_job(*, runner, **_):
            captured.update(_)
            if inspect.iscoroutine(runner):
                runner.close()
            return snapshot

        job_manager.start_job = AsyncMock(side_effect=_start_job)

        store = AutoStore(tmp_path)
        h = StartAutoHandler(event_store=event_store, job_manager=job_manager, store=store)
        # Inject the fake inner so we don't accidentally fire a real pipeline.
        h._inner_auto = fake_inner_auto

        result = await h.handle({"goal": "build a CLI"})
        assert result.is_ok
        assert "job_auto_001" in result.value.content[0].text
        auto_session_id = result.value.meta["auto_session_id"]
        assert isinstance(auto_session_id, str)
        assert auto_session_id.startswith("auto_")
        assert f"Auto session ID: {auto_session_id}" in result.value.content[0].text
        assert result.value.meta["job_id"] == "job_auto_001"
        assert result.value.meta["session_id"] == auto_session_id
        assert result.value.meta["dispatch_mode"] == "job"
        assert captured["links"].session_id == auto_session_id
        assert store.path_for(auto_session_id).exists()
        # The inner AutoHandler must NOT have run synchronously — the runner is
        # enqueued on the JobManager only.
        fake_inner_auto.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_plugin_mode_returns_subagent_without_enqueue(
        self, event_store, tmp_path, fake_inner_auto
    ) -> None:
        job_manager = MagicMock()
        job_manager.start_job = AsyncMock()
        store = AutoStore(tmp_path)
        h = StartAutoHandler(
            event_store=event_store,
            job_manager=job_manager,
            store=store,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )
        h._inner_auto = fake_inner_auto

        result = await h.handle({"goal": "build a CLI"})

        assert result.is_ok
        meta = result.value.meta
        assert meta["job_id"] is None
        assert meta["status"] == "delegated_to_plugin"
        assert meta["dispatch_mode"] == "plugin"
        assert isinstance(meta["auto_session_id"], str)
        assert store.path_for(meta["auto_session_id"]).exists()
        assert meta["_subagent"]["tool_name"] == "ouroboros_start_auto"
        assert meta["_subagent"]["context"]["arguments"]["resume"] == meta["auto_session_id"]
        body = json.loads(result.value.content[0].text)
        assert body["auto_session_id"] == meta["auto_session_id"]
        job_manager.start_job.assert_not_called()
        fake_inner_auto.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_enqueue_failure_returns_persisted_auto_session_id(
        self, event_store, tmp_path, fake_inner_auto
    ) -> None:
        job_manager = MagicMock()
        job_manager.start_job = AsyncMock(side_effect=RuntimeError("queue unavailable"))
        store = AutoStore(tmp_path)
        h = StartAutoHandler(event_store=event_store, job_manager=job_manager, store=store)
        h._inner_auto = fake_inner_auto

        result = await h.handle({"goal": "build a CLI"})

        assert result.is_err
        persisted = list(tmp_path.glob("auto_*.json"))
        assert len(persisted) == 1
        auto_session_id = persisted[0].stem
        assert auto_session_id in result.error.message
        assert result.error.details["auto_session_id"] == auto_session_id
        assert "resume" in result.error.message
        fake_inner_auto.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_active_background_job_for_session_errors_before_enqueue(
        self, event_store, tmp_path, fake_inner_auto
    ) -> None:
        store = AutoStore(tmp_path)
        state = AutoPipelineState(goal="build a CLI", cwd=str(tmp_path))
        store.save(state)
        active_snapshot = MagicMock()
        active_snapshot.job_id = "job_auto_active"
        active_snapshot.status.value = "running"
        job_manager = MagicMock()
        job_manager.find_active_job_by_session = AsyncMock(return_value=active_snapshot)
        job_manager.start_job = AsyncMock()
        h = StartAutoHandler(event_store=event_store, job_manager=job_manager, store=store)
        h._inner_auto = fake_inner_auto

        result = await h.handle({"resume": state.auto_session_id})

        assert result.is_err
        assert state.auto_session_id in result.error.message
        assert result.error.details["job_id"] == "job_auto_active"
        job_manager.start_job.assert_not_called()
        fake_inner_auto.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_pending_lease_blocks_concurrent_resume_before_job_row_exists(
        self, event_store, tmp_path, fake_inner_auto
    ) -> None:
        store = AutoStore(tmp_path)
        state = AutoPipelineState(goal="build a CLI", cwd=str(tmp_path))
        store.save(state)
        started = asyncio.Event()
        release = asyncio.Event()
        job_manager = MagicMock()
        job_manager.find_active_job_by_session = AsyncMock(return_value=None)
        snapshot = MagicMock()
        snapshot.job_id = "job_auto_lease"

        async def _start_job(*, runner, **_):
            if inspect.iscoroutine(runner):
                runner.close()
            started.set()
            await release.wait()
            return snapshot

        job_manager.start_job = AsyncMock(side_effect=_start_job)
        h = StartAutoHandler(event_store=event_store, job_manager=job_manager, store=store)
        h._inner_auto = fake_inner_auto

        first = asyncio.create_task(h.handle({"resume": state.auto_session_id}))
        await asyncio.wait_for(started.wait(), timeout=2.0)
        second = await h.handle({"resume": state.auto_session_id})
        release.set()
        first_result = await first

        assert second.is_err
        assert "pending start lease" in second.error.message
        assert second.error.details["auto_session_id"] == state.auto_session_id
        assert first_result.is_ok
        assert first_result.value.meta["job_id"] == "job_auto_lease"
        fake_inner_auto.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_active_plugin_lease_for_session_errors_before_redispatch(
        self, event_store, tmp_path, fake_inner_auto
    ) -> None:
        store = AutoStore(tmp_path)
        state = AutoPipelineState(goal="build a CLI", cwd=str(tmp_path))
        store.save(state)
        store.path_for(state.auto_session_id).with_suffix(".start_auto_lease.json").write_text(
            json.dumps(
                {
                    "token": "lease_active",
                    "mode": "plugin_dispatched",
                    "created_at": datetime.now(UTC).isoformat(),
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        job_manager = MagicMock()
        job_manager.find_active_job_by_session = AsyncMock(return_value=None)
        job_manager.start_job = AsyncMock()
        h = StartAutoHandler(
            event_store=event_store,
            job_manager=job_manager,
            store=store,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )
        h._inner_auto = fake_inner_auto

        result = await h.handle({"resume": state.auto_session_id})

        assert result.is_err
        assert "active plugin dispatch" in result.error.message
        assert result.error.details["auto_session_id"] == state.auto_session_id
        job_manager.start_job.assert_not_called()
        fake_inner_auto.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_expired_plugin_lease_allows_redispatch(
        self, event_store, tmp_path, fake_inner_auto
    ) -> None:
        store = AutoStore(tmp_path)
        state = AutoPipelineState(goal="build a CLI", cwd=str(tmp_path))
        store.save(state)
        store.path_for(state.auto_session_id).with_suffix(".start_auto_lease.json").write_text(
            json.dumps(
                {
                    "token": "lease_stale",
                    "mode": "plugin_dispatched",
                    "created_at": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
                    "expires_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        job_manager = MagicMock()
        job_manager.find_active_job_by_session = AsyncMock(return_value=None)
        job_manager.start_job = AsyncMock()
        h = StartAutoHandler(
            event_store=event_store,
            job_manager=job_manager,
            store=store,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )
        h._inner_auto = fake_inner_auto

        result = await h.handle({"resume": state.auto_session_id})

        assert result.is_ok
        assert result.value.meta["status"] == "delegated_to_plugin"
        job_manager.start_job.assert_not_called()
        fake_inner_auto.handle.assert_not_called()
