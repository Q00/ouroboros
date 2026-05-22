"""Integration tests for the L2-2 auto pipeline watchdog wiring (#1172).

Pins:

- An ``AutoPipeline`` constructed without a ``watchdog`` parameter
  behaves identically to before the L2-2 integration — pure
  backwards-compatibility guard.
- A wired ``Watchdog`` whose budget has elapsed (relative to
  ``state.created_at``) fires on the first ``run()`` call, transitions
  the session to BLOCKED, and surfaces
  ``stop_reason_code = "watchdog_wall_clock_exceeded"`` on the result
  envelope.
- The watchdog appends exactly one ``runtime.watchdog.cancel`` event.
- A watchdog whose budget has *not* elapsed never fires.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ouroboros.auto.interview_driver import (
    AutoInterviewDriver,
    FunctionInterviewBackend,
    InterviewTurn,
)
from ouroboros.auto.ledger import SeedDraftLedger
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
from ouroboros.runtime.controls import RuntimeControls
from ouroboros.runtime.watchdog import (
    WATCHDOG_AGGREGATE_TYPE,
    WATCHDOG_CANCEL_EVENT_TYPE,
    WATCHDOG_STOP_REASON_CODE,
    Watchdog,
)


def _seed() -> Seed:
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
        metadata=SeedMetadata(seed_id="seed_test_watchdog", ambiguity_score=0.12),
    )


class _CapturingAppender:
    def __init__(self) -> None:
        self.events: list[BaseEvent] = []

    async def append(self, event: BaseEvent) -> None:
        self.events.append(event)


def _fill_ready(ledger: SeedDraftLedger) -> None:
    from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus

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


@pytest.mark.asyncio
async def test_pipeline_without_watchdog_behaves_unchanged(tmp_path) -> None:
    """No ``watchdog`` parameter → no behaviour change. Existing pipelines
    that never set the field keep working."""

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", "interview_1", seed_ready=True, completed=True)

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(_session_id: str) -> Seed:
        return _seed()

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
    )
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        store=AutoStore(tmp_path),
        skip_run=True,
        # no watchdog
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.stop_reason_code is None


@pytest.mark.asyncio
async def test_pipeline_with_watchdog_under_budget_unchanged(tmp_path) -> None:
    """Watchdog wired but budget not exceeded → no firing. Wire the
    fixed ``now`` clock to a moment just inside the budget."""

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", "interview_2", seed_ready=True, completed=True)

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(_session_id: str) -> Seed:
        return _seed()

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()

    appender = _CapturingAppender()
    # ``created_at`` already set to "now" by the state factory; wire
    # the watchdog's clock to just *after* ``created_at`` but well
    # inside the 1h budget.
    started = datetime.fromisoformat(state.created_at)
    watchdog = Watchdog(
        controls=RuntimeControls(session_wall_clock_seconds=3600),
        event_appender=appender,
        now=lambda: started + timedelta(seconds=10),
    )

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
    )
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        store=AutoStore(tmp_path),
        skip_run=True,
        watchdog=watchdog,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.stop_reason_code is None
    assert appender.events == []


@pytest.mark.asyncio
async def test_pipeline_with_watchdog_over_budget_blocks(tmp_path) -> None:
    """Watchdog wired with elapsed budget → fires at ``run()`` entry,
    transitions to BLOCKED, surfaces the typed stop_reason_code on the
    envelope, appends one ``runtime.watchdog.cancel`` event."""

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", "interview_3", seed_ready=True, completed=True)

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(_session_id: str) -> Seed:  # pragma: no cover - watchdog blocks first
        raise AssertionError("seed_generator should not run when watchdog fires at entry")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    started = datetime.fromisoformat(state.created_at)

    appender = _CapturingAppender()
    watchdog = Watchdog(
        controls=RuntimeControls(session_wall_clock_seconds=60),
        event_appender=appender,
        now=lambda: started + timedelta(seconds=120),
    )

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
    )
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        store=AutoStore(tmp_path),
        watchdog=watchdog,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.phase is AutoPhase.BLOCKED
    assert result.stop_reason_code == WATCHDOG_STOP_REASON_CODE
    assert state.last_tool_name == "runtime_watchdog"
    assert "120s" in (state.last_error or "")
    assert "budget 60s" in (state.last_error or "")

    # Exactly one event with the documented shape.
    assert len(appender.events) == 1
    event = appender.events[0]
    assert event.type == WATCHDOG_CANCEL_EVENT_TYPE
    assert event.aggregate_type == WATCHDOG_AGGREGATE_TYPE
    assert event.aggregate_id == state.auto_session_id
    assert event.data["reason"] == "wall_clock_exceeded"


@pytest.mark.asyncio
async def test_pipeline_with_disabled_watchdog_never_fires(tmp_path) -> None:
    """A watchdog with ``session_wall_clock_seconds=0`` is the opt-out
    knob — the pipeline must run normally even when the wall-clock has
    elapsed past any plausible default."""

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", "interview_4", seed_ready=True, completed=True)

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(_session_id: str) -> Seed:
        return _seed()

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()

    appender = _CapturingAppender()
    watchdog = Watchdog(
        controls=RuntimeControls(session_wall_clock_seconds=0),
        event_appender=appender,
        now=lambda: datetime.now(UTC) + timedelta(days=30),
    )

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
    )
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        store=AutoStore(tmp_path),
        skip_run=True,
        watchdog=watchdog,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert appender.events == []
