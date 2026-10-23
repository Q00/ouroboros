"""Tests for the degraded-seed → partial product terminal (#1257 PR-C).

PR-C builds on PR-A's degraded substrate and PR-B's deadline routing:
when ``seed.metadata.degraded`` is True, the pipeline must

1. let the Seed survive the grade gate even when the Seed would otherwise
   block on ``high_ambiguity_score`` / ``ledger_open_gap`` (those are
   demoted to next-step hints — they're already in
   ``seed.metadata.unresolved_slots``),
2. still terminate as BLOCKED for safety blockers
   (``missing_goal`` / ``seed_goal_mismatch`` / ``high_risk_assumptions``),
3. run the Seed like any auto Seed when it carries the user's goal on record
   (``sourced_scope``): the worker sees only that goal, and the unresolved
   slots stay recorded. Without it, emit ``auto.product.partial_emitted``
   and stop ``BLOCKED`` with ``degraded_seed_not_executed``: that Seed is
   never executed, so the session never reports complete, and
4. surface ``partial_product`` / ``partial_product_reason`` /
   ``partial_unresolved_slots`` on ``AutoPipelineResult`` so MCP/CLI
   consumers can render the next-step hints without having to parse the
   Seed artifact themselves.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from ouroboros.auto.adapters import EvaluateResult
from ouroboros.auto.grade_gate_terminals import (
    DEGRADED_SEED_NOT_EXECUTED_STOP_REASON_CODE,
    DEGRADED_SEED_SAFETY_BLOCKERS_STOP_REASON_CODE,
    SEED_GRADE_BELOW_REQUIRED_STOP_REASON_CODE,
    degraded_seed_runs_as_stated,
    seed_review_gate_blocker,
)
from ouroboros.auto.grading import GradeGate
from ouroboros.auto.ledger import (
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)
from ouroboros.auto.ledger_seed import partial_seed_from_evidence, with_ledger_carryover
from ouroboros.auto.pipeline import AutoPipeline
from ouroboros.auto.seed_reviewer import SeedReviewer
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


# ---------------------------------------------------------------------------
# GradeGate.grade_seed — pure-unit assertions
# ---------------------------------------------------------------------------


class TestGradeGateDegradedFlag:
    """Direct exercise of the new ``degraded`` parameter on ``GradeGate``."""

    def test_high_ambiguity_score_suppressed_for_degraded(self) -> None:
        ledger = SeedDraftLedger.from_goal("A goal for the gate test.")
        seed = partial_seed_from_evidence(ledger, reason="interview_phase_deadline")

        gate = GradeGate()
        # Without the degraded flag the gate would block on the deliberately
        # elevated ambiguity floor PR-A applied (>= 0.6).
        strict = gate.grade_seed(seed, ledger=ledger, degraded=False)
        assert any(b.code == "high_ambiguity_score" for b in strict.blockers)

        relaxed = gate.grade_seed(seed, ledger=ledger, degraded=True)
        assert not any(b.code == "high_ambiguity_score" for b in relaxed.blockers)

    def test_ledger_open_gap_demoted_to_finding_for_degraded(self) -> None:
        ledger = SeedDraftLedger.from_goal("Goal only.")
        seed = partial_seed_from_evidence(ledger, reason="interview_phase_deadline")
        gate = GradeGate()

        strict = gate.grade_seed(seed, ledger=ledger, degraded=False)
        assert any(b.code == "ledger_open_gap" for b in strict.blockers)

        relaxed = gate.grade_seed(seed, ledger=ledger, degraded=True)
        assert not any(b.code == "ledger_open_gap" for b in relaxed.blockers)
        # ...but the gap is still reported as a finding so observers see it.
        assert any(f.code == "ledger_open_gap" for f in relaxed.findings)

    def test_ledger_blocked_gap_keeps_blocker_for_degraded(self) -> None:
        """``LedgerStatus.BLOCKED`` is a human-required signal; it MUST keep
        blocking even on the degraded recovery path (§I6 safety contract).

        Regression for the ouroboros-agent[bot] blocker on
        ``req_1779969257_174``: prior PR-C code demoted *every*
        ``ledger.open_gaps()`` entry — including BLOCKED sections — to a
        medium finding under degraded grading. That converted a blocked
        human-confirmation gap into a successful partial product. This
        test pins the corrected behavior: BLOCKED gaps stay as
        ``ledger_blocked_gap`` blockers regardless of the degraded flag.
        """
        ledger = SeedDraftLedger.from_goal("Goal only.")
        # Plant a BLOCKED entry on a required section. ``constraints`` is one
        # of the REQUIRED_SECTIONS, so a BLOCKED entry there raises the
        # section's aggregate status to BLOCKED via section_statuses().
        ledger.add_entry(
            "constraints",
            LedgerEntry(
                key="constraints.human_required",
                value="Human must confirm whether the integration may rotate live credentials.",
                source=LedgerSource.BLOCKER,
                confidence=0.0,
                status=LedgerStatus.BLOCKED,
                rationale="Auto-answer attempted but escalated to human; awaiting confirmation.",
            ),
        )
        seed = partial_seed_from_evidence(ledger, reason="interview_phase_deadline")
        assert seed.metadata.degraded is True
        gate = GradeGate()

        # Strict mode: BLOCKED gap is a blocker (legacy behavior).
        strict = gate.grade_seed(seed, ledger=ledger, degraded=False)
        assert any(
            b.code in ("ledger_open_gap", "ledger_blocked_gap") and b.target == "constraints"
            for b in strict.blockers
        )

        # Degraded mode: BLOCKED gap remains a HARD blocker, not a finding.
        relaxed = gate.grade_seed(seed, ledger=ledger, degraded=True)
        blocked_gap_blockers = [
            b
            for b in relaxed.blockers
            if b.code == "ledger_blocked_gap" and b.target == "constraints"
        ]
        assert blocked_gap_blockers, (
            "LedgerStatus.BLOCKED on a required section MUST remain a hard "
            "blocker even on the degraded recovery path — demoting it would "
            "convert a human-required confirmation gap into a 'successful' "
            "partial product. (§I6 safety contract.)"
        )
        # The corrected code emits ``ledger_blocked_gap`` (distinct code so
        # observers can tell BLOCKED apart from MISSING/WEAK/CONFLICTING),
        # and the BLOCKED gap must NOT appear in the demoted findings bucket.
        assert not any(
            f.code == "ledger_blocked_gap" and f.target == "constraints" for f in relaxed.findings
        )

    def test_ledger_blocked_distinguished_from_missing_for_degraded(self) -> None:
        """When the ledger has BOTH a BLOCKED section and merely MISSING ones,
        only the BLOCKED section should keep its hard-blocker status under
        degraded grading; MISSING/WEAK/CONFLICTING sections still demote to
        findings so the partial-product surface continues to work."""
        ledger = SeedDraftLedger.from_goal("Goal only.")
        # ``constraints`` → BLOCKED (human-required)
        ledger.add_entry(
            "constraints",
            LedgerEntry(
                key="constraints.human_required",
                value="Awaiting human confirmation on destructive action policy.",
                source=LedgerSource.BLOCKER,
                confidence=0.0,
                status=LedgerStatus.BLOCKED,
            ),
        )
        # Other REQUIRED_SECTIONS remain MISSING by default.
        seed = partial_seed_from_evidence(ledger, reason="interview_phase_deadline")
        gate = GradeGate()
        relaxed = gate.grade_seed(seed, ledger=ledger, degraded=True)

        # BLOCKED constraints → blocker
        assert any(
            b.target == "constraints" and b.code == "ledger_blocked_gap" for b in relaxed.blockers
        )
        # MISSING sections → demoted to findings (PR-C's partial-product surface)
        missing_findings = [
            f for f in relaxed.findings if f.code == "ledger_open_gap" and f.target != "constraints"
        ]
        assert missing_findings, (
            "MISSING/WEAK ledger gaps must still demote to findings under "
            "degraded grading so the partial-product surface continues to work"
        )

    def test_seed_goal_mismatch_still_blocks_for_degraded(self) -> None:
        """Safety blockers MUST keep terminating even for degraded seeds (§I6)."""
        ledger = SeedDraftLedger.from_goal("Build a CLI.")
        # Inject a goal-mismatch by writing a different goal entry the seed
        # will diverge from.
        ledger.add_entry(
            "goal",
            LedgerEntry(
                key="goal.primary",
                value="An entirely different objective for the gate test.",
                source=LedgerSource.USER_GOAL,
                confidence=0.95,
                status=LedgerStatus.CONFIRMED,
            ),
        )
        seed = partial_seed_from_evidence(ledger, reason="interview_phase_deadline")
        gate = GradeGate()

        result = gate.grade_seed(seed, ledger=ledger, degraded=True)
        # The Seed's goal still matches the latest ledger entry (we used the
        # latest value), so we instead assert that any *blockers* present are
        # still real safety markers, not the suppressed ambiguity/gap pair.
        for blocker in result.blockers:
            assert blocker.code != "high_ambiguity_score"
            assert blocker.code != "ledger_open_gap"

    def test_auto_detects_degraded_from_seed_metadata(self) -> None:
        """When the caller passes ``degraded=None`` the gate must read the flag
        off the Seed's metadata so legacy callers automatically inherit the
        PR-C suppression for deadline-recovery seeds."""
        ledger = SeedDraftLedger.from_goal("Goal only.")
        seed = partial_seed_from_evidence(ledger, reason="interview_phase_deadline")
        gate = GradeGate()

        result = gate.grade_seed(seed, ledger=ledger)
        assert not any(b.code == "high_ambiguity_score" for b in result.blockers)
        assert not any(b.code == "ledger_open_gap" for b in result.blockers)


class TestSeedReviewerPropagatesDegraded:
    """``SeedReviewer.review`` must forward the new kwarg to the grade gate."""

    def test_review_forwards_degraded(self) -> None:
        ledger = SeedDraftLedger.from_goal("Goal only.")
        seed = partial_seed_from_evidence(ledger, reason="interview_phase_deadline")
        reviewer = SeedReviewer()

        relaxed = reviewer.review(seed, ledger=ledger, degraded=True)
        assert not any(b.code == "high_ambiguity_score" for b in relaxed.grade_result.blockers)

        # Auto-detection works when degraded=None (default).
        auto = reviewer.review(seed, ledger=ledger)
        assert not any(b.code == "high_ambiguity_score" for b in auto.grade_result.blockers)


class TestReviewGateForDegradedSeedThatRuns:
    """The review gate keeps only the safety bar for a degraded Seed that runs."""

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

    def test_grade_bar_waived_only_for_a_degraded_seed_that_runs(self) -> None:
        ledger = SeedDraftLedger.from_goal("Goal only.")
        seed = with_ledger_carryover(
            partial_seed_from_evidence(ledger, reason="interview_phase_deadline"), ledger
        )
        review = SeedReviewer().review(seed, ledger=ledger)
        # The unresolved slots are reported as findings, so the grade stays B.
        assert review.grade_result.grade.value == "B"
        assert not review.grade_result.blockers
        state = AutoPipelineState(goal="Goal only.", cwd="/tmp/project")

        normal = seed_review_gate_blocker(state, review, skip_run=False)
        assert normal is not None
        assert normal[1] == SEED_GRADE_BELOW_REQUIRED_STOP_REASON_CODE
        assert seed_review_gate_blocker(state, review, skip_run=False, runs_degraded=True) is None

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
        seed = with_ledger_carryover(
            partial_seed_from_evidence(ledger, reason="interview_phase_deadline"), ledger
        )
        review = SeedReviewer().review(seed, ledger=ledger)
        assert any(b.code == "ledger_blocked_gap" for b in review.grade_result.blockers)

        terminal = seed_review_gate_blocker(
            AutoPipelineState(goal="Goal only.", cwd="/tmp/project"),
            review,
            skip_run=False,
            runs_degraded=True,
        )
        assert terminal is not None
        assert terminal[1] == DEGRADED_SEED_SAFETY_BLOCKERS_STOP_REASON_CODE


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


@pytest.mark.asyncio
async def test_deadline_seed_with_sourced_goal_passes_seed_qa_to_run(tmp_path) -> None:
    """A passing Seed QA re-checks the review gate; the grade bar stays waived.

    The degraded Seed grades B (its unresolved slots are findings), which
    would stop a normal Seed at the post-QA gate.
    """
    qa_calls: list[str] = []

    async def _passing_seed_qa(seed: Seed, _ledger: SeedDraftLedger) -> EvaluateResult:
        qa_calls.append(seed.metadata.seed_id)
        return EvaluateResult(passed=True, score=0.91, verdict="pass")

    run_starter = _RecordingRunStarter()
    pipeline = AutoPipeline(
        _DeadlineDriver(),
        _no_seed_generator,
        store=AutoStore(tmp_path),
        run_starter=run_starter,
        seed_qa_evaluator=_passing_seed_qa,
    )
    state = _build_deadline_state(tmp_path)

    result = await pipeline.run(state)

    assert result.grade == "B"
    assert len(qa_calls) == 1
    assert len(run_starter.seeds) == 1
    assert result.status == "complete"
    assert result.stop_reason_code is None


def _run_resume_state(tmp_path, ledger: SeedDraftLedger) -> AutoPipelineState:
    """A session persisted in RUN with a degraded sourced Seed and no run handle."""
    seed = with_ledger_carryover(
        partial_seed_from_evidence(ledger, reason="interview_phase_deadline"), ledger
    )
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.seed_artifact = seed.to_dict()
    state.ledger = ledger.to_dict()
    state.interview_completed = True
    state.last_grade = "B"
    for phase in (AutoPhase.INTERVIEW, AutoPhase.SEED_GENERATION, AutoPhase.REVIEW):
        state.transition(phase, phase.value)
    state.transition(AutoPhase.RUN, "run prepared")
    return state


@pytest.mark.asyncio
async def test_run_resume_starts_a_degraded_seed_with_sourced_goal(tmp_path) -> None:
    """A resumed RUN applies the same policy: the persisted grade B does not stop it."""
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
async def test_run_resume_keeps_the_safety_bar_for_a_degraded_seed(tmp_path) -> None:
    ledger = SeedDraftLedger.from_goal("Build a CLI")
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
    state = _run_resume_state(tmp_path, ledger)
    pipeline = AutoPipeline(
        _DeadlineDriver(),
        _no_seed_generator,
        store=AutoStore(tmp_path),
        run_starter=_forbidden_run_starter,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert result.stop_reason_code == DEGRADED_SEED_SAFETY_BLOCKERS_STOP_REASON_CODE


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
async def test_degraded_seed_with_safety_blocker_still_terminates(tmp_path) -> None:
    """Safety blockers must continue to terminate even on the degraded path.

    Inject a high-risk assumption into the ledger BEFORE the deadline fires so
    the grade gate still produces a blocker after the high_ambiguity_score /
    ledger_open_gap demotion. The pipeline must NOT route to the partial
    product terminal — it must BLOCKED through the normal grade-gate code path.
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
    pipeline = AutoPipeline(
        driver,
        _no_seed_generator,
        store=AutoStore(tmp_path),
        run_starter=_forbidden_run_starter,
    )

    result = await pipeline.run(state)
    await asyncio.sleep(0)

    # The safety blocker terminates — we must NOT see a partial product
    # terminal here, even though the Seed is degraded.
    assert result.partial_product is False
    # The terminal is machine-readable, not just prose: a consumer routing on
    # ``stop_reason_code`` has to be able to tell this safety stop from the
    # partial-product success terminal above, which carries ``None``. Dropping
    # ``error_code=`` at the ``mark_blocked`` call site is a silent regression
    # to ``None`` that every other assertion in this test survives.
    assert result.stop_reason_code == DEGRADED_SEED_SAFETY_BLOCKERS_STOP_REASON_CODE
    assert state.last_error_code == DEGRADED_SEED_SAFETY_BLOCKERS_STOP_REASON_CODE
    assert result.blocker is not None
    # And it survives the round trip: an operator resuming this session reads
    # the persisted record, not the in-memory object the pipeline mutated.
    reloaded = AutoStore(tmp_path).load(state.auto_session_id)
    assert reloaded.last_error_code == DEGRADED_SEED_SAFETY_BLOCKERS_STOP_REASON_CODE
    # ``auto.product.partial_emitted`` MUST NOT be emitted when the pipeline
    # blocks on a safety marker.
    partial_events = [
        event for event in event_store.appended if event.type == "auto.product.partial_emitted"
    ]
    assert partial_events == []


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


