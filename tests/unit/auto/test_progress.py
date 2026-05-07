"""Unit tests for the auto pipeline progress event contract."""

from __future__ import annotations

import pytest

from ouroboros.auto.grading import GradeResult, SeedGrade
from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.auto.pipeline import AutoPipeline
from ouroboros.auto.progress import AutoProgressEvent
from ouroboros.auto.seed_repairer import RepairResult
from ouroboros.auto.seed_reviewer import SeedReview
from ouroboros.auto.state import (
    AutoPhase,
    AutoPipelineState,
    AutoStore,
)
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)


class _PassingReviewer:
    """Reviewer stub that always returns an A-grade may_run review."""

    def review(self, _seed, *, ledger=None):  # noqa: ARG002
        grade = GradeResult(grade=SeedGrade.A, scores={}, may_run=True)
        return SeedReview(grade_result=grade, findings=())


class _PassingRepairer:
    """Repairer stub that returns the input seed unchanged with an A grade."""

    def __init__(self, repair_rounds: int = 0) -> None:
        self._repair_rounds = repair_rounds

    def converge(self, seed, *, ledger=None):
        review = _PassingReviewer().review(seed, ledger=ledger)
        history = [
            RepairResult(changed=False, seed=seed, applied_repairs=(), unresolved_findings=())
            for _ in range(self._repair_rounds)
        ]
        return seed, review, history


def _make_seed() -> Seed:
    return Seed(
        goal="Build a CLI",
        constraints=("Use existing project patterns",),
        acceptance_criteria=("Command prints stable output",),
        ontology_schema=OntologySchema(
            name="CliTask",
            description="CLI task ontology",
            fields=(
                OntologyField(
                    name="command",
                    field_type="string",
                    description="Command",
                ),
            ),
        ),
        evaluation_principles=(
            EvaluationPrinciple(
                name="testability",
                description="Observable behavior",
                weight=1.0,
            ),
        ),
        exit_conditions=(
            ExitCondition(
                name="verified",
                description="Checks pass",
                evaluation_criteria="All acceptance criteria pass",
            ),
        ),
        metadata=SeedMetadata(ambiguity_score=0.12),
    )


class _StubInterviewDriver:
    async def run(self, _state, _ledger):  # noqa: ARG002
        raise AssertionError("interview driver should not be invoked at SEED_GENERATION")


def test_auto_progress_event_is_immutable_dataclass() -> None:
    event = AutoProgressEvent(
        auto_session_id="auto_x",
        phase="interview",
        kind="phase",
        message="asking interview round 1/12",
    )
    assert event.round is None
    assert event.grade is None
    assert event.timestamp  # populated via factory
    with pytest.raises(Exception):
        event.kind = "grade"  # type: ignore[misc]


def test_auto_pipeline_with_no_callback_does_not_raise(tmp_path) -> None:
    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _unused_seed_generator,
        store=AutoStore(tmp_path),
    )
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    pipeline._maybe_emit_phase(state)
    pipeline._maybe_emit_grade(state)
    pipeline._maybe_emit_repair(state)


async def _unused_seed_generator(_session_id: str) -> Seed:
    raise AssertionError("seed generator should not be invoked")


@pytest.mark.asyncio
async def test_auto_pipeline_emits_phase_grade_and_repair_in_order(tmp_path) -> None:
    captured: list[AutoProgressEvent] = []

    async def fake_seed_generator(_session_id: str) -> Seed:
        return _make_seed()

    def fake_seed_saver(_seed: Seed) -> str:
        return str(tmp_path / "seed.yaml")

    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    state.ledger = ledger.to_dict()
    state.interview_session_id = "interview_xyz"
    state.interview_completed = True
    state.transition(AutoPhase.INTERVIEW, "primed for resume")
    state.transition(AutoPhase.SEED_GENERATION, "ready for seed generation")
    state.skip_run = True
    store.save(state)

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        fake_seed_generator,
        store=store,
        seed_saver=fake_seed_saver,
        skip_run=True,
        reviewer=_PassingReviewer(),
        repairer=_PassingRepairer(),
        progress_callback=captured.append,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    kinds = [event.kind for event in captured]
    # Phase events fire on each transition; grade fires once after review.
    assert "phase" in kinds
    assert "grade" in kinds
    grade_event = next(event for event in captured if event.kind == "grade")
    assert grade_event.grade == result.grade
    # Repair counter is 0 in this happy path so no repair event is emitted.
    assert "repair" not in kinds
    # Phase events must monotonically advance — no duplicate consecutive phases.
    phase_sequence = [event.phase for event in captured if event.kind == "phase"]
    assert phase_sequence == list(dict.fromkeys(phase_sequence))
    assert phase_sequence[-1] == "complete"


@pytest.mark.asyncio
async def test_auto_pipeline_callback_errors_do_not_break_run(tmp_path) -> None:
    async def fake_seed_generator(_session_id: str) -> Seed:
        return _make_seed()

    def fake_seed_saver(_seed: Seed) -> str:
        return str(tmp_path / "seed.yaml")

    def exploding_callback(_event: AutoProgressEvent) -> None:
        raise RuntimeError("observer failure")

    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    state.ledger = ledger.to_dict()
    state.interview_session_id = "interview_xyz"
    state.interview_completed = True
    state.transition(AutoPhase.INTERVIEW, "primed for resume")
    state.transition(AutoPhase.SEED_GENERATION, "ready for seed generation")
    state.skip_run = True
    store.save(state)

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        fake_seed_generator,
        store=store,
        seed_saver=fake_seed_saver,
        skip_run=True,
        reviewer=_PassingReviewer(),
        repairer=_PassingRepairer(),
        progress_callback=exploding_callback,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"


@pytest.mark.asyncio
async def test_auto_pipeline_emits_phase_for_blocked_terminal(tmp_path) -> None:
    async def fake_seed_generator(_session_id: str) -> Seed:
        raise AssertionError("seed generator should not be invoked when interview blocks")

    captured: list[AutoProgressEvent] = []

    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.interview_completed = True
    state.transition(AutoPhase.INTERVIEW, "primed for resume")
    state.interview_session_id = None  # missing → triggers blocked
    store.save(state)

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        fake_seed_generator,
        store=store,
        progress_callback=captured.append,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    blocked_events = [event for event in captured if event.phase == "blocked"]
    assert blocked_events
    assert blocked_events[-1].kind == "phase"
