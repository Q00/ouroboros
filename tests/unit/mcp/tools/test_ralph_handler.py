"""Tests for the first-class Ralph MCP loop."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from ouroboros.core.conductor import ConductorDirective
from ouroboros.core.types import Result
from ouroboros.mcp.job_manager import JobManager, JobStatus
from ouroboros.mcp.tools.ralph_handlers import RalphHandler, StartRalphHandler
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
async def test_ralph_loop_forwards_conductor_directive_only_to_first_generation() -> None:
    evolve = _FakeEvolveHandler(["continue", "converged"])
    runner = RalphLoopRunner(evolve)
    directive = ConductorDirective(
        source_attention_event_id="attention_1",
        instruction="Correct the rejected evidence without weakening the Seed.",
        deterministic=True,
    )

    result = await runner.run(
        RalphLoopConfig(
            lineage_id="lin_conductor",
            max_generations=2,
            conductor_directive=directive,
            conductor_decision_id="decision_1",
            predecessor_execution_id="exec_predecessor",
        )
    )

    assert result.status == "completed"
    assert evolve.calls[0]["conductor_directive"] == directive.to_event_data()
    assert evolve.calls[0]["conductor_decision_id"] == "decision_1"
    assert evolve.calls[0]["predecessor_execution_id"] == "exec_predecessor"
    assert "conductor_directive" not in evolve.calls[1]
    assert "conductor_decision_id" not in evolve.calls[1]


@pytest.mark.asyncio
async def test_ralph_loop_forwards_checkpoint_commit_state() -> None:
    evolve = _FakeEvolveHandler(["continue", "converged"])

    async def handle(arguments: dict[str, Any]):
        evolve.calls.append(dict(arguments))
        attempts = list(arguments.get("checkpoint_attempted_ac_ids", []))
        if len(evolve.calls) == 1:
            attempts.append("AC-2")
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="ok"),),
                is_error=False,
                meta={
                    "lineage_id": arguments["lineage_id"],
                    "generation": len(evolve.calls),
                    "action": "converged" if len(evolve.calls) == 2 else "continue",
                    "checkpoint_commits": arguments.get("checkpoint_commits", []),
                    "checkpoint_attempted_ac_ids": attempts,
                },
            )
        )

    evolve.handle = handle  # type: ignore[method-assign]
    runner = RalphLoopRunner(evolve)

    result = await runner.run(
        RalphLoopConfig(
            lineage_id="lin_checkpoint",
            seed_content="goal: test",
            max_generations=3,
            commit_policy="ac_checkpoint",
            auto_session_id="auto_123",
            execution_id="exec_123",
            checkpoint_commits=({"ac_id": "AC-1", "commit": "abc123"},),
            checkpoint_attempted_ac_ids=("AC-1",),
        )
    )

    assert result.status == "completed"
    assert evolve.calls[0]["execution_id"] == "exec_123"
    assert evolve.calls[0]["checkpoint_attempted_ac_ids"] == ["AC-1"]
    assert evolve.calls[1]["checkpoint_attempted_ac_ids"] == ["AC-1", "AC-2"]
    assert evolve.calls[1]["checkpoint_commits"] == [{"ac_id": "AC-1", "commit": "abc123"}]


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
        # 60s rather than 30s: GitHub Actions runners under load have been
        # observed to take >30s to drain a 2-iteration FakeEvolveHandler.
        # The job itself is cheap; the slack absorbs runner cold-start +
        # neighbor-job contention without masking real regressions.
        deadline = asyncio.get_running_loop().time() + 60.0
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
async def test_ralph_handler_plugin_mode_forwards_checkpoint_contract() -> None:
    """#1281 review blocker: plugin-mode Ralph must carry the AC checkpoint
    contract the in-process RalphLoopRunner receives, otherwise coding
    complete-product sessions silently run Ralph without checkpoint policy/state.
    """
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
                "lineage_id": "lin_ckpt",
                "seed_content": "goal: plugin",
                "max_generations": 3,
                "commit_policy": "ac_checkpoint",
                "auto_session_id": "auto_ckpt123",
                "execution_id": "exec_ckpt123",
                "checkpoint_commits": [{"ac_id": "AC-1", "commit": "abc1234"}],
                "checkpoint_attempted_ac_ids": ["AC-1"],
            }
        )

        assert result.is_ok
        meta = result.value.meta
        assert meta["status"] == "delegated_to_plugin"
        ctx = meta["_subagent"]["context"]
        assert ctx["commit_policy"] == "ac_checkpoint"
        assert ctx["auto_session_id"] == "auto_ckpt123"
        assert ctx["execution_id"] == "exec_ckpt123"
        assert ctx["checkpoint_commits"] == [{"ac_id": "AC-1", "commit": "abc1234"}]
        assert ctx["checkpoint_attempted_ac_ids"] == ["AC-1"]
        # The prompt advertises the checkpoint obligation to the child session.
        assert "Checkpoint Commits" in meta["_subagent"]["prompt"]
        assert evolve.calls == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_ralph_handler_plugin_mode_omits_checkpoint_when_unset() -> None:
    """Legacy callers that never set commit_policy keep the prior context shape."""
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
            {"lineage_id": "lin_plain", "seed_content": "goal: plugin", "max_generations": 3}
        )

        assert result.is_ok
        ctx = result.value.meta["_subagent"]["context"]
        assert "commit_policy" not in ctx
        assert "checkpoint_commits" not in ctx
        assert "Checkpoint Commits" not in result.value.meta["_subagent"]["prompt"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_ralph_handler_accepts_plugin_null_checkpoint_arrays() -> None:
    """#1501 follow-up (sibling of #1502 evolve_step fix): plugin MCP clients
    may serialize optional array parameters as explicit JSON ``null`` rather
    than omitting them. Building ``RalphLoopConfig`` must not crash with
    ``'NoneType' object is not iterable`` when ``checkpoint_commits`` /
    ``checkpoint_attempted_ac_ids`` arrive as ``None``.
    """
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
                "lineage_id": "lin_null_ckpt",
                "seed_content": "goal: plugin",
                "max_generations": 3,
                "checkpoint_commits": None,
                "checkpoint_attempted_ac_ids": None,
            }
        )

        assert result.is_ok
        assert result.value.meta["status"] == "delegated_to_plugin"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_start_ralph_handler_plugin_mode_delegates_without_local_job() -> None:
    store = EventStore("sqlite+aiosqlite:///:memory:")
    job_manager = JobManager(store)
    evolve = _FakeEvolveHandler(["converged"])
    handler = StartRalphHandler(
        evolve_handler=evolve,  # type: ignore[arg-type]
        event_store=store,
        job_manager=job_manager,
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )

    try:
        result = await handler.handle(
            {
                "lineage_id": "lin_start_plugin",
                "seed_content": "goal: start plugin",
                "max_generations": 3,
            }
        )

        assert result.is_ok
        meta = result.value.meta
        assert meta["job_id"] is None
        assert meta["status"] == "delegated_to_plugin"
        assert meta["dispatch_mode"] == "plugin"
        assert meta["lineage_id"] == "lin_start_plugin"
        assert meta["max_generations"] == 3
        assert meta["_subagent"]["tool_name"] == "ouroboros_ralph"
        assert meta["_subagent"]["context"]["seed_content"] == "goal: start plugin"
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


def test_start_ralph_handler_definition_is_fire_and_forget_alias() -> None:
    handler = StartRalphHandler(evolve_handler=_FakeEvolveHandler(["converged"]))  # type: ignore[arg-type]

    assert handler.definition.name == "ouroboros_start_ralph"
    assert tuple(param.name for param in handler.definition.parameters) == tuple(
        param.name for param in RalphHandler().definition.parameters
    )
    assert "job_id" in handler.definition.description
    assert "OpenCode plugin mode" in handler.definition.description
    assert "job_id=None" in handler.definition.description
    assert "delegated_to_plugin" in handler.definition.description
    assert "not pollable" in handler.definition.description
