"""Tests for AutoPipeline active domain profile wiring."""

from __future__ import annotations

from typing import Any

import pytest

from ouroboros.auto.answerer import AutoAnswerer
from ouroboros.auto.grading import GradeResult, SeedGrade
from ouroboros.auto.interview_driver import AutoInterviewResult
from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus, SeedDraftLedger
from ouroboros.auto.pipeline import AutoPipeline, _apply_active_profile
from ouroboros.auto.seed_reviewer import SeedReview, SeedReviewer
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoResumeCapability
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)


def test_apply_active_profile_preserves_safety_hatch_when_none() -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    answerer = AutoAnswerer(active_profile=object())  # type: ignore[arg-type]

    _apply_active_profile(state, answerer)

    assert answerer.active_profile is None


def test_apply_active_profile_rejects_unknown_durable_profile_name() -> None:
    state = AutoPipelineState(
        goal="Build a CLI",
        cwd="/tmp/project",
        active_domain_profile_name="missing-profile",
    )
    answerer = AutoAnswerer()

    with pytest.raises(
        ValueError, match="active domain profile is not registered: missing-profile"
    ):
        _apply_active_profile(state, answerer)


class _DriverWithAnswerer:
    def __init__(self) -> None:
        self.answerer = AutoAnswerer()
        self.progress_callback = None
        self.invocations = 0

    async def run(self, state, ledger):  # noqa: ANN001
        self.invocations += 1
        return AutoInterviewResult(
            status="seed_ready",
            session_id="interview_should_not_run",
            ledger=ledger,
            rounds=1,
        )


def _fill_ready(ledger: SeedDraftLedger) -> None:
    for section, value in {
        "actors": "Single local CLI user",
        "inputs": "Command arguments",
        "outputs": "Stable stdout and files",
        "constraints": "Use existing project patterns",
        "non_goals": "No cloud sync",
        "acceptance_criteria": "Command prints stable output",
        "verification_plan": "Run command-level tests",
        "failure_modes": "Invalid input exits non-zero",
        "runtime_context": "Existing repository runtime",
    }.items():
        source = (
            LedgerSource.NON_GOAL if section == "non_goals" else LedgerSource.CONSERVATIVE_DEFAULT
        )
        ledger.add_entry(
            section,
            LedgerEntry(
                key=f"{section}.test",
                value=value,
                source=source,
                confidence=0.85,
                status=LedgerStatus.DEFAULTED,
            ),
        )


def _seed() -> Seed:
    return Seed(
        goal="Build a local CLI",
        constraints=("Use existing project patterns",),
        acceptance_criteria=("Command prints stable output",),
        ontology_schema=OntologySchema(
            name="CliTask",
            description="CLI task ontology",
            fields=(OntologyField(name="command", field_type="string", description="Command"),),
        ),
        evaluation_principles=(
            EvaluationPrinciple(name="testability", description="Observable behavior"),
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


async def _seed_generator_ok(_session_id: str) -> Seed:
    return _seed()


async def _unused_seed_generator(_session_id: str):  # pragma: no cover
    raise AssertionError("seed generator should not run for invalid active profile")


class _PassReviewer(SeedReviewer):
    def __init__(self) -> None:
        pass

    def review(self, seed: Seed, *, ledger: Any = None) -> SeedReview:  # noqa: ARG002
        grade = GradeResult(grade=SeedGrade.A, scores={}, findings=[], blockers=[], may_run=True)
        return SeedReview(grade_result=grade, findings=())


@pytest.mark.asyncio
async def test_pipeline_blocks_cleanly_when_durable_profile_is_missing() -> None:
    state = AutoPipelineState(
        goal="Build a CLI",
        cwd="/tmp/project",
        active_domain_profile_name="missing-profile",
    )
    driver = _DriverWithAnswerer()
    pipeline = AutoPipeline(driver, _unused_seed_generator)

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.last_tool_name == "domain_profile_registry"
    assert state.last_error == "active domain profile is not registered: missing-profile"
    assert result.blocker == state.last_error
    assert state.resume_capability() is AutoResumeCapability.RETRY
    assert driver.invocations == 0


@pytest.mark.asyncio
async def test_pipeline_does_not_resolve_profile_for_completed_interview_resume() -> None:
    state = AutoPipelineState(
        goal="Build a CLI",
        cwd="/tmp/project",
        active_domain_profile_name="missing-profile",
    )
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.phase = AutoPhase.INTERVIEW
    state.interview_completed = True
    state.interview_session_id = "interview_done"
    driver = _DriverWithAnswerer()
    pipeline = AutoPipeline(
        driver,
        _seed_generator_ok,
        reviewer=_PassReviewer(),
        skip_run=True,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert state.phase == AutoPhase.COMPLETE
    assert state.last_tool_name != "domain_profile_registry"
    assert state.last_error is None
    assert driver.invocations == 0


@pytest.mark.asyncio
async def test_pipeline_does_not_resolve_profile_after_interview_phase() -> None:
    state = AutoPipelineState(
        goal="Build a CLI",
        cwd="/tmp/project",
        active_domain_profile_name="missing-profile",
    )
    state.phase = AutoPhase.COMPLETE
    driver = _DriverWithAnswerer()
    pipeline = AutoPipeline(driver, _unused_seed_generator)

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert state.last_tool_name is None
    assert state.last_error is None
    assert driver.invocations == 0
