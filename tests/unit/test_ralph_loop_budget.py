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


@dataclass
class _SuccessThenSlowEvolveHandler:
    """First call returns immediately; later calls sleep, exhausting the budget."""

    slow_sleep_seconds: float
    calls: int = 0

    async def handle(self, arguments: dict[str, Any]):
        self.calls += 1
        if self.calls > 1:
            await asyncio.sleep(self.slow_sleep_seconds)
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=f"iter{self.calls}-output"),),
                is_error=False,
                meta={
                    "lineage_id": arguments["lineage_id"],
                    "generation": self.calls,
                    "action": "continue",
                },
            )
        )


@pytest.mark.asyncio
async def test_wall_clock_exhausted_after_successful_iteration_replaces_terminal_result() -> None:
    """Budget exhaustion after iteration 1 must NOT leak iter 1's output.

    Regression for #789 review-3: when iteration N+1 was skipped pre-launch
    on budget exhaustion, the runner used to keep ``final_result`` pointing
    at the prior successful iteration's MCPToolResult, so the terminal
    response reported ``stop_reason=wall_clock_exhausted`` while its
    content/meta still carried ``action=continue`` and the prior iteration
    number. The fix always replaces ``final_result`` with the synthetic
    exhaustion result.
    """
    # Iter 1 finishes immediately (~0s), iter 2 sleeps 0.12s. Budget = 0.05s.
    # Top-of-iter-2 check sees ~0s elapsed and lets iter 2 run; iter 2
    # completes at ~0.12s. Top-of-iter-3 check sees 0.12s >= 0.05s and skips,
    # firing the budget-exhausted branch with a prior successful final_result
    # in flight.
    evolve = _SuccessThenSlowEvolveHandler(slow_sleep_seconds=0.12)
    runner = RalphLoopRunner(evolve)

    result = await runner.run(
        RalphLoopConfig(
            lineage_id="lin_budget_after_success",
            seed_content="goal: budget",
            max_generations=5,
            per_iteration_timeout_seconds=1.0,
            max_total_seconds=0.05,
        )
    )

    assert result.status == "failed"
    assert result.stop_reason == "wall_clock_exhausted"
    assert evolve.calls == 2  # iter 1 + iter 2 ran; iter 3 skipped pre-launch.
    assert result.iteration_count == 2  # only the two successful iterations.
    assert result.iterations[0].action == "continue"
    assert result.iterations[1].action == "continue"

    # Terminal MCPToolResult must reflect budget exhaustion, not iter 2's output.
    assert result.final_result.meta["action"] == "wall_clock_exhausted"
    assert result.final_result.meta["generation"] is None
    assert "wall-clock budget exhausted" in result.final_result.text_content
    assert "iter1-output" not in result.final_result.text_content
    assert "iter2-output" not in result.final_result.text_content

    tool_result = result.to_tool_result()
    assert tool_result.is_error is True
    assert tool_result.meta["status"] == "failed"
    assert tool_result.meta["stop_reason"] == "wall_clock_exhausted"
    assert "iter1-output" not in tool_result.text_content
    assert "iter2-output" not in tool_result.text_content



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


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
async def test_ralph_handler_rejects_non_finite_max_total_seconds(value: str) -> None:
    handler = RalphHandler(evolve_handler=_ImmediateEvolveHandler())  # type: ignore[arg-type]

    result = await handler.handle(
        {
            "lineage_id": "lin_budget_non_finite",
            "max_total_seconds": value,
        }
    )

    assert result.is_err
    assert "max_total_seconds" in str(result.error)
    assert "between 1 and 86400" in str(result.error)


@pytest.mark.asyncio
async def test_plugin_dispatch_forwards_max_total_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plugin-mode dispatch must forward ``max_total_seconds``.

    Wiring lock for #789 review-1: when ``should_dispatch_via_plugin`` returns
    True, the produced ``_subagent`` payload context must include
    ``max_total_seconds`` and the prompt must surface the
    ``stop_reason=wall_clock_exhausted`` contract. Otherwise the public
    wall-clock budget contract is silently dropped on the plugin path and the
    child session can run past an explicit total budget.
    """
    import json as _json

    from ouroboros.mcp.tools import ralph_handlers as _ralph_handlers

    handler = RalphHandler(
        evolve_handler=_ImmediateEvolveHandler(),  # type: ignore[arg-type]
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )

    async def _noop_emit(event_store, *, session_id, payload):  # noqa: ANN001
        return None

    monkeypatch.setattr(
        _ralph_handlers,
        "emit_subagent_dispatched_event",
        _noop_emit,
    )

    result = await handler.handle(
        {
            "lineage_id": "lin_plugin_budget",
            "seed_content": "goal: ship",
            "max_generations": 3,
            "per_iteration_timeout_seconds": 600,
            "max_total_seconds": 4321,
        }
    )

    assert result.is_ok
    tool_result = result.value
    body = _json.loads(tool_result.content[0].text)
    sub = body["_subagent"]
    assert sub["tool_name"] == "ouroboros_ralph"
    assert sub["context"]["max_total_seconds"] == 4321
    assert "max_total_seconds: 4321" in sub["prompt"]
    assert "stop_reason=wall_clock_exhausted" in sub["prompt"]


@pytest.mark.asyncio
async def test_plugin_dispatch_forwards_derived_max_total_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plugin-mode dispatch forwards the derived ceiling when budget is omitted.

    Standalone callers that omit ``max_total_seconds`` get a derived ceiling
    of ``max_generations × per_iteration_timeout_seconds`` auto-applied at the
    handler. The plugin path must forward that derived value too so the child
    session enforces the same implicit ceiling rather than running unbounded.
    """
    import json as _json

    from ouroboros.mcp.tools import ralph_handlers as _ralph_handlers

    handler = RalphHandler(
        evolve_handler=_ImmediateEvolveHandler(),  # type: ignore[arg-type]
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )

    async def _noop_emit(event_store, *, session_id, payload):  # noqa: ANN001
        return None

    monkeypatch.setattr(
        _ralph_handlers,
        "emit_subagent_dispatched_event",
        _noop_emit,
    )

    result = await handler.handle(
        {
            "lineage_id": "lin_plugin_derived",
            "seed_content": "goal: ship",
            "max_generations": 4,
            "per_iteration_timeout_seconds": 60,
            # max_total_seconds intentionally omitted
        }
    )

    assert result.is_ok
    tool_result = result.value
    body = _json.loads(tool_result.content[0].text)
    sub = body["_subagent"]
    assert sub["context"]["max_total_seconds"] == pytest.approx(240.0)
    assert "max_total_seconds: 240" in sub["prompt"]
    assert "stop_reason=wall_clock_exhausted" in sub["prompt"]
