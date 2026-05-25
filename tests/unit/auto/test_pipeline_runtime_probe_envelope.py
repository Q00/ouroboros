"""Integration tests for L3-2 runtime-probe envelope wiring (#1176).

Pins:

- An ``AutoPipeline`` constructed without a ``probe_runner`` keeps
  the envelope ``runtime_probe_evidence`` empty — backwards-compat.
- A wired ``probe_runner`` is invoked at the COMPLETE transition;
  the returned ``RuntimeEvidence`` tuple surfaces on the envelope.
- A ``probe_runner`` failure or explicit probe FAIL blocks PRODUCT_COMPLETE
  instead of allowing a false complete result.
"""

from __future__ import annotations

from typing import Any

import pytest

from ouroboros.auto.adapters import EnvRuntimeProbeRunner, EvaluateResult
from ouroboros.auto.grading import GradeResult, SeedGrade
from ouroboros.auto.interview_driver import (
    AutoInterviewDriver,
    FunctionInterviewBackend,
    InterviewTurn,
)
from ouroboros.auto.ledger import (
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)
from ouroboros.auto.pipeline import AutoPipeline
from ouroboros.auto.seed_reviewer import SeedReview, SeedReviewer
from ouroboros.auto.state import AutoPipelineState, AutoStore
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.orchestrator.runtime_evidence import RuntimeEvidence


def _seed() -> Seed:
    return Seed(
        goal="Build a CLI",
        constraints=("Use existing project patterns",),
        acceptance_criteria=("`habit list` prints stable stdout containing created habits",),
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
        metadata=SeedMetadata(seed_id="seed_probe_env", ambiguity_score=0.12),
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


class _PassReviewer(SeedReviewer):
    def __init__(self) -> None:
        pass

    def review(self, seed: Seed, *, ledger: Any = None) -> SeedReview:  # noqa: ARG002
        grade = GradeResult(grade=SeedGrade.A, scores={}, findings=[], blockers=[], may_run=True)
        return SeedReview(grade_result=grade, findings=())


async def _ralph_starter_completed(_seed: Seed, **_kwargs: Any) -> dict[str, Any]:
    return {
        "job_id": "job_probe_env",
        "lineage_id": "ralph-probe-env",
        "dispatch_mode": "job",
        "terminal_status": "completed",
        "stop_reason": None,
    }


async def _run_starter_ok(_seed: Seed) -> dict[str, Any]:
    return {
        "job_id": "job_run_pe",
        "session_id": "exec_session_pe",
        "execution_id": "execution_pe",
    }


def _state_at_clean_start(tmp_path) -> AutoPipelineState:
    """Build a fresh state with a seed-ready ledger so the pipeline
    reaches Ralph terminal without going through the repair loop."""
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    return state


@pytest.mark.asyncio
async def test_envelope_empty_without_probe_runner(tmp_path) -> None:
    """No ``probe_runner`` → envelope evidence stays empty (default)."""

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", "interview_pe1", seed_ready=True, completed=True)

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(_session_id: str) -> Seed:
        return _seed()

    state = _state_at_clean_start(tmp_path)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
    )
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        reviewer=_PassReviewer(),
        run_starter=_run_starter_ok,
        ralph_starter=_ralph_starter_completed,
        complete_product=True,
        store=AutoStore(tmp_path),
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.runtime_probe_evidence == ()


@pytest.mark.asyncio
async def test_envelope_carries_probe_evidence_when_runner_wired(tmp_path) -> None:
    """``probe_runner`` returns evidence → envelope carries it
    verbatim. Pipeline invokes runner exactly once per ``run()``."""
    invocations: list[str] = []

    async def probe_runner(state: AutoPipelineState) -> tuple[RuntimeEvidence, ...]:
        invocations.append(state.auto_session_id)
        return (
            RuntimeEvidence(
                probe_kind="headless_run",
                passed=True,
                summary="headless run exit_code=0 (duration 0.02s)",
                duration_seconds=0.02,
                payload={"exit_code": 0, "outcome": "completed"},
            ),
        )

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", "interview_pe2", seed_ready=True, completed=True)

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(_session_id: str) -> Seed:
        return _seed()

    state = _state_at_clean_start(tmp_path)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
    )
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        reviewer=_PassReviewer(),
        run_starter=_run_starter_ok,
        ralph_starter=_ralph_starter_completed,
        complete_product=True,
        store=AutoStore(tmp_path),
        probe_runner=probe_runner,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert len(result.runtime_probe_evidence) == 1
    evidence = result.runtime_probe_evidence[0]
    assert isinstance(evidence, RuntimeEvidence)
    assert evidence.probe_kind == "headless_run"
    assert evidence.passed is True
    # Runner invoked exactly once per ``run()`` for the session.
    assert invocations == [state.auto_session_id]


@pytest.mark.asyncio
async def test_runner_exception_blocks_product_complete(tmp_path) -> None:
    """Runner infrastructure failure must not become false PRODUCT_COMPLETE."""

    async def probe_runner(state: AutoPipelineState) -> tuple[RuntimeEvidence, ...]:  # noqa: ARG001
        raise RuntimeError("probe binary missing — should NOT crash the pipeline")

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", "interview_pe3", seed_ready=True, completed=True)

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(_session_id: str) -> Seed:
        return _seed()

    state = _state_at_clean_start(tmp_path)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
    )
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        reviewer=_PassReviewer(),
        run_starter=_run_starter_ok,
        ralph_starter=_ralph_starter_completed,
        complete_product=True,
        store=AutoStore(tmp_path),
        probe_runner=probe_runner,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert (
        result.blocker
        == "runtime probe runner failed: probe binary missing — should NOT crash the pipeline"
    )
    assert state.last_tool_name == "probe_runner"
    assert result.runtime_probe_evidence == ()


