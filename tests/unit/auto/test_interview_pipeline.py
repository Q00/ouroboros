from __future__ import annotations

import asyncio

import pytest

from ouroboros.auto.interview_driver import (
    AutoInterviewDriver,
    FunctionInterviewBackend,
    InterviewTurn,
)
from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus, SeedDraftLedger
from ouroboros.auto.pipeline import AutoPipeline
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
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


def _seed(
    ac: tuple[str, ...] = ("`habit list` prints stable stdout containing created habits",),
) -> Seed:
    return Seed(
        goal="Build a local CLI",
        constraints=("Use existing project patterns",),
        acceptance_criteria=ac,
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


@pytest.mark.asyncio
async def test_interview_driver_blocks_after_max_rounds_with_open_gaps(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What else?", session_id, seed_ready=False)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
        timeout_seconds=1,
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert state.phase == AutoPhase.BLOCKED
    assert "unresolved gaps" in (result.blocker or "")


@pytest.mark.asyncio
async def test_interview_driver_blocks_on_backend_timeout(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        await asyncio.sleep(0.05)
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        timeout_seconds=0.001,
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert "timed out" in (result.blocker or "")


@pytest.mark.asyncio
async def test_pipeline_repairs_b_seed_to_a_and_starts_run(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed(ac=("The CLI should be easy and user-friendly",))

    async def run_seed(seed: Seed) -> dict[str, str | None]:  # noqa: ARG001
        return {"job_id": "job_1", "execution_id": "exec_1"}

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
        timeout_seconds=1,
    )
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.grade == "A"
    assert result.job_id == "job_1"
    assert state.execution_id == "exec_1"


@pytest.mark.asyncio
async def test_pipeline_skip_run_stops_after_a_grade_seed(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed()

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path), skip_run=True)

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.grade == "A"
    assert result.job_id is None


@pytest.mark.asyncio
async def test_interview_resume_uses_persisted_pending_question(tmp_path) -> None:
    calls: list[str] = []

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not start a new interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        calls.append(text)
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "resume interview")
    state.interview_session_id = "interview_1"
    state.pending_question = "What should we verify?"
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )

    result = await driver.run(state, ledger)

    assert result.status == "seed_ready"
    assert calls
    assert "Continue from persisted" not in calls[0]


@pytest.mark.asyncio
async def test_pipeline_non_interview_resume_blocks_without_seed_artifact(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("pipeline should not re-enter interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("pipeline should not re-enter interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("review resume without seed artifact should block")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert "without persisted Seed artifact" in (result.blocker or "")
