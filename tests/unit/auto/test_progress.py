"""Unit tests for the auto pipeline progress event contract."""

from __future__ import annotations

import pytest

from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.auto.pipeline import AutoPipeline
from ouroboros.auto.progress import AutoProgressEvent
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


def test_historical_grade_and_repair_kinds_still_construct() -> None:
    """Recorded ``grade``/``repair`` events (pre-#3064) remain valid events."""
    grade = AutoProgressEvent(
        auto_session_id="auto_x", phase="review", kind="grade", message="Seed grade A", grade="A"
    )
    repair = AutoProgressEvent(
        auto_session_id="auto_x", phase="repair", kind="repair", message="repair round 1", round=1
    )
    assert (grade.kind, grade.grade) == ("grade", "A")
    assert (repair.kind, repair.round) == ("repair", 1)


async def _unused_seed_generator(_session_id: str) -> Seed:
    raise AssertionError("seed generator should not be invoked")


@pytest.mark.asyncio
async def test_auto_pipeline_emits_phase_events_without_grade_or_repair(tmp_path) -> None:
    """A written Seed goes straight on (#3064): no ``grade``/``repair`` events."""
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
        progress_callback=captured.append,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    kinds = {event.kind for event in captured}
    assert kinds == {"phase"}
    # Seed generation hands straight to the skip-run COMPLETE; no review phase.
    phase_sequence = [event.phase for event in captured if event.kind == "phase"]
    assert phase_sequence == ["seed_generation", "complete"]


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
