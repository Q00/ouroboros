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
from ouroboros.auto.seed_repairer import SeedRepairer
from ouroboros.auto.seed_reviewer import ReviewFinding, SeedReview, SeedReviewer
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
        return {"job_id": "job_1", "execution_id": "exec_1", "session_id": "session_1"}

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
    assert result.run_session_id == "session_1"
    assert state.execution_id == "exec_1"
    assert state.run_session_id == "session_1"


def test_seed_repairer_rewrites_each_acceptance_criterion_once() -> None:
    seed = _seed(ac=("The CLI should be easy and user-friendly",))
    ledger = SeedDraftLedger.from_goal(seed.goal)
    _fill_ready(ledger)
    review = SeedReviewer().review(seed, ledger=ledger)

    result = SeedRepairer().repair_once(seed, review, ledger=ledger)

    assert result.changed
    repaired_acceptance = result.seed.acceptance_criteria[0]
    assert repaired_acceptance.count("original requirement for") == 1
    assert "The CLI" in repaired_acceptance
    assert "original requirement for A command/API check" not in repaired_acceptance


def test_seed_repairer_assigns_new_seed_identity_after_mutation() -> None:
    seed = _seed(ac=("The CLI should be easy and user-friendly",))
    ledger = SeedDraftLedger.from_goal(seed.goal)
    _fill_ready(ledger)
    review = SeedReviewer().review(seed, ledger=ledger)

    result = SeedRepairer().repair_once(seed, review, ledger=ledger)

    assert result.changed
    assert result.seed.metadata.seed_id != seed.metadata.seed_id
    assert result.seed.metadata.parent_seed_id == seed.metadata.seed_id


def test_seed_repairer_non_goals_do_not_contradict_goal_scope() -> None:
    seed = _seed()
    ledger = SeedDraftLedger.from_goal("Add authentication and deploy this service to production")
    finding = ReviewFinding.from_parts(
        code="missing_non_goals",
        target="non_goals",
        severity="medium",
        message="Auto-generated Seed has no explicit non-goals",
        repair_instruction="Add MVP non-goals to bound scope.",
    )
    review = SeedReview(
        grade_result=GradeResult(
            grade=SeedGrade.B,
            scores={
                "coverage": 0.8,
                "ambiguity": 0.1,
                "testability": 0.9,
                "execution_feasibility": 0.9,
                "risk": 0.1,
            },
            findings=[],
            blockers=[],
            may_run=False,
        ),
        findings=(finding,),
    )

    result = SeedRepairer().repair_once(seed, review, ledger=ledger)

    assert result.changed
    non_goals = ledger.non_goals()
    assert non_goals
    assert "authentication" not in non_goals[0].lower()
    assert "production deployment" not in non_goals[0].lower()


def test_seed_repairer_converge_returns_latest_repair_when_high_findings_repeat() -> None:
    original_seed_id: str | None = None
    finding = ReviewFinding.from_parts(
        code="vague_acceptance_criteria",
        target="acceptance_criteria[0]",
        severity="high",
        message="Still vague",
        repair_instruction="Make it observable.",
    )

    class RepeatingReviewer:
        def review(self, seed: Seed, *, ledger: SeedDraftLedger | None = None) -> SeedReview:  # noqa: ARG002
            coverage = 0.1 if seed.metadata.seed_id == original_seed_id else 0.9
            return SeedReview(
                grade_result=GradeResult(
                    grade=SeedGrade.B,
                    scores={
                        "coverage": coverage,
                        "ambiguity": 0.2,
                        "testability": 0.5,
                        "execution_feasibility": 0.8,
                        "risk": 0.1,
                    },
                    findings=[],
                    blockers=[],
                    may_run=False,
                ),
                findings=(finding,),
            )

    seed = _seed(ac=("The CLI should be easy and user-friendly",))
    original_seed_id = seed.metadata.seed_id
    repaired, final_review, history = SeedRepairer(
        reviewer=RepeatingReviewer(), max_repair_rounds=3
    ).converge(seed)

    assert history
    assert repaired == history[-1].seed
    assert repaired != seed
    assert final_review.grade_result.scores["coverage"] == 0.9


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
    assert "interview resume failed" in (result.blocker or "")


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
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
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
    assert state.run_start_attempted is False


