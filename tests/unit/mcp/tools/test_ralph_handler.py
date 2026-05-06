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
    qa_verdicts: list[str | None] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def handle(self, arguments: dict[str, Any]):
        self.calls.append(dict(arguments))
        index = len(self.calls) - 1
        action = self.actions[min(index, len(self.actions) - 1)]
        generation = index + 1
        qa_verdict = (
            self.qa_verdicts[min(index, len(self.qa_verdicts) - 1)] if self.qa_verdicts else None
        )
        meta: dict[str, Any] = {
            "lineage_id": arguments["lineage_id"],
            "generation": generation,
            "action": action,
        }
        if qa_verdict is not None:
            meta["qa"] = {"verdict": qa_verdict}
        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=f"generation {generation} action {action}",
                    ),
                ),
                is_error=action == "failed",
                meta=meta,
            )
        )


@dataclass
class _BlockingEvolveHandler:
    """Fake evolve handler that blocks until the Ralph job is cancelled."""

    started: asyncio.Event = field(default_factory=asyncio.Event)
    calls: int = 0

    async def handle(self, arguments: dict[str, Any]):  # noqa: ARG002 - protocol fixture
        self.calls += 1
        self.started.set()
        await asyncio.sleep(60)
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="late"),),
                is_error=False,
                meta={
                    "lineage_id": arguments["lineage_id"],
                    "generation": self.calls,
                    "action": "continue",
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

    assert result.status == "completed"
    assert result.stop_reason == "max_generations reached"
    assert result.iteration_count == 2


@pytest.mark.asyncio
async def test_ralph_loop_stops_when_qa_passes() -> None:
    evolve = _FakeEvolveHandler(["continue", "continue"], qa_verdicts=["fail", "pass"])
    runner = RalphLoopRunner(evolve)

    result = await runner.run(
        RalphLoopConfig(
            lineage_id="lin_qa",
            seed_content="goal: qa",
            max_generations=5,
        )
    )

    assert result.status == "completed"
    assert result.stop_reason == "qa passed"
    assert result.iteration_count == 2
    assert result.iterations[1].qa_verdict == "pass"


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
        deadline = asyncio.get_running_loop().time() + 30.0
        while not snapshot.is_terminal and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
            snapshot = await job_manager.get_snapshot(job_id)
        assert snapshot.status is JobStatus.COMPLETED
        assert snapshot.result_meta["iterations"] == 2
        assert snapshot.result_meta["actions"] == ["continue", "converged"]
        assert len(evolve.calls) == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_ralph_handler_guides_plain_request_without_lineage_id() -> None:
    handler = RalphHandler(evolve_handler=_FakeEvolveHandler(["converged"]))  # type: ignore[arg-type]

    result = await handler.handle({"lineage_id": ""})

    assert result.is_ok
    tool_result = result.value
    assert tool_result.is_error is True
    assert tool_result.meta["status"] == "input_required"
    assert tool_result.meta["missing"] == ["lineage_id"]
    assert "ooo interview" in tool_result.text_content
    assert "ooo seed" in tool_result.text_content


@pytest.mark.asyncio
async def test_ralph_handler_guides_whitespace_only_lineage_id() -> None:
    evolve = _FakeEvolveHandler(["converged"])
    handler = RalphHandler(evolve_handler=evolve)  # type: ignore[arg-type]

    result = await handler.handle({"lineage_id": "   "})

    assert result.is_ok
    assert result.value.is_error is True
    assert result.value.meta["status"] == "input_required"
    assert evolve.calls == []


@pytest.mark.asyncio
async def test_ralph_job_can_be_cancelled_with_job_manager() -> None:
    """Ralph jobs should use the standard job cancellation/status contract."""
    store = EventStore("sqlite+aiosqlite:///:memory:")
    job_manager = JobManager(store)
    evolve = _BlockingEvolveHandler()
    handler = RalphHandler(
        evolve_handler=evolve,  # type: ignore[arg-type]
        event_store=store,
        job_manager=job_manager,
    )

    try:
        started = await handler.handle(
            {
                "lineage_id": "lin_cancel",
                "seed_content": "goal: cancel",
                "max_generations": 5,
            }
        )
        assert started.is_ok
        job_id = started.value.meta["job_id"]

        await asyncio.wait_for(evolve.started.wait(), timeout=1.0)
        cancel_snapshot = await job_manager.cancel_job(job_id)
        assert cancel_snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}

        for _ in range(500):
            snapshot = await job_manager.get_snapshot(job_id)
            if snapshot.is_terminal:
                break
            await asyncio.sleep(0.01)

        assert snapshot.status is JobStatus.CANCELLED
        assert snapshot.links.lineage_id == "lin_cancel"
        assert evolve.calls == 1
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
    assert "ouroboros_cancel_job" in handler.definition.description
    assert "ouroboros_job_cancel" not in handler.definition.description
