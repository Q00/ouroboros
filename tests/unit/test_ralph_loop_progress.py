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
from ouroboros.mcp.tools.qa import FAIL_THRESHOLD
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
async def test_ralph_handler_rejects_fractional_oscillation_window() -> None:
    """Fractional floats must not be silently truncated to int.

    Wiring lock for #788 review-3: ``int(2.9)`` would coerce to ``2`` and
    pass the floor check, changing oscillation semantics behind the
    caller's back. The MCP parameter is declared INTEGER, so reject any
    float that is not exactly integral.
    """
    handler = RalphHandler(evolve_handler=_ImmediateEvolveHandler())  # type: ignore[arg-type]

    for value in (2.9, 2.5, 3.1):
        result = await handler.handle(
            {
                "lineage_id": "lin_osc_frac",
                "oscillation_window": value,
            }
        )
        assert result.is_err, f"value={value!r} should be rejected"
        assert "oscillation_window" in str(result.error)
        assert "integer" in str(result.error)


def test_coerce_window_accepts_integer_valued_float_and_strings() -> None:
    """Integer-valued floats / numeric strings round-trip; bools fall through.

    Direct unit test of the helper — exercising it via ``RalphHandler.handle``
    would also spin up a job manager and event store, which is unrelated to
    the contract under test.
    """
    from ouroboros.mcp.errors import MCPToolError
    from ouroboros.mcp.tools.ralph_handlers import _coerce_window

    assert _coerce_window(3, field_name="x") == 3
    assert _coerce_window(3.0, field_name="x") == 3
    assert _coerce_window("4", field_name="x") == 4

    bad = _coerce_window(2.9, field_name="oscillation_window")
    assert isinstance(bad, MCPToolError)
    assert "oscillation_window" in str(bad)
    assert "integer" in str(bad)

    bad_str = _coerce_window("not-a-number", field_name="oscillation_window")
    assert isinstance(bad_str, MCPToolError)
    assert "oscillation_window" in str(bad_str)