@pytest.mark.asyncio
async def test_pipeline_blocks_duplicate_run_after_start_timeout(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("resume should not regenerate seed")

    calls = 0

    async def run_seed(seed: Seed) -> dict[str, str | None]:  # noqa: ARG001
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return {"job_id": "job_after_timeout", "execution_id": "exec_after_timeout"}

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.seed_artifact = _seed().to_dict()
    state.last_grade = "A"
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        run_starter=run_seed,
        store=AutoStore(tmp_path),
        run_start_timeout_seconds=0.001,
    )

    first = await pipeline.run(state)
    pipeline.run_start_timeout_seconds = 1
    second = await pipeline.run(state)

    assert first.status == "blocked"
    assert state.run_start_attempted is True
    assert second.status == "blocked"
    assert "duplicate execution" in (second.blocker or "")
    assert calls == 1


@pytest.mark.asyncio
async def test_pipeline_retries_after_malformed_run_starter_metadata(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("resume should not regenerate seed")

    calls = 0

    async def run_seed(seed: Seed) -> dict[str, str | None]:  # noqa: ARG001
        nonlocal calls
        calls += 1
        if calls == 1:
            return {}
        return {"job_id": "job_after_retry", "execution_id": "exec_after_retry"}

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.seed_artifact = _seed().to_dict()
    state.last_grade = "A"
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    first = await pipeline.run(state)
    second = await pipeline.run(state)

    assert first.status == "blocked"
    assert state.run_start_attempted is True
    assert second.status == "complete"
    assert second.job_id == "job_after_retry"
    assert calls == 2


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
async def test_pipeline_returns_structured_failure_for_terminal_malformed_seed_artifact(
    tmp_path,
) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("terminal resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("terminal resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("terminal resume should not generate seed")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.seed_artifact = {"goal": "missing required seed fields"}
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.COMPLETE, "complete")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "failed"
    assert "persisted Seed artifact is invalid" in (result.blocker or "")


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
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.seed_artifact = _seed().to_dict()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.last_grade = "A"
    state.transition(AutoPhase.RUN, "run prepared")
    state.run_start_attempted = False
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.job_id == "job_after_resume"
    assert state.run_start_attempted is True


@pytest.mark.asyncio
async def test_pipeline_persists_seed_path_before_skip_run(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed()

    saved: list[str] = []

    def save(seed: Seed) -> str:
        path = str(tmp_path / f"{seed.metadata.seed_id}.yaml")
        saved.append(path)
        return path

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )
    pipeline = AutoPipeline(
        driver, generate_seed, store=AutoStore(tmp_path), seed_saver=save, skip_run=True
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.seed_path == saved[0]
    assert state.seed_path == saved[0]


@pytest.mark.asyncio
async def test_pipeline_resumes_blocked_seed_generation(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed()

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_session_id = "interview_1"
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.mark_blocked("seed generation timed out", tool_name="seed_generator")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path), skip_run=True)

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.grade == "A"


@pytest.mark.asyncio
async def test_pipeline_run_resume_rechecks_persisted_ledger_before_execution(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("run resume should not regenerate seed")

    async def run_seed(seed: Seed) -> dict[str, str | None]:  # noqa: ARG001
        raise AssertionError("unresolved ledger must not start execution")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.seed_artifact = _seed().to_dict()
    state.last_grade = "A"
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run prepared")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert "clear the Seed for execution" in (result.blocker or "")
    assert result.grade == "C"


@pytest.mark.asyncio
async def test_pipeline_refuses_run_resume_without_a_grade(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("run resume should not regenerate seed")

    async def run_seed(seed: Seed) -> dict[str, str | None]:  # noqa: ARG001
        raise AssertionError("non-A run resume must not start execution")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.seed_artifact = _seed().to_dict()
    state.last_grade = "B"
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run prepared")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert "persisted grade" in (result.blocker or "")
    assert state.job_id is None


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
async def test_pipeline_refuses_blocked_run_start_replay_from_seed_path(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("unknown run resume should not generate seed")

    async def run_seed(seed: Seed) -> dict[str, str | None]:  # noqa: ARG001
        raise AssertionError("ambiguous run start resume should not start another run")

    seed = _seed()
    seed_path = str(tmp_path / "seed.yaml")
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.seed_path = seed_path
    state.last_grade = "A"
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    state.mark_blocked("run start timed out", tool_name="run_starter")
    state.run_start_attempted = True
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        run_starter=run_seed,
        store=AutoStore(tmp_path),
        seed_loader=lambda path: (
            seed if path == seed_path else (_ for _ in ()).throw(AssertionError(path))
        ),
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert "duplicate execution" in (result.blocker or "")
    assert result.job_id is None


@pytest.mark.asyncio
async def test_pipeline_recovers_auto_answerer_block_to_interview(tmp_path) -> None:
    calls: list[str] = []

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not start a new interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        calls.append(text)
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed()

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_session_id = "interview_1"
    state.pending_question = "What should we verify?"
    state.mark_blocked("needs human authority", tool_name="auto_answerer")
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path), skip_run=True)

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert calls


@pytest.mark.asyncio
async def test_pipeline_replays_persisted_run_subagent_after_complete_resume(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed()

    async def run_seed(seed: Seed) -> dict[str, object]:  # noqa: ARG001
        return {
            "session_id": "session_1",
            "_subagent": {"tool_name": "ouroboros_execute_seed", "context": {"seed": "x"}},
        }

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    first = await pipeline.run(state)
    resumed = await pipeline.run(state)

    assert first.status == "complete"
    assert first.run_subagent == {"tool_name": "ouroboros_execute_seed", "context": {"seed": "x"}}
    assert state.run_subagent == first.run_subagent
    assert resumed.run_subagent == first.run_subagent


@pytest.mark.asyncio
async def test_pipeline_seed_save_error_marks_failed(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed()

    def save(seed: Seed) -> str:  # noqa: ARG001
        raise OSError("disk full")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )
    pipeline = AutoPipeline(
        driver, generate_seed, store=AutoStore(tmp_path), seed_saver=save, skip_run=True
    )

    result = await pipeline.run(state)

    assert result.status == "failed"
    assert "seed save failed" in (result.blocker or "")


@pytest.mark.asyncio
async def test_pipeline_resumes_seed_saver_failure_from_review(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("resume should not regenerate seed")

    seed = _seed()
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.seed_artifact = seed.to_dict()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.mark_failed("seed save failed: disk full", tool_name="seed_saver")
    saved: list[str] = []

    def save(recovered: Seed) -> str:
        saved.append(recovered.metadata.seed_id)
        return str(tmp_path / "seed.yaml")

    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(
        driver, generate_seed, store=AutoStore(tmp_path), seed_saver=save, skip_run=True
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert saved == [seed.metadata.seed_id]


@pytest.mark.asyncio
async def test_pipeline_grade_gate_resume_prefers_repaired_seed_path(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("resume should not regenerate seed")

    stale_seed = _seed(ac=("The CLI should be easy and user-friendly",))
    repaired_seed = _seed()
    seed_path = str(tmp_path / "seed.yaml")
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.seed_artifact = stale_seed.to_dict()
    state.seed_path = seed_path
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.mark_blocked("Seed did not reach A-grade", tool_name="grade_gate")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        store=AutoStore(tmp_path),
        seed_loader=lambda path: (
            repaired_seed if path == seed_path else (_ for _ in ()).throw(AssertionError(path))
        ),
        skip_run=True,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.grade == "A"
    assert state.seed_artifact == repaired_seed.to_dict()


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
async def test_interview_driver_does_not_send_synthetic_gap_answer_to_specific_prompt(
    tmp_path,
) -> None:
    answers: list[str] = []

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What output format should the export command write?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        answers.append(text)
        return InterviewTurn("done", session_id, completed=True)

    state = AutoPipelineState(goal="Build an export command", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert answers
    assert "single local user" not in answers[0].lower()
    assert "non-goals" not in answers[0].lower()


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


@pytest.mark.asyncio
async def test_interview_driver_does_not_replace_specific_verification_answer_with_gap_prompt(
    tmp_path,
) -> None:
    answers: list[str] = []

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        answers.append(text)
        return InterviewTurn("done", session_id, completed=True)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert answers
    assert "observable behavior" in answers[0].lower()
    assert "single local user" not in answers[0].lower()


@pytest.mark.asyncio
async def test_pipeline_recovers_seed_loader_failure_from_review(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("resume should not regenerate seed")

    stale_seed = _seed(ac=("The CLI should be easy and user-friendly",))
    repaired_seed = _seed()
    seed_path = str(tmp_path / "seed.yaml")
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.seed_artifact = stale_seed.to_dict()
    state.seed_path = seed_path
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.mark_failed("seed load failed: transient parse error", tool_name="seed_loader")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        store=AutoStore(tmp_path),
        seed_loader=lambda path: (
            repaired_seed if path == seed_path else (_ for _ in ()).throw(AssertionError(path))
        ),
        skip_run=True,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.grade == "A"
    assert state.seed_artifact == repaired_seed.to_dict()


@pytest.mark.asyncio
async def test_pipeline_run_resume_requires_may_run_even_when_required_grade_is_b(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("run resume should not regenerate seed")

    async def run_seed(seed: Seed) -> dict[str, str | None]:  # noqa: ARG001
        raise AssertionError("B-grade Seed with may_run=false must not start execution")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.required_grade = "B"
    state.last_grade = "B"
    state.seed_artifact = _seed(ac=("The CLI should be easy and user-friendly",)).to_dict()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run prepared")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert "clear the Seed for execution" in (result.blocker or "")


@pytest.mark.asyncio
async def test_pipeline_run_resume_rejects_grade_b_when_required_grade_a(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("run resume should not regenerate seed")

    async def run_seed(seed: Seed) -> dict[str, str | None]:  # noqa: ARG001
        raise AssertionError("grade B must not run when required grade is A")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.required_grade = "A"
    state.last_grade = "B"
    state.seed_artifact = _seed(ac=("The CLI should be easy and user-friendly",)).to_dict()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run prepared")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert "persisted grade" in (result.blocker or "")


@pytest.mark.asyncio
async def test_interview_blocker_does_not_consume_pending_final_round(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should use the persisted pending question")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("blocked auto answer should not reach backend")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_session_id = "interview_1"
    state.pending_question = "Should we use a billing provider for the live account?"
    state.current_round = 1
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=2
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert result.rounds == 1
    assert state.current_round == 1
    assert state.pending_question == "Should we use a billing provider for the live account?"
    persisted = AutoStore(tmp_path).load(state.auto_session_id)
    assert persisted.current_round == 1
    assert persisted.pending_question == state.pending_question


@pytest.mark.asyncio
async def test_interview_resume_backend_error_uses_resume_tool_name(tmp_path) -> None:
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
    assert "interview resume failed" in (result.blocker or "")
    assert state.last_tool_name == "interview.resume"


@pytest.mark.asyncio
async def test_pipeline_seed_loader_rejects_non_seed_on_review_resume(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("review resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("review resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("review resume should not regenerate seed")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.seed_path = str(tmp_path / "seed.yaml")
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        store=AutoStore(tmp_path),
        seed_loader=lambda path: {"path": path},  # type: ignore[return-value]
    )

    result = await pipeline.run(state)

    assert result.status == "failed"
    assert "seed loader returned dict, expected Seed" in (result.blocker or "")
    assert state.last_tool_name == "seed_loader"
