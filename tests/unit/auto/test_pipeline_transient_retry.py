"""Vision #1157 — bounded in-process transient retry for one-shot infra
failures at the active LLM-backed Seed QA and lateral-repair call sites.

A single TimeoutError / raised exception / transient ``.error`` result must
no longer end the session BLOCKED outright — it must retry
``_TRANSIENT_TOOL_ATTEMPTS`` times (with a short backoff) before giving up,
since the ``--resume`` path already proves these calls are safely
re-enterable. Only once all attempts are exhausted does the pipeline block,
now with a dedicated ``*_transient_exhausted`` error code.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from ouroboros.auto.adapters import EvaluateResult, LateralResult
from ouroboros.auto.interview_driver import (
    AutoInterviewDriver,
    FunctionInterviewBackend,
    InterviewTurn,
)
from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus, SeedDraftLedger
import ouroboros.auto.pipeline as pipeline_module
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


async def _generate_seed(_session_id: str) -> Seed:
    return _build_seed()


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


@pytest.mark.asyncio
async def test_seed_qa_retry_backoff_cannot_outlive_pipeline_deadline(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline_module, "_TRANSIENT_RETRY_BACKOFF_SECONDS", (10.0,))
    calls = 0

    async def seed_qa(seed: Seed, ledger: SeedDraftLedger) -> EvaluateResult:  # noqa: ARG001
        nonlocal calls
        calls += 1
        raise RuntimeError("upstream QA judge unavailable")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.deadline_at = time.monotonic() + 0.03
    state.deadline_at_epoch = time.time() + 0.03
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    pipeline = AutoPipeline(
        _interview_driver(tmp_path),
        _generate_seed,
        store=AutoStore(tmp_path),
        seed_qa_evaluator=seed_qa,
    )

    started = time.monotonic()
    result, _, _ = await pipeline._run_seed_qa_gate(state, ledger, _build_seed(), review=None)

    assert time.monotonic() - started < 1.0
    assert calls == 1
    assert result is not None
    assert result.status == "blocked"
    assert state.last_tool_name == "pipeline_deadline"


@pytest.mark.asyncio
async def test_seed_qa_blocks_unrepairable_ambiguity_without_lowering_score(tmp_path) -> None:
    async def seed_qa(seed: Seed, ledger: SeedDraftLedger) -> EvaluateResult:  # noqa: ARG001
        return EvaluateResult(
            passed=False,
            score=0.5,
            verdict="revise",
            differences=(
                "metadata.ambiguity_score is 0.206, exceeding the required readiness gate of <= 0.20.",
            ),
        )

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    pipeline = AutoPipeline(
        _interview_driver(tmp_path),
        _generate_seed,
        run_starter=lambda *_args, **_kwargs: None,
        store=AutoStore(tmp_path),
        seed_qa_evaluator=seed_qa,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.last_error_code == "seed_qa_ambiguity_unrepairable"
    assert state.seed_artifact["metadata"]["ambiguity_score"] == 0.12


# ---------------------------------------------------------------------------
# Active lateral Seed-repair call site
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_repair_lateral_recovers_after_one_transient_raise(tmp_path) -> None:
    calls = 0

    async def lateral(**kwargs: Any) -> LateralResult:  # noqa: ARG001
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("lateral backend unavailable")
        return LateralResult(
            persona="simplifier",
            approach_summary="Preserve ledger non-goals.",
            text="Represent non-goals as explicit prefixed constraints.",
        )

    pipeline = AutoPipeline(
        _interview_driver(tmp_path),
        _generate_seed,
        lateral_thinker=lateral,
    )
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    qa_result = EvaluateResult(
        passed=False,
        score=0.5,
        verdict="revise",
        differences=("non_goals are missing from executable constraints",),
    )

    repaired = await pipeline._repair_seed_after_qa(state, _build_seed(), qa_result, attempt=1)

    assert calls == 2
    assert any("explicit prefixed constraints" in item for item in repaired.constraints)


@pytest.mark.asyncio
async def test_seed_repair_lateral_falls_back_after_transient_exhaustion(tmp_path) -> None:
    calls = 0

    async def lateral(**kwargs: Any) -> LateralResult:  # noqa: ARG001
        nonlocal calls
        calls += 1
        raise RuntimeError("lateral backend unavailable")

    pipeline = AutoPipeline(
        _interview_driver(tmp_path),
        _generate_seed,
        lateral_thinker=lateral,
    )
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    qa_result = EvaluateResult(
        passed=False,
        score=0.5,
        verdict="revise",
        differences=("non_goals are missing from executable constraints",),
    )

    repaired = await pipeline._repair_seed_after_qa(state, _build_seed(), qa_result, attempt=1)

    assert calls == pipeline_module._TRANSIENT_TOOL_ATTEMPTS
    assert any("Non-goal:" in item for item in repaired.constraints)


@pytest.mark.asyncio
async def test_lateral_retry_backoff_cannot_outlive_pipeline_deadline(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del monkeypatch
    calls = 0

    async def lateral(**kwargs: Any) -> LateralResult:  # noqa: ARG001
        nonlocal calls
        calls += 1
        state.deadline_at = time.monotonic() - 1
        state.deadline_at_epoch = time.time() - 1
        raise RuntimeError("lateral backend unavailable")

    async def seed_qa(seed: Seed, ledger: SeedDraftLedger) -> EvaluateResult:  # noqa: ARG001
        return EvaluateResult(
            passed=False,
            score=0.5,
            verdict="revise",
            differences=("non_goals are missing from executable constraints",),
        )

    run_calls = 0

    async def forbidden_run(seed: Seed, *, idempotency_key: str = "") -> dict[str, str]:
        del seed, idempotency_key
        nonlocal run_calls
        run_calls += 1
        return {"job_id": "must-not-run"}

    pipeline = AutoPipeline(
        _interview_driver(tmp_path),
        _generate_seed,
        lateral_thinker=lateral,
        store=AutoStore(tmp_path),
        seed_qa_evaluator=seed_qa,
        run_starter=forbidden_run,
    )
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.deadline_at = time.monotonic() + 60
    state.deadline_at_epoch = time.time() + 60
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()

    result = await pipeline.run(state)

    assert calls == 1
    assert result.status == "blocked"
    assert state.last_tool_name == "pipeline_deadline"
    assert state.phase is AutoPhase.BLOCKED
    assert state.run_start_attempted is False
    assert run_calls == 0