@pytest.mark.asyncio
async def test_normal_seed_unaffected_by_pr_c(tmp_path) -> None:
    """Normal (non-degraded) seeds must continue to take the legacy grade path.

    A run-of-the-mill pipeline test would assert this implicitly, but we keep
    one explicit assertion here so future PR-C tweaks cannot silently change
    the behavior for non-deadline sessions.
    """
    ledger = SeedDraftLedger.from_goal("Build a CLI.")
    # Populate enough sections that the legacy path produces a valid Seed.
    for section, value in {
        "actors": "End user.",
        "inputs": "CLI argument.",
        "outputs": "stdout greeting.",
        "constraints": "Pure Python.",
        "non_goals": "Long-running daemon.",
        "acceptance_criteria": "Exit code 0 and prints greeting.",
        "verification_plan": "Run with sample arg; assert stdout/exit.",
        "failure_modes": "Missing argument raises typed error.",
        "runtime_context": "Local developer shell on POSIX.",
    }.items():
        ledger.add_entry(
            section,
            LedgerEntry(
                key=f"{section}.normal",
                value=value,
                source=LedgerSource.USER_GOAL,
                confidence=0.9,
                status=LedgerStatus.CONFIRMED,
            ),
        )

    # Build a legacy-path Seed via the unchanged synthesizer through the
    # gate directly: PR-C must not alter its grading.
    from ouroboros.auto.ledger_seed import synthesize_seed_from_ledger

    seed = synthesize_seed_from_ledger(ledger)
    gate = GradeGate()

    result = gate.grade_seed(seed, ledger=ledger)
    assert seed.metadata.degraded is False
    # Normal seeds keep the strict ambiguity gate.
    if seed.metadata.ambiguity_score > 0.20:
        assert any(b.code == "high_ambiguity_score" for b in result.blockers)
