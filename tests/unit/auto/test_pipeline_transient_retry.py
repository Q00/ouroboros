"""Vision #1157 — bounded in-process transient retry for one-shot infra
failures at the three LLM-backed tool call sites in ``AutoPipeline``:
``_run_seed_qa_gate`` (seed_qa), ``_run_evaluate`` (evaluator), and
``_run_lateral`` (lateral_thinker).

A single TimeoutError / raised exception / transient ``.error`` result must
no longer end the session BLOCKED outright — it must retry
``_TRANSIENT_TOOL_ATTEMPTS`` times (with a short backoff) before giving up,
since the ``--resume`` path already proves these calls are safely
re-enterable. Only once all attempts are exhausted does the pipeline block,
now with a dedicated ``*_transient_exhausted`` error code.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ouroboros.auto.adapters import EvaluateResult, LateralResult
from ouroboros.auto.grading import GradeResult, SeedGrade
from ouroboros.auto.interview_driver import (
    AutoInterviewDriver,
    AutoInterviewResult,
    FunctionInterviewBackend,
    InterviewTurn,
)
from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus, SeedDraftLedger
import ouroboros.auto.pipeline as pipeline_module
from ouroboros.auto.pipeline import AutoPipeline
from ouroboros.auto.seed_reviewer import SeedReview, SeedReviewer
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.events.base import BaseEvent

pytestmark = pytest.mark.usefixtures("_legacy_unsafe_bank")


@pytest.fixture(autouse=True)
def _zero_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this module exercises the retry loop; keep it instant."""
    monkeypatch.setattr(pipeline_module, "_TRANSIENT_RETRY_BACKOFF_SECONDS", (0.0,))


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


def _build_seed(seed_id: str = "seed_retry_001") -> Seed:
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


class _RecordingEventStore:
    def __init__(self) -> None:
        self.appended: list[BaseEvent] = []

    async def append(self, event: BaseEvent, **_: Any) -> None:
        self.appended.append(event)


def _interview_driver(tmp_path, event_store: _RecordingEventStore | None = None):
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn(
            "done", "interview_retry", seed_ready=True, completed=True, ambiguity_score=0.12
        )

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn(
            "done", session_id, seed_ready=True, completed=True, ambiguity_score=0.12
        )

    return AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
        event_store=event_store,
    )


async def _run_starter_ok(_seed: Seed, **kwargs: Any) -> dict[str, Any]:
    return {
        "job_id": "job_run_001",
        "session_id": "exec_session_001",
        "execution_id": "execution_001",
    }


async def _generate_seed(_session_id: str) -> Seed:
    return _build_seed()


class _PassReviewer(SeedReviewer):
    def __init__(self) -> None:
        pass

    def review(self, seed: Seed, *, ledger: Any = None) -> SeedReview:  # noqa: ARG002
        grade = GradeResult(grade=SeedGrade.A, scores={}, findings=[], blockers=[], may_run=True)
        return SeedReview(grade_result=grade, findings=())


