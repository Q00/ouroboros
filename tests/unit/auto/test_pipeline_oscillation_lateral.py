"""L5-a regression tests for Ralph ``oscillation_detected`` → UNSTUCK_LATERAL plumbing (#1157).

When Ralph terminates with ``stop_reason == "oscillation_detected"`` in
complete-product mode AND a ``lateral_thinker`` is wired on the
pipeline, the auto pipeline now routes through ``UNSTUCK_LATERAL`` and
invokes ``_run_lateral`` first instead of bailing straight to
``BLOCKED``. Mirrors the EVALUATE→UNSTUCK_LATERAL path already
implemented for QA failures.

Other Ralph blocked stop_reasons (iteration_timeout,
wall_clock_exhausted, grade_regressing, max_generations reached) are
budget-exhaustion terminals rather than spec-reframe candidates, so
they continue to BLOCKED unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest

from ouroboros.auto.adapters import LateralResult
from ouroboros.auto.grading import GradeResult, SeedGrade
from ouroboros.auto.interview_driver import AutoInterviewResult
from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.auto.pipeline import AutoPipeline
from ouroboros.auto.seed_reviewer import SeedReview, SeedReviewer
from ouroboros.auto.state import AutoPhase, AutoPipelineState
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.resilience.lateral import ThinkingPersona

# ---------------------------------------------------------------------------
# Test fixtures — duplicated from test_pipeline_ralph_handoff because
# tests/unit/auto/ is not a Python package (no __init__.py) so a relative
# import is not available. Kept minimal and in sync with the source file.
# ---------------------------------------------------------------------------


def _build_seed(seed_id: str = "seed_test_001") -> Seed:
    return Seed(
        goal="Build a CLI",
        constraints=("Use existing project patterns",),
        acceptance_criteria=("Command prints stable output",),
        ontology_schema=OntologySchema(
            name="CliTask",
            description="CLI task ontology",
            fields=(OntologyField(name="command", field_type="string", description="Command"),),
        ),
        evaluation_principles=(
            EvaluationPrinciple(name="testability", description="Observable behavior", weight=1.0),
        ),
        exit_conditions=(
            ExitCondition(
                name="verified",
                description="Checks pass",
                evaluation_criteria="All acceptance criteria pass",
            ),
        ),
        metadata=SeedMetadata(seed_id=seed_id, ambiguity_score=0.12),
    )


class _StubInterviewDriver:
    def __init__(self) -> None:
        self.invocations = 0
        self.progress_callback = None

    async def run(self, state: AutoPipelineState, ledger: Any) -> AutoInterviewResult:
        self.invocations += 1
        state.interview_session_id = "interview_stub"
        state.interview_completed = True
        return AutoInterviewResult(
            status="seed_ready",
            session_id="interview_stub",
            ledger=ledger,
            rounds=1,
        )


def _state_at_run_phase(tmp_path) -> AutoPipelineState:
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.arm_deadline()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_session_id = "interview_stub"
    state.interview_completed = True
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    seed = _build_seed()
    state.seed_id = seed.metadata.seed_id
    state.seed_artifact = seed.to_dict()
    state.last_grade = "A"
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    return state


def _state_in_ralph_handoff(tmp_path) -> AutoPipelineState:
    state = _state_at_run_phase(tmp_path)
    state.run_start_attempted = True
    state.run_handoff_status = "started"
    state.job_id = "job_run_existing"
    state.execution_id = "execution_existing"
    state.run_session_id = "session_existing"
    state.ralph_lineage_id = "ralph-seed_test_001-auto_abc"
    state.ralph_job_id = "job_ralph_existing"
    state.ralph_dispatch_mode = "job"
    state.transition(AutoPhase.RALPH_HANDOFF, "persisted ralph checkpoint")
    return state


async def _run_starter_ok(_seed: Seed) -> dict[str, Any]:
    return {
        "job_id": "job_run_001",
        "session_id": "exec_session_001",
        "execution_id": "execution_001",
    }


async def _seed_generator_unused(_session_id: str) -> Seed:  # pragma: no cover
    raise AssertionError("seed generator should not run when seed_artifact is set")


class _PassReviewer(SeedReviewer):
    def __init__(self) -> None:  # noqa: D401 - intentionally trivial
        pass

    def review(self, seed: Seed, *, ledger: Any = None) -> SeedReview:  # noqa: ARG002
        grade = GradeResult(grade=SeedGrade.A, scores={}, findings=[], blockers=[], may_run=True)
        return SeedReview(grade_result=grade, findings=())


def _oscillation_ralph_starter():
    """Return a ralph_starter stub that terminates with oscillation_detected."""

    async def ralph_starter(_seed: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "job_id": "job_ralph_oscillate_001",
            "lineage_id": "ralph-oscillate",
            "dispatch_mode": "job",
            "terminal_status": "failed",
            "stop_reason": "oscillation_detected",
        }

    return ralph_starter


@pytest.mark.asyncio
async def test_ralph_oscillation_enters_unstuck_lateral_when_wired(tmp_path) -> None:
    """L5-a: Ralph oscillation + complete_product + lateral_thinker wired →
    transitions through UNSTUCK_LATERAL and runs the persona advisor
    before BLOCKED.

    The lateral thinker is invoked with a synthetic QA-style payload
    derived from the Ralph oscillation context; the persona output is
    persisted on state and surfaced through the result envelope just
    like the EVALUATE→UNSTUCK_LATERAL path does for QA failures.
    """
    state = _state_at_run_phase(tmp_path)

    captured_calls: list[dict[str, Any]] = []

    async def lateral_thinker(**kwargs: Any) -> LateralResult:
        captured_calls.append(dict(kwargs))
        return LateralResult(
            persona="architect",
            approach_summary="Architect: Reframes the spec to prevent oscillation cycles",
            text="# Lateral Thinking: Architect\n\nThe oscillation suggests the AC pair conflicts...",
        )

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_oscillation_ralph_starter(),
        complete_product=True,
        lateral_thinker=lateral_thinker,
    )

    result = await pipeline.run(state)

    # Pipeline lands in BLOCKED after lateral runs to terminal — same shape
    # as the EVALUATE→UNSTUCK_LATERAL path's blocker outcome.
    assert result.status == "blocked"
    assert state.phase is AutoPhase.BLOCKED
    assert state.last_tool_name == "lateral_thinker"

    # Lateral thinker was actually invoked — pin the integration, not the
    # specific persona (persona routing depends on the synthetic QA-shape
    # we synthesize from oscillation_detected, which may shift across
    # persona-routing tweaks).
    assert captured_calls, "lateral_thinker was not invoked"
    first_call = captured_calls[0]
    assert isinstance(first_call.get("persona"), ThinkingPersona)
    # The synthetic QA differences carry the oscillation marker so the
    # persona's reframing input reflects what actually went wrong.
    differences_text = str(first_call.get("qa_differences"))
    assert "oscillat" in differences_text.lower(), (
        f"expected oscillation marker in qa_differences; got {differences_text!r}"
    )

    # Persona output surfaced on the envelope.
    assert state.last_lateral_persona == "architect"
    assert state.last_lateral_approach_summary is not None
    assert "Architect" in state.last_lateral_approach_summary
    assert result.last_lateral_persona == "architect"


@pytest.mark.asyncio
async def test_ralph_oscillation_blocks_directly_when_no_lateral_thinker(tmp_path) -> None:
    """Regression: without a wired lateral_thinker, oscillation_detected
    keeps the legacy behaviour of going straight to BLOCKED. Otherwise
    pipelines that intentionally opt out of lateral recovery would
    suddenly hard-fail differently."""
    state = _state_at_run_phase(tmp_path)

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_oscillation_ralph_starter(),
        complete_product=True,
        lateral_thinker=None,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.phase is AutoPhase.BLOCKED
    assert state.last_error == "oscillation_detected"
    assert state.last_tool_name == "ralph_starter"
    # No lateral persona output should appear on the envelope when the
    # lateral thinker is not wired.
    assert state.last_lateral_persona is None


@pytest.mark.asyncio
async def test_ralph_iteration_timeout_does_not_invoke_lateral(tmp_path) -> None:
    """Regression: ``iteration_timeout`` is a budget terminal, not a
    spec-reframe candidate. It must keep going to BLOCKED directly even
    when a lateral_thinker is wired. Pinned so a future broadening of
    the L5-a path through to budget terminals requires an explicit
    decision rather than slipping in by accident."""
    state = _state_at_run_phase(tmp_path)

    async def ralph_starter(_seed: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "job_id": "job_ralph_iter_timeout",
            "lineage_id": "ralph-iter",
            "dispatch_mode": "job",
            "terminal_status": "failed",
            "stop_reason": "iteration_timeout",
        }

    async def lateral_thinker(**_kwargs: Any) -> LateralResult:  # pragma: no cover
        raise AssertionError("lateral_thinker must not be invoked for iteration_timeout")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
        lateral_thinker=lateral_thinker,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.last_error == "iteration_timeout"
    assert state.last_tool_name == "ralph_starter"


@pytest.mark.asyncio
async def test_ralph_resume_oscillation_enters_unstuck_lateral_when_wired(tmp_path) -> None:
    """L5-a applies to persisted resume polling, not only fresh handoff.

    A client can disconnect after Ralph dispatch and resume only after the
    background Ralph job has already reached ``oscillation_detected``. That
    terminal snapshot has the same producer contract as the live handoff
    result, so it must enter ``UNSTUCK_LATERAL`` instead of reverting to the
    legacy direct-BLOCKED mapping.
    """
    state = _state_in_ralph_handoff(tmp_path)
    captured_calls: list[dict[str, Any]] = []

    async def ralph_resumer(*, job_id: str) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "lineage_id": state.ralph_lineage_id,
            "dispatch_mode": "job",
            "terminal_status": "failed",
            "stop_reason": "oscillation_detected",
        }

    async def ralph_starter(_seed: Seed, **kwargs: Any) -> dict[str, Any]:
        return {
            "job_id": "job_ralph_recovery",
            "lineage_id": kwargs["lineage_id"],
            "dispatch_mode": "job",
            "terminal_status": "completed",
            "stop_reason": "qa passed",
        }

    async def lateral_thinker(**kwargs: Any) -> LateralResult:
        captured_calls.append(dict(kwargs))
        return LateralResult(
            persona="architect",
            approach_summary="Architect: break the oscillation loop",
            text="The resumed Ralph snapshot still needs spec reframing.",
        )

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        ralph_resumer=ralph_resumer,
        complete_product=True,
        lateral_thinker=lateral_thinker,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert state.phase is AutoPhase.COMPLETE
    assert captured_calls, "resume poll did not invoke lateral_thinker"
    differences_text = str(captured_calls[0].get("qa_differences"))
    assert "oscillat" in differences_text.lower()
    assert state.last_lateral_persona == "architect"


@pytest.mark.asyncio
async def test_ralph_reattach_oscillation_enters_unstuck_lateral_when_wired(tmp_path) -> None:
    """L5-a also applies to the direct re-attach terminal consumer.

    ``_reattach_ralph_job`` observes a Ralph job that was already dispatched
    before process death. If that observation returns ``oscillation_detected``,
    it must share the same lateral recovery route as fresh handoff and resume
    polling.
    """
    state = _state_in_ralph_handoff(tmp_path)
    captured_calls: list[dict[str, Any]] = []
    captured_attach: dict[str, Any] = {}

    async def ralph_starter(_seed: Seed | None, **kwargs: Any) -> dict[str, Any]:
        if "attach_job_id" not in kwargs:
            return {
                "job_id": "job_ralph_recovery",
                "lineage_id": kwargs["lineage_id"],
                "dispatch_mode": "job",
                "terminal_status": "completed",
                "stop_reason": "qa passed",
            }
        captured_attach.update(kwargs)
        return {
            "job_id": kwargs["attach_job_id"],
            "lineage_id": kwargs["lineage_id"],
            "dispatch_mode": "job",
            "terminal_status": "failed",
            "stop_reason": "oscillation_detected",
        }

    async def lateral_thinker(**kwargs: Any) -> LateralResult:
        captured_calls.append(dict(kwargs))
        return LateralResult(
            persona="architect",
            approach_summary="Architect: reframe re-attached oscillation",
            text="The re-attached Ralph result still needs lateral recovery.",
        )

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
        lateral_thinker=lateral_thinker,
    )

    result = await pipeline._reattach_ralph_job(state, ledger=SeedDraftLedger.from_goal(state.goal))

    assert captured_attach["attach_job_id"] == "job_ralph_existing"
    assert result.status == "complete"
    assert state.phase is AutoPhase.COMPLETE
    assert captured_calls, "re-attach did not invoke lateral_thinker"
    differences_text = str(captured_calls[0].get("qa_differences"))
    assert "oscillat" in differences_text.lower()
    assert state.last_lateral_persona == "architect"
