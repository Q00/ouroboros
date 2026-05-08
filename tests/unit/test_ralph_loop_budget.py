"""Regression tests for the total wall-clock budget in Ralph loop.

Covers issue #777: a Ralph loop with `max_total_seconds` set must skip launching
new iterations once the cumulative wall-clock budget is exhausted, surfacing
``stop_reason="wall_clock_exhausted"`` to clients. The MCP layer validates the
user-supplied bound at ``1 <= x <= 86400`` seconds, and standalone callers that
omit ``max_total_seconds`` get a derived ceiling
(``max_generations * per_iteration_timeout_seconds``) auto-applied with a
WARNING log.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import Any

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.tools.ralph_handlers import RalphHandler
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.ralph_loop import RalphLoopConfig, RalphLoopRunner


@dataclass
class _SleepingEvolveHandler:
    """Evolve handler whose ``handle`` blocks for a configurable duration."""

    sleep_seconds: float
    calls: int = 0
    started: asyncio.Event = field(default_factory=asyncio.Event)

    async def handle(self, arguments: dict[str, Any]):
        self.calls += 1
        self.started.set()
        await asyncio.sleep(self.sleep_seconds)
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="ok"),),
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
async def test_ralph_loop_skips_iteration_when_wall_clock_exhausted() -> None:
    """First iteration runs; second is skipped pre-launch on budget exhaustion."""
    # Iteration sleeps 0.07s, budget is 0.1s. After iter 1 ~0.07s elapsed; iter 2
    # check happens; iter 2 still runs (0.07 < 0.1). After iter 2 ~0.14s elapsed,
    # iter 3 check trips the budget. Use a 2-iteration variant that guarantees
    # second iteration is skipped: sleep 0.07s, budget 0.05s — but that means
    # iter 1 itself overruns the budget at completion. The check is at the TOP
    # of each iteration, so iter 1 launches (elapsed ~0s < 0.05s) and runs to
    # 0.07s; iter 2's pre-check sees 0.07s >= 0.05s and skips. Exactly the
    # contract the AC asks for.
    evolve = _SleepingEvolveHandler(sleep_seconds=0.07)
    runner = RalphLoopRunner(evolve)

    result = await runner.run(
        RalphLoopConfig(
            lineage_id="lin_budget",
            seed_content="goal: budget",
            max_generations=5,
            per_iteration_timeout_seconds=1.0,
            max_total_seconds=0.05,
        )
    )

    assert result.status == "failed"
    assert result.stop_reason == "wall_clock_exhausted"
    # Exactly one iteration ran; iteration 2 was skipped pre-launch.
    assert evolve.calls == 1
    assert result.iteration_count == 1
    assert result.iterations[0].action == "continue"


@pytest.mark.asyncio
async def test_ralph_loop_wall_clock_exhausted_surfaces_in_tool_result_meta() -> None:
    """``iterations`` end-to-end metadata exposes wall-clock exhaustion."""
    evolve = _SleepingEvolveHandler(sleep_seconds=0.07)
    runner = RalphLoopRunner(evolve)

    result = await runner.run(
        RalphLoopConfig(
            lineage_id="lin_budget_meta",
            max_generations=3,
            per_iteration_timeout_seconds=1.0,
            max_total_seconds=0.05,
        )
    )

    tool_result = result.to_tool_result()
    assert tool_result.is_error is True
    assert tool_result.meta["status"] == "failed"
    assert tool_result.meta["stop_reason"] == "wall_clock_exhausted"


@pytest.mark.asyncio
async def test_ralph_loop_max_total_seconds_none_does_not_constrain() -> None:
    """When max_total_seconds is None at the runner level, no extra cap applies."""
    evolve = _SleepingEvolveHandler(sleep_seconds=0.0)
    runner = RalphLoopRunner(evolve)

    result = await runner.run(
        RalphLoopConfig(
            lineage_id="lin_none",
            max_generations=2,
            per_iteration_timeout_seconds=1.0,
            max_total_seconds=None,
        )
    )

    # Both iterations should complete with action="continue", reaching the
    # max_generations terminal.
    assert result.stop_reason == "max_generations reached"
    assert evolve.calls == 2


@pytest.mark.asyncio
async def test_ralph_handler_auto_applies_derived_ceiling_when_omitted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Standalone callers that omit max_total_seconds get a derived ceiling + WARNING."""
    handler = RalphHandler(evolve_handler=_ImmediateEvolveHandler())  # type: ignore[arg-type]

    captured: dict[str, Any] = {}

    class _FakeResult:
        def to_tool_result(self) -> MCPToolResult:
            return MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="ok"),),
                is_error=False,
                meta={"action": "converged"},
            )

    class _CapturingRunner:
        def __init__(self, evolve_handler: Any) -> None:
            self.evolve_handler = evolve_handler

        async def run(self, config: RalphLoopConfig) -> Any:
            captured["config"] = config
            return _FakeResult()

    from ouroboros.mcp.tools import ralph_handlers as _ralph_handlers

    original_runner = _ralph_handlers.RalphLoopRunner
    _ralph_handlers.RalphLoopRunner = _CapturingRunner  # type: ignore[assignment]
    try:
        with caplog.at_level(logging.WARNING, logger="ouroboros.mcp.tools.ralph_handlers"):
            result = await handler.handle(
                {
                    "lineage_id": "lin_derived",
                    "max_generations": 4,
                    "per_iteration_timeout_seconds": 60,
                    # max_total_seconds intentionally omitted
                }
            )
    finally:
        _ralph_handlers.RalphLoopRunner = original_runner  # type: ignore[assignment]

    assert result.is_ok
    # Wait for the captured runner config — start_job runs the coroutine which
    # awaits runner.run. JobManager kicks the runner asynchronously, so wait
    # briefly for it to land.
    for _ in range(200):
        if "config" in captured:
            break
        await asyncio.sleep(0.01)
    assert "config" in captured, "Runner.run was never invoked"
    config = captured["config"]
    # 4 generations * 60s/iter = 240s derived ceiling.
    assert config.max_total_seconds == pytest.approx(240.0)

    warnings = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.WARNING and "max_total_seconds not provided" in rec.getMessage()
    ]
    assert warnings, f"Expected WARNING about derived ceiling, got: {caplog.records}"
    assert "240" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_ralph_handler_rejects_max_total_seconds_below_floor() -> None:
    handler = RalphHandler(evolve_handler=_ImmediateEvolveHandler())  # type: ignore[arg-type]

    result = await handler.handle(
        {
            "lineage_id": "lin_budget_floor",
            "max_total_seconds": 0,
        }
    )

    assert result.is_err
    assert "max_total_seconds" in str(result.error)
    assert "between 1 and 86400" in str(result.error)


@pytest.mark.asyncio
async def test_ralph_handler_rejects_max_total_seconds_above_ceiling() -> None:
    handler = RalphHandler(evolve_handler=_ImmediateEvolveHandler())  # type: ignore[arg-type]

    result = await handler.handle(
        {
            "lineage_id": "lin_budget_ceil",
            "max_total_seconds": 100000,
        }
    )

    assert result.is_err
    assert "max_total_seconds" in str(result.error)
    assert "between 1 and 86400" in str(result.error)


@pytest.mark.asyncio
async def test_ralph_handler_rejects_non_numeric_max_total_seconds() -> None:
    handler = RalphHandler(evolve_handler=_ImmediateEvolveHandler())  # type: ignore[arg-type]

    result = await handler.handle(
        {
            "lineage_id": "lin_budget_bad",
            "max_total_seconds": "not-a-number",
        }
    )

    assert result.is_err
    assert "max_total_seconds must be a number" in str(result.error)
