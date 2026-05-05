"""Tests for the first-class Ralph MCP loop."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.job_manager import JobManager, JobStatus
from ouroboros.mcp.tools.ralph_handlers import RalphHandler
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.persistence.event_store import EventStore
from ouroboros.ralph_loop import RalphLoopConfig, RalphLoopRunner


@dataclass
class _FakeEvolveHandler:
    actions: list[str]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def handle(self, arguments: dict[str, Any]):
        self.calls.append(dict(arguments))
        index = len(self.calls) - 1
        action = self.actions[min(index, len(self.actions) - 1)]
        generation = index + 1
        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=f"generation {generation} action {action}",
                    ),
                ),
                is_error=action == "failed",
                meta={
                    "lineage_id": arguments["lineage_id"],
                    "generation": generation,
                    "action": action,
                },
            )
        )


@pytest.mark.asyncio
async def test_ralph_loop_runs_multiple_generations_until_converged() -> None:
    evolve = _FakeEvolveHandler(["continue", "continue", "converged"])
    runner = RalphLoopRunner(evolve)

    result = await runner.run(
        RalphLoopConfig(
            lineage_id="lin_test",
            seed_content="goal: test",
            max_generations=5,
        )
    )

    assert result.status == "completed"
    assert result.stop_reason == "converged"
    assert result.iteration_count == 3
    assert [call.get("seed_content") for call in evolve.calls] == ["goal: test", None, None]


@pytest.mark.asyncio
async def test_ralph_loop_stops_at_max_generations() -> None:
    evolve = _FakeEvolveHandler(["continue"])
    runner = RalphLoopRunner(evolve)

    result = await runner.run(
        RalphLoopConfig(
            lineage_id="lin_test",
            seed_content="goal: test",
            max_generations=2,
        )
    )

    assert result.status == "failed"
    assert result.stop_reason == "max_generations reached"
    assert result.iteration_count == 2


@pytest.mark.asyncio
async def test_ralph_handler_returns_job_id_and_completes_loop() -> None:
    store = EventStore("sqlite+aiosqlite:///:memory:")
    job_manager = JobManager(store)
    evolve = _FakeEvolveHandler(["continue", "converged"])
    handler = RalphHandler(
        evolve_handler=evolve,  # type: ignore[arg-type]
        event_store=store,
        job_manager=job_manager,
    )

    try:
        started = await handler.handle(
            {
                "lineage_id": "lin_job",
                "seed_content": "goal: job",
                "max_generations": 5,
            }
        )
        assert started.is_ok
        job_id = started.value.meta["job_id"]
        assert job_id.startswith("job_")

        snapshot = await job_manager.get_snapshot(job_id)
        for _ in range(500):
            if snapshot.is_terminal:
                break
            await asyncio.sleep(0.01)
            snapshot = await job_manager.get_snapshot(job_id)
        assert snapshot.status is JobStatus.COMPLETED
        assert snapshot.result_meta["iterations"] == 2
        assert snapshot.result_meta["actions"] == ["continue", "converged"]
        assert len(evolve.calls) == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_ralph_handler_plugin_mode_delegates_without_local_job() -> None:
    store = EventStore("sqlite+aiosqlite:///:memory:")
    job_manager = JobManager(store)
    evolve = _FakeEvolveHandler(["converged"])
    handler = RalphHandler(
        evolve_handler=evolve,  # type: ignore[arg-type]
        event_store=store,
        job_manager=job_manager,
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )

    try:
        result = await handler.handle(
            {
                "lineage_id": "lin_plugin",
                "seed_content": "goal: plugin",
                "max_generations": 3,
            }
        )

        assert result.is_ok
        meta = result.value.meta
        assert meta["job_id"] is None
        assert meta["status"] == "delegated_to_plugin"
        assert meta["dispatch_mode"] == "plugin"
        assert meta["lineage_id"] == "lin_plugin"
        assert meta["max_generations"] == 3
        assert meta["_subagent"]["tool_name"] == "ouroboros_ralph"
        assert meta["_subagent"]["context"]["seed_content"] == "goal: plugin"
        assert meta["_subagent"]["context"]["delegation_depth"] == 1
        assert meta["_subagent"]["context"]["allow_nested_ouroboros_ralph"] is False
        assert evolve.calls == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_ralph_handler_rejects_excessive_max_generations() -> None:
    handler = RalphHandler(evolve_handler=_FakeEvolveHandler(["converged"]))  # type: ignore[arg-type]

    result = await handler.handle({"lineage_id": "lin_cap", "max_generations": 11})

    assert result.is_err
    assert "between 1 and 10" in str(result.error)


@pytest.mark.asyncio
async def test_ralph_handler_rejects_nested_delegation_marker() -> None:
    handler = RalphHandler(evolve_handler=_FakeEvolveHandler(["converged"]))  # type: ignore[arg-type]

    result = await handler.handle({"lineage_id": "lin_nested", "delegation_depth": 1})

    assert result.is_err
    assert "nested ouroboros_ralph delegation is not allowed" in str(result.error)


def test_ralph_handler_definition_is_public_tool() -> None:
    handler = RalphHandler(evolve_handler=_FakeEvolveHandler(["converged"]))  # type: ignore[arg-type]

    assert handler.definition.name == "ouroboros_ralph"
    assert {param.name for param in handler.definition.parameters} >= {
        "lineage_id",
        "seed_content",
        "max_generations",
    }
