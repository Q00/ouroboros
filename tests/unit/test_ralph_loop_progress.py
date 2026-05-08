"""Regression tests for the no-progress / oscillation guards in Ralph loop.

Covers issue #778: stop early when ``evolve_step`` produces the same finding
set across iterations or when the QA grade strictly regresses, instead of
burning the entire ``max_generations`` wall-clock budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.tools.ralph_handlers import RalphHandler
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.ralph_loop import RalphLoopConfig, RalphLoopRunner


@dataclass
class _ScriptedEvolveHandler:
    """Evolve handler that returns a fixed sequence of meta payloads."""

    metas: list[dict[str, Any]]
    calls: int = 0
    seen_arguments: list[dict[str, Any]] = field(default_factory=list)

    async def handle(self, arguments: dict[str, Any]):
        self.seen_arguments.append(arguments)
        index = min(self.calls, len(self.metas) - 1)
        meta = dict(self.metas[index])
        meta.setdefault("lineage_id", arguments["lineage_id"])
        meta.setdefault("generation", self.calls + 1)
        meta.setdefault("action", "continue")
        self.calls += 1
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="step"),),
                is_error=False,
                meta=meta,
            )
        )


@dataclass
class _ImmediateEvolveHandler:
    """Trivial evolve handler used only by RalphHandler validation tests."""

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


def _findings(*labels: str) -> list[dict[str, Any]]:
    return [{"id": label, "msg": f"finding-{label}"} for label in labels]


@pytest.mark.asyncio
async def test_oscillation_detected_after_window_of_identical_findings_hashes() -> None:
    """3 iterations sharing one findings_hash with no QA pass must stop early."""
    repeated = _findings("a", "b")
    evolve = _ScriptedEvolveHandler(
        metas=[
            {"action": "continue", "findings": repeated},
            {"action": "continue", "findings": repeated},
            {"action": "continue", "findings": repeated},
            {"action": "continue", "findings": repeated},
        ]
    )
    runner = RalphLoopRunner(evolve)

    result = await runner.run(
        RalphLoopConfig(
            lineage_id="lin_osc",
            max_generations=5,
            oscillation_window=3,
        )
    )

    assert result.status == "failed"
    assert result.stop_reason == "oscillation_detected"
    assert result.iteration_count == 3
    # All three iterations carry the same hash.
    hashes = {item.findings_hash for item in result.iterations}
    assert len(hashes) == 1
    assert next(iter(hashes)) is not None
    # The handler must not have been invoked a fourth time.
    assert evolve.calls == 3


@pytest.mark.asyncio
async def test_grade_regressing_two_iterations_strictly_decreasing() -> None:
    """[0.8, 0.5] over the default window of 2 must stop with grade_regressing."""
    evolve = _ScriptedEvolveHandler(
        metas=[
            {"action": "continue", "qa": {"score": 0.8, "verdict": "fail"}},
            {"action": "continue", "qa": {"score": 0.5, "verdict": "fail"}},
            {"action": "continue", "qa": {"score": 0.4, "verdict": "fail"}},
        ]
    )
    runner = RalphLoopRunner(evolve)

    result = await runner.run(
        RalphLoopConfig(
            lineage_id="lin_regress",
            max_generations=5,
            grade_regression_window=2,
        )
    )

    assert result.status == "failed"
    assert result.stop_reason == "grade_regressing"
    assert result.iteration_count == 2
    assert [item.grade for item in result.iterations] == [0.8, 0.5]
    assert evolve.calls == 2


@pytest.mark.asyncio
async def test_grade_with_none_resets_regression_streak() -> None:
    """[0.8, None] must NOT trigger grade_regressing; None is a neutral observation."""
    evolve = _ScriptedEvolveHandler(
        metas=[
            {"action": "continue", "qa": {"score": 0.8, "verdict": "fail"}},
            # No qa block at all → grade is None.
            {"action": "continue"},
        ]
    )
    runner = RalphLoopRunner(evolve)

    result = await runner.run(
        RalphLoopConfig(
            lineage_id="lin_none_resets",
            max_generations=2,
            grade_regression_window=2,
        )
    )

    # Loop ran to max_generations rather than tripping a no-progress guard.
    assert result.status == "completed"
    assert result.stop_reason == "max_generations reached"
    assert result.iteration_count == 2
    assert [item.grade for item in result.iterations] == [0.8, None]


@pytest.mark.asyncio
async def test_equal_grades_do_not_trigger_grade_regressing() -> None:
    """[0.8, 0.8] is flat, not strictly decreasing — must not stop early."""
    evolve = _ScriptedEvolveHandler(
        metas=[
            {"action": "continue", "qa": {"score": 0.8, "verdict": "fail"}},
            {"action": "continue", "qa": {"score": 0.8, "verdict": "fail"}},
        ]
    )
    runner = RalphLoopRunner(evolve)

    result = await runner.run(
        RalphLoopConfig(
            lineage_id="lin_flat",
            max_generations=2,
            grade_regression_window=2,
        )
    )

    assert result.status == "completed"
    assert result.stop_reason == "max_generations reached"
    assert result.iteration_count == 2
    assert [item.grade for item in result.iterations] == [0.8, 0.8]


@pytest.mark.asyncio
async def test_mixed_hashes_do_not_trigger_oscillation_stop() -> None:
    """Same hash 2× then a new hash on iteration 3 must not stop with oscillation."""
    repeated = _findings("a")
    different = _findings("b")
    evolve = _ScriptedEvolveHandler(
        metas=[
            {"action": "continue", "findings": repeated},
            {"action": "continue", "findings": repeated},
            {"action": "continue", "findings": different},
        ]
    )
    runner = RalphLoopRunner(evolve)

    result = await runner.run(
        RalphLoopConfig(
            lineage_id="lin_mixed",
            max_generations=3,
            oscillation_window=3,
        )
    )

    assert result.status == "completed"
    assert result.stop_reason == "max_generations reached"
    assert result.iteration_count == 3
    hashes = [item.findings_hash for item in result.iterations]
    assert hashes[0] == hashes[1]
    assert hashes[2] != hashes[0]


@pytest.mark.asyncio
async def test_ralph_handler_rejects_oscillation_window_below_floor() -> None:
    """oscillation_window < 2 must be rejected; one iteration cannot oscillate."""
    handler = RalphHandler(evolve_handler=_ImmediateEvolveHandler())  # type: ignore[arg-type]

    for value in (0, 1):
        result = await handler.handle(
            {
                "lineage_id": "lin_osc_low",
                "oscillation_window": value,
            }
        )
        assert result.is_err, f"value={value} should be rejected"
        assert "oscillation_window" in str(result.error)
        assert "between 2 and 10" in str(result.error)


@pytest.mark.asyncio
async def test_ralph_handler_rejects_oscillation_window_above_ceiling() -> None:
    handler = RalphHandler(evolve_handler=_ImmediateEvolveHandler())  # type: ignore[arg-type]

    result = await handler.handle(
        {
            "lineage_id": "lin_osc_high",
            "oscillation_window": 11,
        }
    )

    assert result.is_err
    assert "oscillation_window" in str(result.error)
    assert "between 2 and 10" in str(result.error)


@pytest.mark.asyncio
async def test_ralph_handler_rejects_grade_regression_window_below_floor() -> None:
    """grade_regression_window < 2 must be rejected; strict-decrease needs two grades.

    Wiring lock for #788 review-2: previously the handler accepted
    ``grade_regression_window=1`` but the runner's ``_is_grade_regressing``
    returns ``False`` whenever ``window < 2``, so callers using ``1`` had a
    silently disabled stop condition. Public contract now matches runtime.
    """
    handler = RalphHandler(evolve_handler=_ImmediateEvolveHandler())  # type: ignore[arg-type]

    for value in (0, 1):
        result = await handler.handle(
            {
                "lineage_id": "lin_grade_low",
                "grade_regression_window": value,
            }
        )
        assert result.is_err, f"value={value} should be rejected"
        assert "grade_regression_window" in str(result.error)
        assert "between 2 and 10" in str(result.error)


@pytest.mark.asyncio
async def test_precomputed_findings_hash_is_used_verbatim() -> None:
    """If meta provides a findings_hash string, it must pass through unchanged."""
    evolve = _ScriptedEvolveHandler(
        metas=[
            {"action": "continue", "findings_hash": "deadbeef"},
            {"action": "continue", "findings_hash": "deadbeef"},
            {"action": "continue", "findings_hash": "deadbeef"},
        ]
    )
    runner = RalphLoopRunner(evolve)

    result = await runner.run(
        RalphLoopConfig(
            lineage_id="lin_precomputed",
            max_generations=5,
            oscillation_window=3,
        )
    )

    assert result.stop_reason == "oscillation_detected"
    assert all(item.findings_hash == "deadbeef" for item in result.iterations)


@pytest.mark.asyncio
async def test_oscillation_detected_from_qa_differences_in_meta() -> None:
    """In-process loop must hash QA differences/suggestions when no top-level findings.

    Wiring lock for #788 review-2: ``EvolveStepHandler`` only attaches a
    ``qa`` block to its result meta — it does not synthesize a top-level
    ``findings`` or ``findings_hash`` field. Oscillation detection must
    therefore fall back to ``meta["qa"]["differences"]`` and
    ``meta["qa"]["suggestions"]`` to fire on the real production path.
    """
    qa_payload = {
        "score": 0.4,
        "verdict": "fail",
        "differences": ["acceptance: missing implementation in module X"],
        "suggestions": ["add unit test in tests/unit/test_x.py"],
    }
    evolve = _ScriptedEvolveHandler(
        metas=[
            {"action": "continue", "qa": dict(qa_payload)},
            {"action": "continue", "qa": dict(qa_payload)},
            {"action": "continue", "qa": dict(qa_payload)},
            {"action": "continue", "qa": dict(qa_payload)},
        ]
    )
    runner = RalphLoopRunner(evolve)

    result = await runner.run(
        RalphLoopConfig(
            lineage_id="lin_qa_osc",
            max_generations=5,
            oscillation_window=3,
        )
    )

    assert result.stop_reason == "oscillation_detected"
    assert result.iteration_count == 3
    hashes = [item.findings_hash for item in result.iterations]
    assert hashes[0] is not None
    assert all(h == hashes[0] for h in hashes)


@pytest.mark.asyncio
async def test_qa_differences_fingerprint_changes_when_diffs_change() -> None:
    """Different QA differences across iterations must yield different hashes.

    Without this, the QA-derived fingerprint would be a constant and would
    spuriously trigger oscillation_detected on any loop that happens to run
    QA at all.
    """
    evolve = _ScriptedEvolveHandler(
        metas=[
            {
                "action": "continue",
                "qa": {"differences": ["a"], "suggestions": ["fix a"]},
            },
            {
                "action": "continue",
                "qa": {"differences": ["b"], "suggestions": ["fix b"]},
            },
            {
                "action": "continue",
                "qa": {"differences": ["a"], "suggestions": ["fix a"]},
            },
        ]
    )
    runner = RalphLoopRunner(evolve)

    result = await runner.run(
        RalphLoopConfig(
            lineage_id="lin_qa_diff",
            max_generations=3,
            oscillation_window=3,
        )
    )

    assert result.stop_reason == "max_generations reached"
    assert result.iteration_count == 3
    hashes = [item.findings_hash for item in result.iterations]
    assert hashes[0] is not None
    assert hashes[0] == hashes[2]
    assert hashes[1] != hashes[0]


@pytest.mark.asyncio
async def test_letter_grade_b_maps_to_three_quarters() -> None:
    """Grade letter ``B`` must yield 0.75 and feed regression detection."""
    evolve = _ScriptedEvolveHandler(
        metas=[
            {"action": "continue", "qa": {"grade": "A", "verdict": "fail"}},
            {"action": "continue", "qa": {"grade": "B", "verdict": "fail"}},
        ]
    )
    runner = RalphLoopRunner(evolve)

    result = await runner.run(
        RalphLoopConfig(
            lineage_id="lin_letters",
            max_generations=5,
            grade_regression_window=2,
        )
    )

    assert result.stop_reason == "grade_regressing"
    assert [item.grade for item in result.iterations] == [1.0, 0.75]


@pytest.mark.asyncio
async def test_plugin_dispatch_forwards_progress_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plugin-mode dispatch must forward both progress windows.

    Wiring lock for #788 review-1: when ``should_dispatch_via_plugin`` returns
    True, the produced ``_subagent`` payload context must include both
    ``oscillation_window`` and ``grade_regression_window``. Otherwise the
    public ``stop_reason=oscillation_detected`` and
    ``stop_reason=grade_regressing`` contracts are silently dropped on the
    plugin path while the in-process path still honors them.
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
            "lineage_id": "lin_plugin_progress",
            "seed_content": "goal: ship",
            "max_generations": 5,
            "oscillation_window": 4,
            "grade_regression_window": 3,
        }
    )

    assert result.is_ok
    tool_result = result.value
    body = _json.loads(tool_result.content[0].text)
    sub = body["_subagent"]
    assert sub["tool_name"] == "ouroboros_ralph"
    assert sub["context"]["oscillation_window"] == 4
    assert sub["context"]["grade_regression_window"] == 3
    assert "oscillation_window: 4" in sub["prompt"]
    assert "grade_regression_window: 3" in sub["prompt"]
    assert "stop_reason=oscillation_detected" in sub["prompt"]
    assert "stop_reason=grade_regressing" in sub["prompt"]


@pytest.mark.asyncio
async def test_plugin_dispatch_uses_default_progress_windows_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defaults must round-trip through the plugin payload unchanged.

    A caller that does not pass ``oscillation_window`` or
    ``grade_regression_window`` still picks up the documented defaults
    (3 / 2). The plugin payload must reflect those, otherwise the in-process
    and plugin paths diverge by silent omission.
    """
    import json as _json

    from ouroboros.mcp.tools import ralph_handlers as _ralph_handlers
    from ouroboros.ralph_loop import (
        DEFAULT_GRADE_REGRESSION_WINDOW,
        DEFAULT_OSCILLATION_WINDOW,
    )

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
            "lineage_id": "lin_plugin_defaults",
            "seed_content": "goal: ship",
            "max_generations": 5,
        }
    )

    assert result.is_ok
    body = _json.loads(result.value.content[0].text)
    sub = body["_subagent"]
    assert sub["context"]["oscillation_window"] == DEFAULT_OSCILLATION_WINDOW
    assert sub["context"]["grade_regression_window"] == DEFAULT_GRADE_REGRESSION_WINDOW