@pytest.mark.asyncio
async def test_ralph_handler_rejects_fractional_grade_regression_window() -> None:
    """Mirror of the oscillation fractional check on the grade-regression input."""
    handler = RalphHandler(evolve_handler=_ImmediateEvolveHandler())  # type: ignore[arg-type]

    result = await handler.handle(
        {
            "lineage_id": "lin_grade_frac",
            "grade_regression_window": 2.5,
        }
    )

    assert result.is_err
    assert "grade_regression_window" in str(result.error)
    assert "integer" in str(result.error)


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

    handler = RalphHandler(
        evolve_handler=_ImmediateEvolveHandler(),  # type: ignore[arg-type]
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
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

    from ouroboros.ralph_loop import (
        DEFAULT_GRADE_REGRESSION_WINDOW,
        DEFAULT_OSCILLATION_WINDOW,
    )

    handler = RalphHandler(
        evolve_handler=_ImmediateEvolveHandler(),  # type: ignore[arg-type]
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
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


def _qa_meta(score: float, verdict: str, pass_threshold: float = 0.8) -> dict[str, Any]:
    """Build the ``meta["qa"]`` payload exactly as ``QAHandler.handle`` publishes it.

    ``passed`` is derived from the score, never from the model's ``verdict``
    string (``src/ouroboros/mcp/tools/qa.py``), which is what makes the two
    fields capable of contradicting each other.
    """
    if score >= pass_threshold:
        loop_action = "done"
    elif score >= FAIL_THRESHOLD:
        loop_action = "continue"
    else:
        loop_action = "escalate"
    return {
        "score": score,
        "verdict": verdict,
        "pass_threshold": pass_threshold,
        "passed": score >= pass_threshold,
        "loop_action": loop_action,
    }


@pytest.mark.asyncio
async def test_sub_threshold_score_does_not_stop_the_loop_on_a_pass_verdict() -> None:
    """A model-authored ``verdict: "pass"`` must not outrank the QA gate's own numbers.

    The verdict string comes straight from the model whenever it is one of the
    valid tokens; ``passed``/``score``/``pass_threshold`` are computed by the QA
    gate itself. When they contradict, the computed result wins — otherwise one
    string field converts a failed generation into a terminal success.
    """
    evolve = _ScriptedEvolveHandler(
        metas=[
            {"action": "continue", "qa": _qa_meta(0.1, "pass")},
            {"action": "continue", "qa": _qa_meta(0.1, "pass")},
        ]
    )
    runner = RalphLoopRunner(evolve)

    result = await runner.run(
        RalphLoopConfig(lineage_id="lin_contradicted_pass", max_generations=2)
    )

    assert result.stop_reason != "qa passed"
    assert result.stop_reason == "max_generations reached"
    assert result.iteration_count == 2
    assert evolve.calls == 2
    # An authoritative QA failure must not serialize as a successful, non-error
    # terminal result — `to_tool_result` is what the job/auto layer reads.
    assert result.status == "failed"
    assert result.to_tool_result().is_error is True


@pytest.mark.asyncio
async def test_uncontradicted_pass_verdict_still_stops_the_loop() -> None:
    """The guard must only fire on contradiction: an honest pass still stops at once."""
    evolve = _ScriptedEvolveHandler(
        metas=[
            {"action": "continue", "qa": _qa_meta(0.95, "pass")},
            {"action": "continue", "qa": _qa_meta(0.95, "pass")},
        ]
    )
    runner = RalphLoopRunner(evolve)

    result = await runner.run(RalphLoopConfig(lineage_id="lin_honest_pass", max_generations=2))

    assert result.status == "completed"
    assert result.stop_reason == "qa passed"
    assert result.iteration_count == 1
    assert evolve.calls == 1


@pytest.mark.asyncio
async def test_converged_action_does_not_complete_on_a_contradicted_qa_pass() -> None:
    """``converged`` is the sibling success exit and must consult the same QA result.

    ``evolution_handlers`` runs post-execution QA for both ``continue`` and
    ``converged`` and publishes the pre-QA action unchanged, so a converged step
    can arrive carrying an authoritative QA failure. Guarding only the ``qa
    passed`` branch would leave this exit reporting terminal success.
    """
    evolve = _ScriptedEvolveHandler(metas=[{"action": "converged", "qa": _qa_meta(0.1, "pass")}])
    runner = RalphLoopRunner(evolve)

    result = await runner.run(RalphLoopConfig(lineage_id="lin_converged_bad_qa", max_generations=3))

    assert result.status == "failed"
    assert result.stop_reason == "qa_failed"
    assert result.to_tool_result().is_error is True
    assert result.iteration_count == 1


@pytest.mark.asyncio
async def test_converged_action_still_completes_on_an_uncontradicted_qa_pass() -> None:
    """The converged guard must fire only on contradiction, not on every converge."""
    evolve = _ScriptedEvolveHandler(metas=[{"action": "converged", "qa": _qa_meta(0.95, "pass")}])
    runner = RalphLoopRunner(evolve)

    result = await runner.run(RalphLoopConfig(lineage_id="lin_converged_ok", max_generations=3))

    assert result.status == "completed"
    assert result.to_tool_result().is_error is False
    assert result.iteration_count == 1


@pytest.mark.asyncio
async def test_converged_without_any_qa_attempt_keeps_completing() -> None:
    """A payload from a generation where QA never ran keeps its legacy behaviour.

    Narrow on purpose: no ``executed`` marker means the producer never reached its
    QA branch, so there is no missing verdict to fail closed on. Contrast with
    ``test_converged_fails_when_qa_ran_but_returned_no_verdict``, where QA did run
    and the absent block is the evidence that it failed.
    """
    evolve = _ScriptedEvolveHandler(metas=[{"action": "converged"}])
    runner = RalphLoopRunner(evolve)

    result = await runner.run(RalphLoopConfig(lineage_id="lin_converged_legacy", max_generations=3))

    assert result.status == "completed"
    assert result.stop_reason == "converged"
    assert result.to_tool_result().is_error is False


@pytest.mark.asyncio
async def test_exhaustion_fails_when_qa_ran_but_returned_no_verdict() -> None:
    """Absent QA evidence must fail closed when QA was actually attempted.

    ``evolution_handlers`` drops the whole ``qa`` block when ``QAHandler`` returns
    an error, which is exactly what a malformed or truncated QA completion
    produces. Gating on "did QA authoritatively fail?" would then make a garbled
    QA response *safer* than a parseable failing one, so the gate has to key on
    evidence of a pass. ``executed`` marks the runs where QA was in fact run.
    """
    evolve = _ScriptedEvolveHandler(
        metas=[
            {"action": "continue", "qa_attempted": True, "qa": _qa_meta(0.1, "fail")},
            # QA ran again but its completion could not be parsed → no `qa` key.
            {"action": "continue", "qa_attempted": True},
        ]
    )
    runner = RalphLoopRunner(evolve)

    result = await runner.run(RalphLoopConfig(lineage_id="lin_qa_unparseable", max_generations=2))

    assert result.status == "failed"
    assert result.stop_reason == "max_generations reached"
    assert result.to_tool_result().is_error is True


@pytest.mark.asyncio
async def test_converged_fails_when_qa_ran_but_returned_no_verdict() -> None:
    """The converged exit must not mint a success out of a missing QA verdict."""
    evolve = _ScriptedEvolveHandler(metas=[{"action": "converged", "qa_attempted": True}])
    runner = RalphLoopRunner(evolve)

    result = await runner.run(RalphLoopConfig(lineage_id="lin_converged_no_qa", max_generations=3))

    assert result.status == "failed"
    assert result.stop_reason == "qa_failed"
    assert result.to_tool_result().is_error is True


@pytest.mark.asyncio
async def test_skip_qa_keeps_completing_without_any_qa_evidence() -> None:
    """The legitimate QA-disabled opt-out must stay intact."""
    evolve = _ScriptedEvolveHandler(metas=[{"action": "converged", "executed": True}])
    runner = RalphLoopRunner(evolve)

    result = await runner.run(
        RalphLoopConfig(lineage_id="lin_skip_qa", max_generations=3, skip_qa=True)
    )

    assert result.status == "completed"
    assert result.stop_reason == "converged"
    assert result.to_tool_result().is_error is False
