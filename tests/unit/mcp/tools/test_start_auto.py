"""Tests for StartAutoHandler — fire-and-forget ``ooo auto`` wrapper.

Mirrors :mod:`test_start_evaluate`. The synchronous ``ouroboros_auto`` tool
routinely exceeds an MCP client's tool-call timeout because the Socratic
interview + repair loops + (optional) Ralph chain run end-to-end. The fire-
and-forget handler must return a ``job_id`` immediately and run the pipeline
under a :class:`JobManager`-backed background task.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.auto.state import AutoStore
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
