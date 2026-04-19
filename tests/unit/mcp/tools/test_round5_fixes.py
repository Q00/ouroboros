"""Tests for PR #442 round-5 reviewer fixes.

Issue #1: start_* plugin-mode registers real JobManager record (real job_id).
Issue #2: get_ouroboros_tools wires all plugin-capable handlers.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ouroboros.mcp.job_manager import JobManager, JobStatus
from ouroboros.persistence.event_store import EventStore

# ---------------------------------------------------------------------------
# Issue #1: StartExecuteSeedHandler plugin-mode returns real job_id
# ---------------------------------------------------------------------------


class TestStartExecuteSeedPluginJobId:
    """start_execute_seed in plugin mode registers a real job."""

    @pytest.fixture
    async def event_store(self):
        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()
        yield store
        await store.close()

    @pytest.fixture
    def handler(self, event_store):
        from ouroboros.mcp.tools.execution_handlers import StartExecuteSeedHandler

        jm = JobManager(event_store)
        return StartExecuteSeedHandler(
            execute_handler=MagicMock(),
            event_store=event_store,
            job_manager=jm,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )

    async def test_returns_real_job_id(self, handler) -> None:
        result = await handler.handle({"seed_content": "goal: test"})
        assert result.is_ok
        meta = result.value.meta
        assert meta["job_id"] is not None
        assert meta["job_id"].startswith("job_")

    async def test_job_id_is_queryable(self, handler) -> None:
        result = await handler.handle({"seed_content": "goal: test"})
        job_id = result.value.meta["job_id"]
        snapshot = await handler._job_manager.get_snapshot(job_id)
        assert snapshot.job_id == job_id
        assert snapshot.job_type == "execute_seed"

    async def test_job_completes_with_dispatch_meta(self, handler) -> None:
        result = await handler.handle({"seed_content": "goal: test"})
        job_id = result.value.meta["job_id"]
        # Allow background task to settle
        import asyncio

        await asyncio.sleep(0.1)
        snapshot = await handler._job_manager.get_snapshot(job_id)
        assert snapshot.status in {JobStatus.COMPLETED, JobStatus.RUNNING}

    async def test_subagent_payload_still_present(self, handler) -> None:
        result = await handler.handle({"seed_content": "goal: test"})
        assert "_subagent" in result.value.meta
        assert result.value.meta["_subagent"]["tool_name"] == "ouroboros_execute_seed"

    async def test_dispatch_mode_is_plugin(self, handler) -> None:
        result = await handler.handle({"seed_content": "goal: test"})
        assert result.value.meta["dispatch_mode"] == "plugin"
        assert result.value.meta["status"] == "delegated_to_subagent"


# ---------------------------------------------------------------------------
# Issue #1: StartEvolveStepHandler plugin-mode returns real job_id
# ---------------------------------------------------------------------------


class TestStartEvolveStepPluginJobId:
    """start_evolve_step in plugin mode registers a real job."""

    @pytest.fixture
    async def event_store(self):
        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()
        yield store
        await store.close()

    @pytest.fixture
    def handler(self, event_store):
        from ouroboros.mcp.tools.evolution_handlers import StartEvolveStepHandler

        jm = JobManager(event_store)
        return StartEvolveStepHandler(
            evolve_handler=MagicMock(),
            event_store=event_store,
            job_manager=jm,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )

    async def test_returns_real_job_id(self, handler) -> None:
        result = await handler.handle({"lineage_id": "lin-abc"})
        assert result.is_ok
        meta = result.value.meta
        assert meta["job_id"] is not None
        assert meta["job_id"].startswith("job_")

    async def test_job_id_is_queryable(self, handler) -> None:
        result = await handler.handle({"lineage_id": "lin-abc"})
        job_id = result.value.meta["job_id"]
        snapshot = await handler._job_manager.get_snapshot(job_id)
        assert snapshot.job_id == job_id
        assert snapshot.job_type == "evolve_step"

    async def test_subagent_payload_still_present(self, handler) -> None:
        result = await handler.handle({"lineage_id": "lin-abc"})
        assert "_subagent" in result.value.meta
        payload = result.value.meta["_subagent"]
        assert payload["tool_name"] == "ouroboros_evolve_step"

    async def test_lineage_id_in_response(self, handler) -> None:
        result = await handler.handle({"lineage_id": "lin-abc"})
        assert result.value.meta["lineage_id"] == "lin-abc"


# ---------------------------------------------------------------------------
# Issue #2: get_ouroboros_tools wires plugin-capable handlers
# ---------------------------------------------------------------------------


class TestGetOuroborosToolsPluginWiring:
    """get_ouroboros_tools threads runtime/mode to all plugin handlers."""

    def test_lateral_think_handler_wired(self) -> None:
        from ouroboros.mcp.tools.definitions import get_ouroboros_tools
        from ouroboros.mcp.tools.evaluation_handlers import LateralThinkHandler

        tools = get_ouroboros_tools(runtime_backend="opencode", opencode_mode="plugin")
        h = next(t for t in tools if isinstance(t, LateralThinkHandler))
        assert h.agent_runtime_backend == "opencode"
        assert h.opencode_mode == "plugin"

    def test_evolve_step_handler_wired(self) -> None:
        from ouroboros.mcp.tools.definitions import get_ouroboros_tools
        from ouroboros.mcp.tools.evolution_handlers import EvolveStepHandler

        tools = get_ouroboros_tools(runtime_backend="opencode", opencode_mode="plugin")
        h = next(t for t in tools if isinstance(t, EvolveStepHandler))
        assert h.agent_runtime_backend == "opencode"
        assert h.opencode_mode == "plugin"

    def test_start_evolve_step_handler_wired(self) -> None:
        from ouroboros.mcp.tools.definitions import get_ouroboros_tools
        from ouroboros.mcp.tools.evolution_handlers import StartEvolveStepHandler

        tools = get_ouroboros_tools(runtime_backend="opencode", opencode_mode="plugin")
        h = next(t for t in tools if isinstance(t, StartEvolveStepHandler))
        assert h.agent_runtime_backend == "opencode"
        assert h.opencode_mode == "plugin"

    def test_start_evolve_inner_handler_also_wired(self) -> None:
        from ouroboros.mcp.tools.definitions import get_ouroboros_tools
        from ouroboros.mcp.tools.evolution_handlers import StartEvolveStepHandler

        tools = get_ouroboros_tools(runtime_backend="opencode", opencode_mode="plugin")
        h = next(t for t in tools if isinstance(t, StartEvolveStepHandler))
        inner = h._evolve_handler
        assert inner.agent_runtime_backend == "opencode"
        assert inner.opencode_mode == "plugin"

    def test_factory_fns_accept_kwargs(self) -> None:
        from ouroboros.mcp.tools.definitions import (
            evolve_step_handler,
            lateral_think_handler,
            start_evolve_step_handler,
        )

        lt = lateral_think_handler(runtime_backend="opencode", opencode_mode="plugin")
        assert lt.agent_runtime_backend == "opencode"
        assert lt.opencode_mode == "plugin"

        ev = evolve_step_handler(runtime_backend="opencode", opencode_mode="plugin")
        assert ev.agent_runtime_backend == "opencode"
        assert ev.opencode_mode == "plugin"

        sev = start_evolve_step_handler(runtime_backend="opencode", opencode_mode="plugin")
        assert sev.agent_runtime_backend == "opencode"
        assert sev.opencode_mode == "plugin"

    def test_total_tool_count_unchanged(self) -> None:
        from ouroboros.mcp.tools.definitions import get_ouroboros_tools

        tools = get_ouroboros_tools()
        assert len(tools) == 24
