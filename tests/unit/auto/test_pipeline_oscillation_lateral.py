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

from ouroboros.auto.grading import GradeResult, SeedGrade
from ouroboros.auto.interview_driver import AutoInterviewResult
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
