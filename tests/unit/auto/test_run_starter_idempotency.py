"""Auto-pipeline bounded-retry contract for unknown run handoffs.

Regression suite for Q00/ouroboros#774. Each scenario exercises the
inline retry that ``AutoPipeline.run`` performs when the run starter
either times out or returns no durable tracking handle. The retry uses
the same ``idempotency_key`` (``state.auto_session_id``) so the
server-side handler can short-circuit a duplicate enqueue.
"""

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


def _primed_run_state(tmp_path) -> AutoPipelineState:
    """Build an auto state already at the RUN phase with a passing seed."""
    state = AutoPipelineState(goal="Build a local CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.seed_artifact = _seed().to_dict()
    state.last_grade = "A"
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    return state


async def _unused_start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
    raise AssertionError("run-phase resume must not restart the interview")


async def _unused_answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
    raise AssertionError("run-phase resume must not answer the interview")


async def _unused_seed_generator(_session_id: str) -> Seed:
    raise AssertionError("run-phase resume must not regenerate the Seed")


@pytest.mark.asyncio
async def test_first_call_times_out_retry_returns_handle_completes(tmp_path) -> None:
    """Scenario 1: first call raises TimeoutError; retry returns a handle -> COMPLETE."""

    calls: list[str] = []

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str | None]:  # noqa: ARG001
        calls.append(idempotency_key)
        if len(calls) == 1:
            await asyncio.sleep(1.0)  # forces wait_for to TimeoutError
            raise AssertionError("first call should have been cancelled by wait_for")
        return {"job_id": "job_after_retry", "execution_id": "exec_after_retry"}

    state = _primed_run_state(tmp_path)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(_unused_start, _unused_answer),
        store=AutoStore(tmp_path),
    )
    pipeline = AutoPipeline(
        driver,
        _unused_seed_generator,
        run_starter=run_seed,
        store=AutoStore(tmp_path),
        run_start_timeout_seconds=0.01,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.execution_id == "exec_after_retry"
    assert result.job_id == "job_after_retry"
    # Retry MUST reuse the same idempotency key on both attempts.
    assert calls == [state.auto_session_id, state.auto_session_id]


@pytest.mark.asyncio
async def test_first_call_returns_no_handle_retry_returns_handle_completes(tmp_path) -> None:
    """Scenario 2: first call returns ``{}``; retry returns a handle -> COMPLETE."""

    calls: list[str] = []

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str | None]:  # noqa: ARG001
        calls.append(idempotency_key)
        if len(calls) == 1:
            return {}
        return {"job_id": "job_after_retry", "execution_id": "exec_after_retry"}

    state = _primed_run_state(tmp_path)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(_unused_start, _unused_answer),
        store=AutoStore(tmp_path),
    )
    pipeline = AutoPipeline(
        driver,
        _unused_seed_generator,
        run_starter=run_seed,
        store=AutoStore(tmp_path),
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.execution_id == "exec_after_retry"
    assert calls == [state.auto_session_id, state.auto_session_id]


@pytest.mark.asyncio
async def test_both_calls_fail_pipeline_blocks_with_documented_phrase(tmp_path) -> None:
    """Scenario 3: both attempts fail -> BLOCKED with the documented retry phrase."""

    calls: list[str] = []

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str | None]:  # noqa: ARG001
        calls.append(idempotency_key)
        return {}

    state = _primed_run_state(tmp_path)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(_unused_start, _unused_answer),
        store=AutoStore(tmp_path),
    )
    pipeline = AutoPipeline(
        driver,
        _unused_seed_generator,
        run_starter=run_seed,
        store=AutoStore(tmp_path),
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.last_tool_name == "run_starter"
    assert "retried once with idempotency key" in (state.last_error or "")
    assert "retried once with idempotency key" in (result.blocker or "")
    assert calls == [state.auto_session_id, state.auto_session_id]
