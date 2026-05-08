"""Regression tests for the per-iteration wall-clock timeout in Ralph loop.

Covers issue #776: a single hung ``evolve_step`` invocation must not consume
the entire wall clock. The runner enforces a per-iteration timeout via
``asyncio.wait_for``; the MCP layer validates the user-supplied bound at
``30 <= x <= 7200`` seconds.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.tools.ralph_handlers import RalphHandler
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.ralph_loop import RalphLoopConfig, RalphLoopRunner


@dataclass
class _SleepingEvolveHandler:
    """Evolve handler whose ``handle`` blocks past the per-iteration timeout."""

    sleep_seconds: float
    calls: int = 0
    started: asyncio.Event = field(default_factory=asyncio.Event)

    async def handle(self, arguments: dict[str, Any]):
        self.calls += 1
        self.started.set()
        await asyncio.sleep(self.sleep_seconds)
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


@dataclass
class _ImmediateEvolveHandler:
    """Trivial evolve handler so RalphHandler validation tests can be wired."""

    async def handle(self, arguments: dict[str, Any]):  # pragma: no cover - unused path
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="ok"),),
                is_error=False,
                meta={
                    "lineage_id": arguments["lineage_id"],
                    "generation": 1,
                    "action": "converged",
                },
            )
        )


@pytest.mark.asyncio
async def test_ralph_loop_stops_when_iteration_exceeds_timeout() -> None:
    """A handler that hangs longer than the timeout must abort the loop."""
    evolve = _SleepingEvolveHandler(sleep_seconds=0.5)
    runner = RalphLoopRunner(evolve)

    result = await runner.run(
        RalphLoopConfig(
            lineage_id="lin_timeout",
            seed_content="goal: timeout",
            max_generations=5,
            per_iteration_timeout_seconds=0.05,
        )
    )

    assert result.status == "failed"
    assert result.stop_reason == "iteration_timeout"
    assert result.iteration_count == 1
    assert result.iterations[0].action == "iteration_timeout"
    assert result.iterations[0].is_error is True
    # The loop must abort after the first iteration; no second handler call.
    assert evolve.calls == 1


@pytest.mark.asyncio
async def test_ralph_loop_timeout_surfaces_in_tool_result_meta() -> None:
    """``iterations`` end-to-end metadata exposes the timeout cause to clients."""
    evolve = _SleepingEvolveHandler(sleep_seconds=0.2)
    runner = RalphLoopRunner(evolve)

    result = await runner.run(
        RalphLoopConfig(
            lineage_id="lin_timeout_meta",
            max_generations=3,
            per_iteration_timeout_seconds=0.05,
        )
    )

    tool_result = result.to_tool_result()
    assert tool_result.is_error is True
    assert tool_result.meta["status"] == "failed"
    assert tool_result.meta["stop_reason"] == "iteration_timeout"
    assert tool_result.meta["actions"] == ["iteration_timeout"]


@pytest.mark.asyncio
async def test_ralph_handler_rejects_per_iteration_timeout_below_floor() -> None:
    handler = RalphHandler(evolve_handler=_ImmediateEvolveHandler())  # type: ignore[arg-type]

    result = await handler.handle(
        {
            "lineage_id": "lin_floor",
            "per_iteration_timeout_seconds": 10,
        }
    )

    assert result.is_err
    assert "per_iteration_timeout_seconds" in str(result.error)
    assert "between 30 and 7200" in str(result.error)


@pytest.mark.asyncio
async def test_ralph_handler_rejects_per_iteration_timeout_above_ceiling() -> None:
    handler = RalphHandler(evolve_handler=_ImmediateEvolveHandler())  # type: ignore[arg-type]

    result = await handler.handle(
        {
            "lineage_id": "lin_ceiling",
            "per_iteration_timeout_seconds": 10000,
        }
    )

    assert result.is_err
    assert "per_iteration_timeout_seconds" in str(result.error)
    assert "between 30 and 7200" in str(result.error)


@pytest.mark.asyncio
async def test_ralph_handler_rejects_non_numeric_per_iteration_timeout() -> None:
    handler = RalphHandler(evolve_handler=_ImmediateEvolveHandler())  # type: ignore[arg-type]

    result = await handler.handle(
        {
            "lineage_id": "lin_bad",
            "per_iteration_timeout_seconds": "not-a-number",
        }
    )

    assert result.is_err
    assert "per_iteration_timeout_seconds must be a number" in str(result.error)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
async def test_ralph_handler_rejects_non_finite_per_iteration_timeout(
    bad_value: float,
) -> None:
    """Non-finite values bypass plain range comparisons (`NaN < 30` is False),
    so the handler must reject them explicitly before they can leak into
    ``asyncio.wait_for`` or the plugin-path subagent context.
    """
    handler = RalphHandler(evolve_handler=_ImmediateEvolveHandler())  # type: ignore[arg-type]

    result = await handler.handle(
        {
            "lineage_id": "lin_nonfinite",
            "per_iteration_timeout_seconds": bad_value,
        }
    )

    assert result.is_err
    assert "per_iteration_timeout_seconds must be a finite number" in str(result.error)


@pytest.mark.asyncio
async def test_plugin_dispatch_forwards_per_iteration_timeout_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plugin-mode dispatch must forward per_iteration_timeout_seconds.

    Wiring lock for #784 review-1: when ``should_dispatch_via_plugin`` returns
    True, the produced ``_subagent`` payload context must include
    ``per_iteration_timeout_seconds``. Otherwise the public stop-reason
    contract (``iteration_timeout``) is silently dropped on the plugin path
    and the child session can hang indefinitely.
    """
    import json as _json

    from ouroboros.mcp.tools import ralph_handlers as _ralph_handlers

    handler = RalphHandler(
        evolve_handler=_ImmediateEvolveHandler(),  # type: ignore[arg-type]
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )

    # Make the audit emitter a no-op (handler initializes its own EventStore).
    async def _noop_emit(event_store, *, session_id, payload):  # noqa: ANN001
        return None

    monkeypatch.setattr(
        _ralph_handlers,
        "emit_subagent_dispatched_event",
        _noop_emit,
    )

    result = await handler.handle(
        {
            "lineage_id": "lin_plugin_timeout",
            "seed_content": "goal: ship",
            "max_generations": 3,
            "per_iteration_timeout_seconds": 1234,
        }
    )

    assert result.is_ok
    tool_result = result.value
    body = _json.loads(tool_result.content[0].text)
    sub = body["_subagent"]
    assert sub["tool_name"] == "ouroboros_ralph"
    assert sub["context"]["per_iteration_timeout_seconds"] == 1234
    assert "per_iteration_timeout_seconds: 1234" in sub["prompt"]
    assert "stop_reason=iteration_timeout" in sub["prompt"]