# ---------------------------------------------------------------------------
# Site 1 — ``_run_seed_qa_gate``
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_qa_gate_recovers_after_one_transient_raise(tmp_path) -> None:
    calls = 0

    async def seed_qa(seed: Seed, ledger: SeedDraftLedger) -> EvaluateResult:  # noqa: ARG001
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("upstream QA judge unavailable")
        return EvaluateResult(passed=True, score=0.92, verdict="pass")

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str]:  # noqa: ARG001
        return {"job_id": "job_seed_qa_retry_pass"}

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    pipeline = AutoPipeline(
        _interview_driver(tmp_path),
        _generate_seed,
        run_starter=run_seed,
        store=AutoStore(tmp_path),
        seed_qa_evaluator=seed_qa,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert calls == 2
    assert state.phase is not AutoPhase.BLOCKED
    assert state.last_qa_score == 0.92


@pytest.mark.asyncio
async def test_seed_qa_gate_reports_advisory_after_exhausting_transient_raises(
    tmp_path,
) -> None:
    calls = 0

    async def seed_qa(seed: Seed, ledger: SeedDraftLedger) -> EvaluateResult:  # noqa: ARG001
        nonlocal calls
        calls += 1
        raise RuntimeError("upstream QA judge unavailable")

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str]:  # noqa: ARG001
        return {"job_id": "job_seed_qa_advisory"}

    event_store = _RecordingEventStore()
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    pipeline = AutoPipeline(
        _interview_driver(tmp_path, event_store),
        _generate_seed,
        run_starter=run_seed,
        store=AutoStore(tmp_path),
        seed_qa_evaluator=seed_qa,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert calls == pipeline_module._TRANSIENT_TOOL_ATTEMPTS

    advisory_events = [
        event for event in event_store.appended if event.type == "auto.seed_qa.advisory_override"
    ]
    assert len(advisory_events) == 1
    assert advisory_events[0].data["reason"] == "evaluator_error"
    assert advisory_events[0].data["attempts"] == pipeline_module._TRANSIENT_TOOL_ATTEMPTS


@pytest.mark.asyncio
async def test_seed_qa_gate_recovers_after_one_transient_error_result(tmp_path) -> None:
    calls = 0

    async def seed_qa(seed: Seed, ledger: SeedDraftLedger) -> EvaluateResult:  # noqa: ARG001
        nonlocal calls
        calls += 1
        if calls == 1:
            return EvaluateResult(
                passed=False, score=0.0, verdict="fail", error="QA service unreachable"
            )
        return EvaluateResult(passed=True, score=0.88, verdict="pass")

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str]:  # noqa: ARG001
        return {"job_id": "job_seed_qa_error_then_pass"}

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    pipeline = AutoPipeline(
        _interview_driver(tmp_path),
        _generate_seed,
        run_starter=run_seed,
        store=AutoStore(tmp_path),
        seed_qa_evaluator=seed_qa,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert calls == 2
    assert state.last_qa_score == 0.88


# ---------------------------------------------------------------------------
# Site 2 — ``_run_evaluate``
# ---------------------------------------------------------------------------


class _StubLedger:
    def summary(self) -> dict[str, Any]:
        return {
            "provenance": {},
            "evidence_backed_sections": (),
            "assumption_only_sections": (),
        }

    def assumptions(self) -> list[str]:
        return []

    def assumption_sources(self) -> list[Any]:
        return []

    def non_goals(self) -> list[str]:
        return []


class _StubInterviewDriver:
    def __init__(self) -> None:
        self.progress_callback = None

    async def run(self, state: AutoPipelineState, ledger: Any) -> AutoInterviewResult:
        state.interview_session_id = "interview_stub"
        state.interview_completed = True
        return AutoInterviewResult(
            status="seed_ready", session_id="interview_stub", ledger=ledger, rounds=1
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


@pytest.mark.asyncio
async def test_run_evaluate_recovers_after_one_transient_raise(tmp_path) -> None:
    state = _state_at_run_phase(tmp_path)
    calls = 0

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("evaluator infra hiccup")
        return EvaluateResult(passed=True, score=0.95, verdict="pass")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _generate_seed,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        complete_product=True,
        evaluator=evaluator,
    )

    result = await pipeline._run_evaluate(
        state,
        ledger=_StubLedger(),
        seed=_build_seed(),
        review=None,
        run_subagent=None,
        ralph_result_text="artifact bytes",
        stop_reason="qa passed",
    )

    assert result.status == "complete"
    assert calls == 2
    assert state.last_qa_verdict == "pass"
    assert state.phase is not AutoPhase.BLOCKED


@pytest.mark.asyncio
async def test_run_evaluate_blocks_after_exhausting_transient_raises(tmp_path) -> None:
    state = _state_at_run_phase(tmp_path)
    calls = 0

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        nonlocal calls
        calls += 1
        raise RuntimeError("evaluator infra hiccup")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _generate_seed,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        complete_product=True,
        evaluator=evaluator,
    )

    result = await pipeline._run_evaluate(
        state,
        ledger=_StubLedger(),
        seed=_build_seed(),
        review=None,
        run_subagent=None,
        ralph_result_text="artifact bytes",
        stop_reason="qa failed",
    )

    assert result.status == "blocked"
    assert calls == pipeline_module._TRANSIENT_TOOL_ATTEMPTS
    assert state.last_tool_name == "evaluator"
    assert state.last_error_code == "evaluator_transient_exhausted"
    assert "evaluator infra hiccup" in (state.last_error or "")


# ---------------------------------------------------------------------------
# Site 3 — ``_run_lateral``
# ---------------------------------------------------------------------------


def _stale_recovery_plan() -> dict[str, Any]:
    return {
        "action": "ralph_redispatch",
        "safe_to_redispatch": True,
        "reason": "stale successful lateral advice from an earlier round",
        "qa_score": 0.2,
        "qa_verdict": "fail",
        "differences": ["old failure"],
        "suggestions": ["old suggestion"],
        "persona": "hacker",
        "instruction": "old redispatch instruction",
    }


def _ralph_starter(*, result_text: str = "stdout: ok\nexit_code: 0"):
    async def _starter(seed: Seed, **kwargs: Any) -> dict[str, Any]:
        return {
            "job_id": "job_ralph_001",
            "lineage_id": kwargs["lineage_id"],
            "dispatch_mode": "job",
            "terminal_status": "completed",
            "stop_reason": "qa passed",
            "result_text": result_text,
        }

    return _starter


@pytest.mark.asyncio
async def test_run_lateral_blocks_after_exhausting_timeouts(tmp_path) -> None:
    state = _state_at_run_phase(tmp_path)
    state.timeout_seconds_by_phase[AutoPhase.UNSTUCK_LATERAL.value] = 1
    state.last_recovery_plan = _stale_recovery_plan()
    calls = 0

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        return EvaluateResult(passed=False, score=0.1, verdict="fail", differences=("xx",))

    async def hanging_lateral(**kwargs: Any) -> LateralResult:  # noqa: ARG001
        nonlocal calls
        calls += 1
        await asyncio.sleep(10)
        return LateralResult(persona="hacker", approach_summary="", text="")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _generate_seed,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(),
        complete_product=True,
        evaluator=evaluator,
        lateral_thinker=hanging_lateral,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert calls == pipeline_module._TRANSIENT_TOOL_ATTEMPTS
    assert state.last_tool_name == "lateral_thinker"
    assert state.last_error_code == "lateral_transient_exhausted"
    assert "timed out" in (state.last_error or "")
