from __future__ import annotations

import asyncio

import pytest

from ouroboros.auto.grading import GradeResult, SeedGrade
from ouroboros.auto.interview_driver import (
    AutoInterviewDriver,
    FunctionInterviewBackend,
    InterviewTurn,
)
from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus, SeedDraftLedger
from ouroboros.auto.pipeline import AutoPipeline
from ouroboros.auto.seed_reviewer import ReviewFinding, SeedReview
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
    repaired_acceptance = state.seed_artifact["acceptance_criteria"][0]
    assert "The CLI" in repaired_acceptance
    assert "stable observable output" in repaired_acceptance
    assert (
        repaired_acceptance
        != "A command/API check returns stable observable output or artifacts proving this requirement."
    )
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


@pytest.mark.asyncio
async def test_interview_resume_backend_error_blocks_and_persists(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not start a new interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer without a question")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "resume interview")
    state.interview_session_id = "interview_1"
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert state.phase == AutoPhase.BLOCKED
    assert "resume/start failed" in (result.blocker or "")


@pytest.mark.asyncio
async def test_pipeline_seed_generator_error_marks_failed(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise RuntimeError("generator exploded")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "failed"
    assert "seed generation failed" in (result.blocker or "")


@pytest.mark.asyncio
async def test_pipeline_run_starter_error_marks_failed(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed()

    async def run_seed(seed: Seed) -> dict[str, str | None]:  # noqa: ARG001
        raise RuntimeError("runner exploded")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "failed"
    assert "run start failed" in (result.blocker or "")


@pytest.mark.asyncio
async def test_pipeline_serializes_blocking_review_findings(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed(ac=("The command uses clean architecture",))

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()

    class BlockingRepairer:
        def converge(
            self, seed: Seed, *, ledger: SeedDraftLedger
        ) -> tuple[Seed, SeedReview, list[object]]:  # noqa: ARG002
            finding = ReviewFinding.from_parts(
                code="still_vague",
                target="acceptance_criteria[0]",
                severity="high",
                message="Still not observable",
                repair_instruction="Make it observable.",
            )
            review = SeedReview(
                grade_result=GradeResult(
                    grade=SeedGrade.B,
                    scores={
                        "coverage": 0.8,
                        "ambiguity": 0.3,
                        "testability": 0.4,
                        "execution_feasibility": 0.8,
                        "risk": 0.2,
                    },
                    findings=[],
                    blockers=[],
                    may_run=False,
                ),
                findings=(finding,),
            )
            return seed, review, []

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )
    pipeline = AutoPipeline(
        driver, generate_seed, store=AutoStore(tmp_path), repairer=BlockingRepairer(), skip_run=True
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.findings
    assert "fingerprint" in state.findings[0]


@pytest.mark.asyncio
async def test_interview_driver_blocks_when_backend_never_marks_ready(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("Another question", session_id, seed_ready=False, completed=False)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert "before backend marked" in (result.blocker or "")


@pytest.mark.asyncio
async def test_pipeline_resumes_review_from_persisted_seed_artifact(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("resume should not regenerate seed")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.seed_artifact = _seed().to_dict()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path), skip_run=True)

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.grade == "A"


@pytest.mark.asyncio
async def test_pipeline_resumes_completed_interview_without_reanswering(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("completed interview should not restart")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("completed interview should not answer again")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed()

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_session_id = "interview_1"
    state.interview_completed = True
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path), skip_run=True)

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.grade == "A"


@pytest.mark.asyncio
async def test_pipeline_refuses_duplicate_unknown_run_resume(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("run resume should not regenerate seed")

    async def run_seed(seed: Seed) -> dict[str, str | None]:  # noqa: ARG001
        raise AssertionError("unknown run resume should not start another run")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.seed_artifact = _seed().to_dict()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    state.run_start_attempted = True
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert "duplicate execution" in (result.blocker or "")


@pytest.mark.asyncio
async def test_pipeline_blocks_run_start_without_tracking_handle(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed()

    async def run_seed(seed: Seed) -> dict[str, str | None]:  # noqa: ARG001
        return {"job_id": None, "execution_id": None}

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert "tracking handle" in (result.blocker or "")
    assert state.phase == AutoPhase.BLOCKED


@pytest.mark.asyncio
async def test_pipeline_resumes_run_with_persisted_handle_without_restarting(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("run resume should not regenerate seed")

    async def run_seed(seed: Seed) -> dict[str, str | None]:  # noqa: ARG001
        raise AssertionError("persisted run handle should not start another run")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.seed_artifact = _seed().to_dict()
    state.job_id = "job_existing"
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.job_id == "job_existing"


@pytest.mark.asyncio
async def test_interview_driver_persists_blocker_ledger_entry(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What API key should the workflow use?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("blocker should stop before backend answer")

    state = AutoPipelineState(goal="Deploy a service", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert state.ledger
    persisted = SeedDraftLedger.from_dict(state.ledger)
    assert any(
        entry.status == LedgerStatus.BLOCKED for entry in persisted.sections["constraints"].entries
    )
    assert persisted.question_history


@pytest.mark.asyncio
async def test_pipeline_blocks_completed_interview_without_session_id(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("completed interview should not restart")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("completed interview should not answer")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("missing interview session should not generate seed")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_completed = True
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path), skip_run=True)

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert "interview_session_id" in (result.blocker or "")


@pytest.mark.asyncio
async def test_pipeline_resumes_repair_phase_through_review(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("repair resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("repair resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("repair resume should not regenerate seed")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.seed_artifact = _seed().to_dict()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.REPAIR, "repair")
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path), skip_run=True)

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.grade == "A"


@pytest.mark.asyncio
async def test_interview_driver_blocks_when_backend_completes_before_ledger_ready(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=3
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert "completed before auto ledger was ready" in (result.blocker or "")
    assert state.phase == AutoPhase.BLOCKED


@pytest.mark.asyncio
async def test_interview_driver_steers_generic_questions_to_open_gaps(tmp_path) -> None:
    answers: list[str] = []

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What else should we know?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        answers.append(text)
        completed = len(answers) >= 5
        return InterviewTurn("What else should we know?", session_id, completed=completed)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=6
    )

    result = await driver.run(state, ledger)

    assert result.status == "seed_ready"
    assert ledger.is_seed_ready()
    assert any("single local user" in item.lower() for item in answers)
    assert any("non-goals" in item.lower() or "non-goal" in item.lower() for item in answers)
    assert any("runtime" in item.lower() for item in answers)


def test_auto_state_rejects_malformed_resume_optional_fields() -> None:
    base = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project").to_dict()
    base["pending_question"] = []

    with pytest.raises(ValueError, match="pending_question"):
        AutoPipelineState.from_dict(base)

    base = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project").to_dict()
    base["interview_completed"] = "yes"

    with pytest.raises(ValueError, match="interview_completed"):
        AutoPipelineState.from_dict(base)


@pytest.mark.asyncio
async def test_interview_driver_does_not_persist_completion_as_pending_question(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )

    result = await driver.run(state, ledger)

    assert result.status == "seed_ready"
    assert state.interview_completed is True
    assert state.pending_question is None


@pytest.mark.asyncio
async def test_pipeline_blocks_completed_interview_with_unresolved_ledger(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("completed interview should not restart")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("completed interview should not answer")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("unresolved completed interview should not generate seed")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_session_id = "interview_1"
    state.interview_completed = True
    state.ledger = SeedDraftLedger.from_goal(state.goal).to_dict()
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path), skip_run=True)

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert "unresolved ledger gaps" in (result.blocker or "")


@pytest.mark.asyncio
async def test_pipeline_marks_malformed_seed_generator_result_failed(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("completed interview should not restart")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("completed interview should not answer")

    async def generate_seed(session_id: str):  # noqa: ANN202, ARG001
        return {"not": "a seed"}

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_session_id = "interview_1"
    state.interview_completed = True
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path), skip_run=True)

    result = await pipeline.run(state)

    assert result.status == "failed"
    assert "expected Seed" in (result.blocker or "")


@pytest.mark.asyncio
async def test_pipeline_marks_malformed_run_starter_result_failed(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed()

    async def run_seed(seed: Seed):  # noqa: ANN202, ARG001
        return ["not", "metadata"]

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "failed"
    assert "expected dict" in (result.blocker or "")


@pytest.mark.asyncio
async def test_interview_driver_blocks_malformed_backend_turn(tmp_path) -> None:
    async def start(goal: str, cwd: str):  # noqa: ANN202, ARG001
        return {"question": "not a turn"}

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("malformed start should not answer")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert "expected InterviewTurn" in (result.blocker or "")


@pytest.mark.asyncio
async def test_interview_driver_clears_pending_question_before_backend_answer(tmp_path) -> None:
    store = AutoStore(tmp_path)

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        persisted = store.load(state.auto_session_id)
        assert persisted.pending_question is None
        assert persisted.last_tool_name == "auto_answerer"
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=store, max_rounds=1)

    result = await driver.run(state, ledger)

    assert result.status == "seed_ready"
    assert state.pending_question is None


@pytest.mark.asyncio
async def test_pipeline_seed_generation_resume_uses_persisted_seed_artifact(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("seed resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("seed resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("persisted seed artifact should not regenerate")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_session_id = "interview_1"
    state.interview_completed = True
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.seed_id = "seed_existing"
    state.seed_artifact = _seed().to_dict()
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path), skip_run=True)

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.grade == "A"


@pytest.mark.asyncio
async def test_pipeline_resumes_prepared_run_before_first_attempt(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("run resume should not regenerate seed")

    async def run_seed(seed: Seed) -> dict[str, str | None]:  # noqa: ARG001
        return {"job_id": "job_after_resume", "execution_id": "exec_after_resume"}

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.seed_artifact = _seed().to_dict()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run prepared")
    state.run_start_attempted = False
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.job_id == "job_after_resume"
    assert state.run_start_attempted is True


@pytest.mark.asyncio
async def test_pipeline_seed_generation_resume_requires_interview_session_id(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("seed resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("seed resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("missing interview session should fail before generator")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_completed = True
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path), skip_run=True)

    result = await pipeline.run(state)

    assert result.status == "failed"
    assert "interview_session_id" in (result.blocker or "")


@pytest.mark.asyncio
async def test_pipeline_review_resume_marks_malformed_seed_artifact_failed(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("review resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("review resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("review resume should not regenerate seed")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.seed_artifact = {"goal": "missing required fields"}
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path), skip_run=True)

    result = await pipeline.run(state)

    assert result.status == "failed"
    assert "persisted Seed artifact is invalid" in (result.blocker or "")


@pytest.mark.asyncio
async def test_interview_driver_accepts_initial_completed_turn_without_answering(tmp_path) -> None:
    answered = False

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("already complete", "interview_done", seed_ready=True, completed=True)

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        nonlocal answered
        answered = True
        raise AssertionError("completed initial turn should not be answered")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))

    result = await driver.run(state, ledger)

    assert result.status == "seed_ready"
    assert result.rounds == 0
    assert state.interview_completed is True
    assert state.pending_question is None
    assert not answered
