"""AutoPipeline REVIEW-phase Seed preflight gate.

A Seed whose contract references fabricated facts (missing claimed scripts,
unbound env vars, unresolvable brownfield paths) must block *before* the
LLM Seed QA judge and before RUN, with open questions and truthful guidance
to revise the Seed and start a replacement session.
"""

from __future__ import annotations

from typing import Any

import pytest

from ouroboros.auto.interview_driver import (
    AutoInterviewDriver,
    FunctionInterviewBackend,
    InterviewTurn,
)
from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus, SeedDraftLedger
from ouroboros.auto.pipeline import AutoPipeline, _recoverable_phase_for_tool
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    BrownfieldContext,
    ContextReference,
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.events.base import BaseEvent

pytestmark = pytest.mark.usefixtures("_legacy_unsafe_bank")


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


def _seed_base() -> dict[str, object]:
    return {
        "goal": "Build a local CLI",
        "constraints": ("Use existing project patterns",),
        "ontology_schema": OntologySchema(
            name="CliTask",
            description="CLI task ontology",
            fields=(OntologyField(name="command", field_type="string", description="Command"),),
        ),
        "evaluation_principles": (
            EvaluationPrinciple(name="testability", description="Observable behavior"),
        ),
        "exit_conditions": (
            ExitCondition(
                name="verified",
                description="Checks pass",
                evaluation_criteria="All acceptance criteria pass",
            ),
        ),
        "metadata": SeedMetadata(ambiguity_score=0.12),
    }


def _fabricated_seed() -> Seed:
    return Seed(
        acceptance_criteria=(
            AcceptanceCriterionSpec(
                description="Readonly report satisfies the checks",
                verify_command='python3 scripts/verify_report.py --vault "$VAULT_PATH"',
            ),
        ),
        brownfield_context=BrownfieldContext(
            project_type="brownfield",
            context_references=(
                ContextReference(path="Obsidian Vault", role="primary", summary="vault"),
            ),
            existing_dependencies=("scripts/verify_report.py",),
        ),
        **_seed_base(),  # type: ignore[arg-type]
    )


def _clean_seed() -> Seed:
    return Seed(
        acceptance_criteria=("`habit list` prints stable stdout",),
        **_seed_base(),  # type: ignore[arg-type]
    )


class _RecordingEventStore:
    def __init__(self) -> None:
        self.appended: list[BaseEvent] = []

    async def append(self, event: BaseEvent, **_: Any) -> None:
        self.appended.append(event)


def _driver(tmp_path, event_store: _RecordingEventStore | None = None) -> AutoInterviewDriver:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn(
            "done",
            "interview_seed_preflight",
            seed_ready=True,
            completed=True,
            ambiguity_score=0.12,
        )

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn(
            "done",
            session_id,
            seed_ready=True,
            completed=True,
            ambiguity_score=0.12,
        )

    return AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
        event_store=event_store,
    )


@pytest.mark.asyncio
async def test_pipeline_blocks_fabricated_seed_before_run(tmp_path) -> None:
    generation_calls = 0

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        nonlocal generation_calls
        generation_calls += 1
        return _fabricated_seed()

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str]:  # noqa: ARG001
        raise AssertionError("run must not start when Seed preflight blocks")

    async def seed_qa(seed: Seed, ledger: SeedDraftLedger):  # noqa: ARG001
        raise AssertionError("Seed QA must not be consulted when preflight blocks")

    event_store = _RecordingEventStore()
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    pipeline = AutoPipeline(
        _driver(tmp_path, event_store),
        generate_seed,
        run_starter=run_seed,
        store=AutoStore(tmp_path),
        seed_qa_evaluator=seed_qa,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.last_tool_name == "seed_preflight"
    assert state.last_error_code == "seed_preflight_unexecutable"
    assert result.stop_reason_code == "seed_preflight_unexecutable"
    assert result.blocker is not None
    # The blocker carries the actual open questions, not a withheld counter.
    assert "Obsidian Vault" in result.blocker
    assert "$VAULT_PATH" in result.blocker
    assert "verify_report.py" in result.blocker
    assert "start a new session" in result.blocker
    assert state.resume_capability().value == "none"

    # The gate announces itself: a preflight event with the open questions AND
    # a generic terminal-block event so attention-driven observers wake up.
    preflight_events = [
        event for event in event_store.appended if event.type == "auto.seed_preflight.blocked"
    ]
    assert len(preflight_events) == 1
    assert preflight_events[0].data["open_questions"]
    session_blocked = [
        event for event in event_store.appended if event.type == "auto.session.blocked"
    ]
    assert len(session_blocked) == 1
    assert session_blocked[0].data["stop_reason_code"] == "seed_preflight_unexecutable"
    assert session_blocked[0].data["tool_name"] == "seed_preflight"
    assert session_blocked[0].data["blocker"] == "Auto session requires operator attention"
    assert "Obsidian Vault" not in session_blocked[0].data["blocker"]

    persisted = AutoStore(tmp_path).load(state.auto_session_id)
    repeated = await pipeline.run(persisted)
    assert repeated.status == "blocked"
    assert repeated.blocker == result.blocker
    assert generation_calls == 1


@pytest.mark.asyncio
async def test_pipeline_clean_seed_passes_preflight_and_runs(tmp_path) -> None:
    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _clean_seed()

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str]:  # noqa: ARG001
        return {"job_id": "job_preflight_clean"}

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    pipeline = AutoPipeline(
        _driver(tmp_path),
        generate_seed,
        run_starter=run_seed,
        store=AutoStore(tmp_path),
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.job_id == "job_preflight_clean"


def test_seed_preflight_block_requires_revised_seed_in_new_session() -> None:
    assert _recoverable_phase_for_tool("seed_preflight") is None


def test_seed_reviewer_timeout_block_resumes_into_review() -> None:
    # Pre-run review timeout blocks with tool_name="seed_reviewer" — a pure
    # transient that must not be a permanent dead end on --resume.
    assert _recoverable_phase_for_tool("seed_reviewer") is AutoPhase.REVIEW