@pytest.mark.asyncio
async def test_failed_probe_blocks_product_complete_and_surfaces_evidence(tmp_path) -> None:
    """Explicit probe FAIL weakens the completion grade to BLOCKED."""

    async def probe_runner(state: AutoPipelineState) -> tuple[RuntimeEvidence, ...]:  # noqa: ARG001
        return (
            RuntimeEvidence(
                probe_kind="headless_run",
                passed=False,
                summary="headless run exit_code=2 (duration 0.01s)",
                duration_seconds=0.01,
                payload={"exit_code": 2, "outcome": "completed"},
            ),
        )

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", "interview_pe4", seed_ready=True, completed=True)

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(_session_id: str) -> Seed:
        return _seed()

    state = _state_at_clean_start(tmp_path)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
    )
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        reviewer=_PassReviewer(),
        run_starter=_run_starter_ok,
        ralph_starter=_ralph_starter_completed,
        complete_product=True,
        store=AutoStore(tmp_path),
        probe_runner=probe_runner,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert result.blocker == "runtime probe failed: headless run exit_code=2 (duration 0.01s)"
    assert state.last_tool_name == "probe_runner"
    assert len(result.runtime_probe_evidence) == 1
    assert result.runtime_probe_evidence[0].passed is False


@pytest.mark.asyncio
async def test_evaluate_complete_path_invokes_probe_runner(tmp_path) -> None:
    """EVALUATE-pass COMPLETE path also carries runtime evidence."""
    invocations: list[str] = []

    async def probe_runner(state: AutoPipelineState) -> tuple[RuntimeEvidence, ...]:
        invocations.append(state.phase.value)
        return (
            RuntimeEvidence(
                probe_kind="headless_run",
                passed=True,
                summary="headless run exit_code=0 (duration 0.01s)",
            ),
        )

    async def evaluator(_seed: Seed, _artifact: str) -> EvaluateResult:
        return EvaluateResult(
            score=0.95,
            verdict="pass",
            passed=True,
            differences=(),
            suggestions=(),
        )

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", "interview_pe5", seed_ready=True, completed=True)

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(_session_id: str) -> Seed:
        return _seed()

    async def ralph_starter(_seed: Seed, **_kwargs: Any) -> dict[str, Any]:
        return {
            "job_id": "job_probe_eval",
            "lineage_id": "ralph-probe-eval",
            "dispatch_mode": "job",
            "terminal_status": "completed",
            "stop_reason": None,
            "result_text": "artifact ready",
        }

    state = _state_at_clean_start(tmp_path)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
    )
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        reviewer=_PassReviewer(),
        run_starter=_run_starter_ok,
        ralph_starter=ralph_starter,
        complete_product=True,
        evaluator=evaluator,
        store=AutoStore(tmp_path),
        probe_runner=probe_runner,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert len(result.runtime_probe_evidence) == 1
    assert invocations == ["evaluate"]


@pytest.mark.asyncio
async def test_completed_resume_preserves_persisted_probe_evidence(tmp_path) -> None:
    """A completed session replay keeps runtime_probe_evidence from state."""

    async def probe_runner(state: AutoPipelineState) -> tuple[RuntimeEvidence, ...]:  # noqa: ARG001
        return (
            RuntimeEvidence(
                probe_kind="headless_run",
                passed=True,
                summary="headless run exit_code=0 (duration 0.01s)",
                payload={"exit_code": 0, "outcome": "completed"},
            ),
        )

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", "interview_pe6", seed_ready=True, completed=True)

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(_session_id: str) -> Seed:
        return _seed()

    store = AutoStore(tmp_path)
    state = _state_at_clean_start(tmp_path)
    pipeline = AutoPipeline(
        AutoInterviewDriver(
            FunctionInterviewBackend(start, answer),
            store=store,
            max_rounds=1,
        ),
        generate_seed,
        reviewer=_PassReviewer(),
        run_starter=_run_starter_ok,
        ralph_starter=_ralph_starter_completed,
        complete_product=True,
        store=store,
        probe_runner=probe_runner,
    )

    first = await pipeline.run(state)
    persisted = store.load(first.auto_session_id)
    assert persisted is not None

    replay = AutoPipeline(
        AutoInterviewDriver(
            FunctionInterviewBackend(start, answer),
            store=store,
            max_rounds=1,
        ),
        generate_seed,
        reviewer=_PassReviewer(),
        run_starter=_run_starter_ok,
        ralph_starter=_ralph_starter_completed,
        complete_product=True,
        store=store,
    )

    second = await replay.run(persisted)

    assert second.status == "complete"
    assert len(second.runtime_probe_evidence) == 1
    assert second.runtime_probe_evidence[0].summary == "headless run exit_code=0 (duration 0.01s)"


@pytest.mark.asyncio
async def test_env_runtime_probe_runner_blocks_public_entrypoint_failures(tmp_path) -> None:
    """Public composition runner executes configured command and returns failing evidence."""
    runner = EnvRuntimeProbeRunner(
        env={
            "OUROBOROS_RUNTIME_PROBE_COMMAND": "python -c 'import sys; sys.exit(7)'",
            "OUROBOROS_RUNTIME_PROBE_TIMEOUT_SECONDS": "5",
        }
    )
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))

    evidence = await runner(state)

    assert len(evidence) == 1
    assert evidence[0].probe_kind == "headless_run"
    assert evidence[0].passed is False
    assert evidence[0].payload["exit_code"] == 7
