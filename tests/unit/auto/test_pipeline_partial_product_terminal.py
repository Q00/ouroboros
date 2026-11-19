"""Tests for the degraded-seed → partial product terminal (#1257 PR-C).

PR-C builds on PR-A's degraded substrate and PR-B's deadline routing:
when ``seed.metadata.degraded`` is True, the pipeline must

1. stop BLOCKED with ``degraded_seed_safety_blockers`` while a required
   ledger section is ``BLOCKED`` (an answer only a human can give); the
   grade gate's other findings were removed with it (#3064),
2. run the Seed like any auto Seed when it carries the user's goal on record
   (``sourced_scope``): the worker sees only that goal, and the unresolved
   slots stay recorded. Without it, emit ``auto.product.partial_emitted``
   and stop ``BLOCKED`` with ``degraded_seed_not_executed``: that Seed is
   never executed, so the session never reports complete, and
3. surface ``partial_product`` / ``partial_product_reason`` /
   ``partial_unresolved_slots`` on ``AutoPipelineResult`` so MCP/CLI
   consumers can render the next-step hints without having to parse the
   Seed artifact themselves.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from ouroboros.auto.degraded_seed import (
    DEGRADED_SEED_NOT_EXECUTED_STOP_REASON_CODE,
    DEGRADED_SEED_SAFETY_BLOCKERS_STOP_REASON_CODE,
    degraded_seed_runs_as_stated,
    degraded_seed_safety_blocker,
)
from ouroboros.auto.ledger import (
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)
from ouroboros.auto.ledger_seed import partial_seed_from_evidence, with_ledger_carryover
from ouroboros.auto.pipeline import AutoPipeline
from ouroboros.auto.state import (
    AutoPhase,
    AutoPipelineState,
    AutoStore,
)
from ouroboros.core.seed import Seed, ac_texts
from ouroboros.core.seed_contract import GOAL_OBJECTIVE_CRITERION
from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.runner import build_system_prompt, build_task_prompt


class _RecordingEventStore:
    def __init__(self) -> None:
        self.appended: list[BaseEvent] = []

    async def append(self, event: BaseEvent, **_: Any) -> None:
        self.appended.append(event)


class _DeadlineDriver:
    """Mimics the production driver surface used by the pipeline."""

    progress_callback = None

    def __init__(self, event_store: _RecordingEventStore | None = None) -> None:
        self.event_store = event_store
        self._pending_emit_tasks: set[asyncio.Task[None]] = set()

    async def wait_for_pending_emits(self) -> None:
        return None

    async def run(self, _state, _ledger):  # noqa: ARG002
        await asyncio.sleep(3600)
        raise AssertionError("must be cancelled by phase timeout")


async def _no_seed_generator(_session_id: str):  # pragma: no cover - never called
    raise AssertionError("seed generator must not run on the deadline path")


def _build_deadline_state(tmp_path) -> AutoPipelineState:
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.timeout_seconds_by_phase[AutoPhase.INTERVIEW.value] = 1
    state.deadline_at = time.monotonic() + 3600
    state.deadline_at_epoch = time.time() + 3600
    state.transition(AutoPhase.INTERVIEW, "starting interview")
    return state


class TestDegradedSeedRules:
    """The deadline closure rules a degraded Seed meets before RUN."""

    def test_only_a_degraded_seed_with_a_sourced_goal_runs(self) -> None:
        ledger = SeedDraftLedger.from_goal("Goal only.")
        partial = partial_seed_from_evidence(ledger, reason="interview_phase_deadline")
        assert partial.sourced_scope is None
        assert degraded_seed_runs_as_stated(partial) is False

        sourced = with_ledger_carryover(partial, ledger)
        assert sourced.sourced_scope is not None
        assert degraded_seed_runs_as_stated(sourced) is True

        not_degraded = sourced.model_copy(
            update={"metadata": sourced.metadata.model_copy(update={"degraded": False})}
        )
        assert degraded_seed_runs_as_stated(not_degraded) is False

    def test_safety_blockers_still_stop_a_degraded_seed_that_runs(self) -> None:
        ledger = SeedDraftLedger.from_goal("Goal only.")
        ledger.add_entry(
            "constraints",
            LedgerEntry(
                key="constraints.human_required",
                value="Human must confirm the production credential rotation.",
                source=LedgerSource.BLOCKER,
                confidence=0.0,
                status=LedgerStatus.BLOCKED,
            ),
        )
        terminal = degraded_seed_safety_blocker(ledger)
        assert terminal is not None
        assert "constraints" in terminal[0]
        assert terminal[1] == DEGRADED_SEED_SAFETY_BLOCKERS_STOP_REASON_CODE

    def test_unanswered_sections_alone_do_not_stop_a_degraded_seed(self) -> None:
        # MISSING/WEAK sections are the deadline's unresolved slots, not a stop.
        assert degraded_seed_safety_blocker(SeedDraftLedger.from_goal("Goal only.")) is None


# ---------------------------------------------------------------------------
# Pipeline end-to-end — partial product terminal
# ---------------------------------------------------------------------------


class _RecordingRunStarter:
    """Run starter stub that records the Seed it was asked to execute."""

    def __init__(self) -> None:
        self.seeds: list[Seed] = []

    async def __call__(self, seed: Seed, **_: Any) -> dict[str, str]:
        self.seeds.append(seed)
        return {"job_id": "job_1", "execution_id": "exec_1", "session_id": "session_1"}


async def _forbidden_run_starter(_seed: Seed, **_: Any) -> dict[str, str]:
    raise AssertionError("a degraded Seed that must stop was handed to RUN")


class _InferredGoalDriver(_DeadlineDriver):
    """Leaves the ledger without the user's goal on record before the deadline.

    The goal section holds only a model-inferred entry, so the recorded Seed
    gets no ``sourced_scope``: there is no sourced goal to run.
    """

    async def run(self, _state, ledger: SeedDraftLedger):  # noqa: ARG002
        ledger.sections["goal"].entries = [
            LedgerEntry(
                key="goal.inferred",
                value="Build a CLI",
                source=LedgerSource.INFERENCE,
                confidence=0.6,
                status=LedgerStatus.CONFIRMED,
            )
        ]
        await asyncio.sleep(3600)
        raise AssertionError("must be cancelled by phase timeout")


@pytest.mark.asyncio
async def test_deadline_seed_with_sourced_goal_runs_on_the_request_as_stated(tmp_path) -> None:
    """Nobody answered the open slots: the degraded Seed runs on the user's goal.

    django-15863 (Ambig-SWE, v0.59.4 round 2) stopped here unexecuted while the
    plain agent given the same issue resolved it. The worker is told only the
    sourced goal, so the unresolved slots are never turned into answers; they
    stay in the Seed, the result and the deadline event.
    """
    event_store = _RecordingEventStore()
    driver = _DeadlineDriver(event_store=event_store)
    state = _build_deadline_state(tmp_path)
    run_starter = _RecordingRunStarter()
    pipeline = AutoPipeline(
        driver, _no_seed_generator, store=AutoStore(tmp_path), run_starter=run_starter
    )

    result = await pipeline.run(state)

    assert len(run_starter.seeds) == 1
    assert state.phase == AutoPhase.COMPLETE
    assert result.status == "complete"
    assert result.stop_reason_code is None
    assert result.job_id == "job_1"
    seed = run_starter.seeds[0]
    assert seed.metadata.degraded is True
    assert seed.sourced_scope is not None
    assert seed.sourced_scope.goal == "Build a CLI"
    assert seed.metadata.unresolved_slots

    # The unresolved slots stay recorded, never resolved.
    assert result.partial_product is False
    assert result.partial_product_reason == "interview_phase_deadline"
    assert result.partial_unresolved_slots == seed.metadata.unresolved_slots
    deadline_events = [
        event for event in event_store.appended if event.type == "runtime.deadline.interview.fired"
    ]
    assert len(deadline_events) == 1
    assert deadline_events[0].data["open_gaps"] == list(seed.metadata.unresolved_slots)
    assert not [
        event for event in event_store.appended if event.type == "auto.product.partial_emitted"
    ]

    # The worker sees the goal as stated, not the smoke criterion or the
    # unresolved-slot constraints the Seed keeps for the record.
    task_prompt = build_task_prompt(seed)
    assert GOAL_OBJECTIVE_CRITERION in task_prompt
    for prompt in (build_system_prompt(seed), task_prompt):
        assert "Build a CLI" in prompt
        for criterion in ac_texts(seed.acceptance_criteria):
            assert criterion not in prompt
        for constraint in seed.constraints:
            assert constraint not in prompt


def _run_resume_state(tmp_path, ledger: SeedDraftLedger) -> AutoPipelineState:
    """A session persisted in RUN with a degraded sourced Seed and no run handle."""
    seed = with_ledger_carryover(
        partial_seed_from_evidence(ledger, reason="interview_phase_deadline"), ledger
    )
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.seed_artifact = seed.to_dict()
    state.ledger = ledger.to_dict()
    state.interview_completed = True
    for phase in (AutoPhase.INTERVIEW, AutoPhase.SEED_GENERATION):
        state.transition(phase, phase.value)
    state.transition(AutoPhase.RUN, "run prepared")
    return state


@pytest.mark.asyncio
async def test_run_resume_starts_a_degraded_seed_with_sourced_goal(tmp_path) -> None:
    """A resumed RUN starts the degraded sourced Seed; nothing re-reviews it."""
    state = _run_resume_state(tmp_path, SeedDraftLedger.from_goal("Build a CLI"))
    run_starter = _RecordingRunStarter()
    pipeline = AutoPipeline(
        _DeadlineDriver(), _no_seed_generator, store=AutoStore(tmp_path), run_starter=run_starter
    )

    result = await pipeline.run(state)

    assert len(run_starter.seeds) == 1
    assert result.status == "complete"
    assert result.stop_reason_code is None


@pytest.mark.asyncio
async def test_deadline_seed_without_sourced_goal_stops_as_partial_product(tmp_path) -> None:
    """Without the user's goal on record, the degraded Seed is not executed.

    It stops BLOCKED with ``degraded_seed_not_executed``, never COMPLETE: a
    session that ran nothing must not claim a product (matplotlib-26113).
    """
    event_store = _RecordingEventStore()
    driver = _InferredGoalDriver(event_store=event_store)
    state = _build_deadline_state(tmp_path)
    pipeline = AutoPipeline(
        driver,
        _no_seed_generator,
        store=AutoStore(tmp_path),
        run_starter=_forbidden_run_starter,
    )

    result = await pipeline.run(state)
    await asyncio.sleep(0)  # flush the fire-and-forget event-append tasks

    # 1. Terminal is a typed BLOCKED stop, never COMPLETE without execution.
    assert state.seed_artifact is not None
    assert "sourced_scope" not in state.seed_artifact
    assert state.phase == AutoPhase.BLOCKED
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.stop_reason_code == DEGRADED_SEED_NOT_EXECUTED_STOP_REASON_CODE
    assert result.artifact_state != "complete_unverified"

    # 2. Partial-product fields populated on the result envelope.
    assert result.partial_product is True
    assert result.partial_product_reason == "interview_phase_deadline"
    assert result.partial_unresolved_slots, (
        "result must surface unresolved slots so MCP/CLI consumers can render "
        "next-step hints without parsing the Seed artifact"
    )

    # 3. ``auto.product.partial_emitted`` event landed with the contract payload.
    partial_events = [
        event for event in event_store.appended if event.type == "auto.product.partial_emitted"
    ]
    assert len(partial_events) == 1
    payload = partial_events[0].data
    assert payload["auto_session_id"] == state.auto_session_id
    assert payload["recovery_reason"] == "interview_phase_deadline"
    assert payload["unresolved_slots"]
    assert payload["seed_id"] == state.seed_id


@pytest.mark.asyncio
async def test_degraded_seed_with_risky_assumption_runs_on_the_sourced_goal(tmp_path) -> None:
    """The grade gate's keyword rule on assumptions is gone (#3064).

    A ledger assumption that mentions production credentials no longer stops a
    degraded Seed that carries the user's goal: the worker is told only that
    goal, so the assumption never reaches it.
    """
    event_store = _RecordingEventStore()

    class _UnsafeLedgerDriver(_DeadlineDriver):
        async def run(self, _state, ledger: SeedDraftLedger):  # noqa: ARG002
            # Mark a high-risk assumption explicitly so the grade gate fires
            # ``high_risk_assumptions`` (a safety blocker that survives PR-C).
            ledger.add_entry(
                "constraints",
                LedgerEntry(
                    key="risk.production_credentials",
                    value="Deploys to production and rotates live credentials.",
                    source=LedgerSource.ASSUMPTION,
                    confidence=0.9,
                    status=LedgerStatus.CONFIRMED,
                    rationale="Operator-supplied unsafe action without a confirmed reversal plan.",
                ),
            )
            await asyncio.sleep(3600)
            raise AssertionError("must be cancelled by phase timeout")

    driver = _UnsafeLedgerDriver(event_store=event_store)
    state = _build_deadline_state(tmp_path)
    run_starter = _RecordingRunStarter()
    pipeline = AutoPipeline(
        driver, _no_seed_generator, store=AutoStore(tmp_path), run_starter=run_starter
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.stop_reason_code is None
    assert len(run_starter.seeds) == 1
    seed = run_starter.seeds[0]
    for prompt in (build_system_prompt(seed), build_task_prompt(seed)):
        assert "rotates live credentials" not in prompt


@pytest.mark.asyncio
async def test_deadline_path_with_blocked_ledger_section_terminates_blocked(
    tmp_path,
) -> None:
    """Regression for the PR-C BLOCKED-gap blocker (ouroboros-agent[bot]
    ``req_1779969257_174``).

    End-to-end: if the interview driver records a ``LedgerStatus.BLOCKED``
    entry on a required section before the phase deadline fires, the
    resulting degraded Seed MUST terminate BLOCKED (or otherwise NOT route
    to the partial-product success terminal). Demoting BLOCKED to a
    finding would convert a human-required confirmation gap into a
    silently "successful" partial product.
    """
    event_store = _RecordingEventStore()

    class _BlockedLedgerDriver(_DeadlineDriver):
        async def run(self, _state, ledger: SeedDraftLedger):  # noqa: ARG002
            ledger.add_entry(
                "constraints",
                LedgerEntry(
                    key="constraints.human_required",
                    value=(
                        "Human must confirm whether rotation of live "
                        "production credentials is permitted."
                    ),
                    source=LedgerSource.BLOCKER,
                    confidence=0.0,
                    status=LedgerStatus.BLOCKED,
                    rationale=("Auto-answer escalated to human; awaiting confirmation."),
                ),
            )
            await asyncio.sleep(3600)
            raise AssertionError("must be cancelled by phase timeout")

    driver = _BlockedLedgerDriver(event_store=event_store)
    state = _build_deadline_state(tmp_path)
    pipeline = AutoPipeline(
        driver,
        _no_seed_generator,
        store=AutoStore(tmp_path),
        run_starter=_forbidden_run_starter,
    )

    result = await pipeline.run(state)
    await asyncio.sleep(0)

    # The user's goal is on record, so this is the run path: the BLOCKED
    # section still stops it before RUN with the typed safety code.
    assert state.seed_artifact is not None
    assert state.seed_artifact["sourced_scope"]["goal"] == "Build a CLI"
    assert result.stop_reason_code == DEGRADED_SEED_SAFETY_BLOCKERS_STOP_REASON_CODE

    # MUST NOT route to the partial-product success terminal.
    assert result.partial_product is False, (
        "A degraded seed with a BLOCKED required section must NOT reach the "
        "partial-product success terminal — BLOCKED is the ledger's explicit "
        "human-required signal."
    )
    partial_events = [
        event for event in event_store.appended if event.type == "auto.product.partial_emitted"
    ]
    assert partial_events == [], (
        "auto.product.partial_emitted must not fire when a BLOCKED required "
        "ledger section is unresolved"
    )
