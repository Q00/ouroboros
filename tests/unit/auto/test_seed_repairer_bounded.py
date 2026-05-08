"""Bounded-iteration regression tests for SeedRepairer.converge.

Issue #775 introduces an explicit ``max_iterations`` cap on the convergence
loop and an outer ``asyncio.wait_for`` budget around the synchronous
``repairer.converge`` call inside ``AutoPipeline.run``. These tests pin both
guarantees so a future regression cannot reintroduce an unbounded LLM-spend
hang.
"""

from __future__ import annotations

import threading
import time

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

# ---------------------------------------------------------------------------
# Test helpers (mirrors of the small fixtures used in test_interview_pipeline.py
# kept local so this regression file can be read in isolation by reviewers).
# ---------------------------------------------------------------------------


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
    ac: tuple[str, ...] = ("The CLI should be easy and user-friendly",),
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


def _vague_review(message: str = "Still vague") -> SeedReview:
    """Build a SeedReview that reports a single high-severity vague AC.

    The exact ``message`` is rolled into the finding fingerprint, so callers
    can vary it across iterations to defeat the existing dedup short-circuit
    (``high == previous_high_fingerprints``) and exercise the
    ``max_iterations`` bound directly.
    """
    finding = ReviewFinding.from_parts(
        code="vague_acceptance_criteria",
        target="acceptance_criteria[0]",
        severity="high",
        message=message,
        repair_instruction="Make it observable.",
    )
    return SeedReview(
        grade_result=GradeResult(
            grade=SeedGrade.B,
            scores={
                "coverage": 0.5,
                "ambiguity": 0.5,
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


class _AlwaysVagueReviewer:
    """Reviewer that always returns a vague-AC finding.

    Each call rotates the finding message to produce a fresh fingerprint, so
    the convergence loop's dedup short-circuit does not fire and the only
    remaining bound is ``max_iterations``.
    """

    def __init__(self) -> None:
        self.calls = 0

    def review(self, seed: Seed, *, ledger: SeedDraftLedger | None = None) -> SeedReview:  # noqa: ARG002 — protocol shape
        self.calls += 1
        return _vague_review(message=f"Still vague pass {self.calls}")


# ---------------------------------------------------------------------------
# AC: max_iterations caps the repair attempt count.
# ---------------------------------------------------------------------------


def test_converge_stops_after_default_max_iterations() -> None:
    reviewer = _AlwaysVagueReviewer()
    repairer = SeedRepairer(reviewer=reviewer)
    seed = _seed()

    _, _, history = repairer.converge(seed)

    # Default max_iterations is 5 (mirrors AutoPipelineState.max_repair_rounds).
    assert repairer.max_iterations == 5
    assert len(history) == 5


def test_converge_respects_custom_max_iterations() -> None:
    reviewer = _AlwaysVagueReviewer()
    repairer = SeedRepairer(reviewer=reviewer, max_iterations=2)
    seed = _seed()

    _, _, history = repairer.converge(seed)

    assert len(history) == 2


def test_converge_reviews_once_more_to_reconcile_after_bound_hit() -> None:
    """Once ``len(history) >= max_iterations`` the loop must NOT keep repairing.

    The original AC was "don't keep repairing past the bound" — we still honor
    that. But when the bound is reached *immediately after* a successful
    repair, the cached review still describes the pre-repair seed. PR #785
    review-1 requires exactly one final reconciliation review so the returned
    ``(seed, review)`` pair is consistent (the pipeline persists
    ``state.last_grade`` / ``state.findings`` from this review).
    """
    reviewer = _AlwaysVagueReviewer()
    repairer = SeedRepairer(reviewer=reviewer, max_iterations=3)
    seed = _seed()

    _, _, history = repairer.converge(seed)

    # 1 initial review + 1 review after each non-final repair (2)
    # + 1 final reconciliation review at the bound = 4. No further repair
    # attempts run, so ``history`` is still exactly ``max_iterations``.
    assert len(history) == 3
    assert reviewer.calls == 4


def test_converge_returned_seed_and_review_are_consistent_at_bound() -> None:
    """At the bound the returned ``(seed, review)`` pair must be consistent.

    Regression test for PR #785 review-1: previously when the loop hit
    ``max_iterations`` immediately after applying a repair, the returned
    ``review`` still described the *pre-repair* seed. Re-running the reviewer
    on the returned seed produced a different review than the one returned —
    that drove ``AutoPipeline.run`` to persist a stale ``last_grade`` /
    ``findings`` and could block a seed that the final allowed repair fixed.
    """

    class _SwitchOnReconcileReviewer:
        """Returns vague findings until the reconciliation review at the bound.

        With ``max_iterations=2`` the converge loop calls reviewer 3 times:
          - call 1 (initial review)            → vague
          - call 2 (post first repair)         → vague (drives second repair)
          - call 3 (reconciliation at bound)   → clean A-grade

        The pre-fix code skipped call 3 and returned the call-2 vague review
        describing the *pre-second-repair* seed. Re-running the reviewer on
        the returned seed produced the clean call-3 review — i.e., the pair
        was inconsistent.
        """

        def __init__(self) -> None:
            self.calls = 0

        def review(self, seed: Seed, *, ledger: SeedDraftLedger | None = None) -> SeedReview:  # noqa: ARG002 — protocol shape
            self.calls += 1
            if self.calls < 3:
                return _vague_review(message=f"Still vague pass {self.calls}")
            return SeedReview(
                grade_result=GradeResult(
                    grade=SeedGrade.A,
                    scores={
                        "coverage": 0.95,
                        "ambiguity": 0.95,
                        "testability": 0.95,
                        "execution_feasibility": 0.95,
                        "risk": 0.05,
                    },
                    findings=[],
                    blockers=[],
                    may_run=True,
                ),
                findings=(),
            )

    reviewer = _SwitchOnReconcileReviewer()
    repairer = SeedRepairer(reviewer=reviewer, max_iterations=2)
    seed = _seed()

    returned_seed, returned_review, history = repairer.converge(seed)

    # The bound was reached after a repair was applied, so the loop ran one
    # final reconciliation review. The returned review describes the returned
    # seed (clean A) — not the stale pre-repair vague review.
    assert len(history) == 2
    assert returned_review.grade_result.grade == SeedGrade.A
    assert returned_review.findings == ()


@pytest.mark.asyncio
async def test_pipeline_last_grade_reflects_returned_seed_at_bound(tmp_path) -> None:
    """``AutoPipeline.run`` must persist a grade consistent with the returned seed.

    Regression for PR #785 review-1: with the pre-fix code, hitting the
    repair bound immediately after a fix would persist the pre-repair grade
    onto ``state.last_grade``, blocking a seed that was actually fixed.
    """

    class _SwitchOnReconcileReviewer:
        """Vague until the reconciliation review at the bound, then A-grade."""

        def __init__(self) -> None:
            self.calls = 0

        def review(self, seed: Seed, *, ledger: SeedDraftLedger | None = None) -> SeedReview:  # noqa: ARG002 — protocol shape
            self.calls += 1
            if self.calls < 3:
                return _vague_review(message=f"pass {self.calls}")
            return SeedReview(
                grade_result=GradeResult(
                    grade=SeedGrade.A,
                    scores={
                        "coverage": 0.95,
                        "ambiguity": 0.95,
                        "testability": 0.95,
                        "execution_feasibility": 0.95,
                        "risk": 0.05,
                    },
                    findings=[],
                    blockers=[],
                    may_run=True,
                ),
                findings=(),
            )

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", "interview_1", seed_ready=True, completed=True)

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("completed interview should not need another answer")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed()

    reviewer = _SwitchOnReconcileReviewer()
    repairer = SeedRepairer(reviewer=reviewer, max_iterations=2)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        store=AutoStore(tmp_path),
        repairer=repairer,
        skip_run=True,
    )

    await pipeline.run(state)

    # The repair at the bound produced a clean seed: the persisted grade and
    # findings must reflect the post-repair review, not the stale pre-repair
    # one. With the pre-fix code ``state.last_grade`` would be "B".
    assert state.last_grade == "A"
    assert state.findings == []


# ---------------------------------------------------------------------------
# AC: outer asyncio.wait_for inside AutoPipeline.run blocks runaway converge
# without leaking the worker thread (no orphan threads after timeout).
# ---------------------------------------------------------------------------


class _SleepyReviewer:
    """Reviewer whose first call sleeps long enough to trip wait_for."""

    def __init__(self, sleep_seconds: float) -> None:
        self.sleep_seconds = sleep_seconds
        self.calls = 0
        self.thread_finished = threading.Event()

    def review(self, seed: Seed, *, ledger: SeedDraftLedger | None = None) -> SeedReview:  # noqa: ARG002 — protocol shape
        self.calls += 1
        try:
            time.sleep(self.sleep_seconds)
        finally:
            self.thread_finished.set()
        return _vague_review()


@pytest.mark.asyncio
async def test_pipeline_blocks_when_repair_phase_exceeds_timeout(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", "interview_1", seed_ready=True, completed=True)

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("completed interview should not need another answer")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed()

    timeout = 1  # whole seconds — phase_timeout_seconds rejects non-positive ints
    sleepy = _SleepyReviewer(sleep_seconds=timeout * 2)
    repairer = SeedRepairer(reviewer=sleepy)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.timeout_seconds_by_phase[AutoPhase.REPAIR.value] = timeout
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        store=AutoStore(tmp_path),
        repairer=repairer,
        skip_run=True,
    )

    result = await pipeline.run(state)
    threads_after_return = threading.active_count()

    # Pipeline reaches BLOCKED, attribution points at the repairer, and the
    # error message contains the literal phrase the issue requires.
    assert state.phase == AutoPhase.BLOCKED
    assert state.last_tool_name == "seed_repairer"
    assert state.last_error is not None
    assert f"repair phase exceeded {timeout}s" in state.last_error
    assert result.status == "blocked"
    assert result.blocker is not None and "repair phase exceeded" in result.blocker

    # Wait for the daemon worker thread to unwind on its own. ``wait_for``
    # cannot cancel a synchronous ``time.sleep`` mid-call, so the worker
    # keeps running until its bounded sleep completes. The guarantee we want
    # is "no orphan thread leak" — i.e. once the sleep is over, no new
    # thread spun up under the rug, and ``active_count`` does not grow
    # beyond what the pipeline was already keeping alive.
    assert sleepy.thread_finished.wait(timeout=timeout * 4), (
        "sleepy reviewer thread never finished — orphan thread leak"
    )
    # Allow the asyncio default executor a beat to mark the worker idle, then
    # confirm the thread count did not balloon past the post-return baseline.
    deadline = time.monotonic() + 2.0
    while threading.active_count() > threads_after_return and time.monotonic() < deadline:
        time.sleep(0.01)
    assert threading.active_count() <= threads_after_return, (
        f"orphan threads: before-return={threads_after_return}, "
        f"after-sleep={threading.active_count()}"
    )


# ---------------------------------------------------------------------------
# AC: a session blocked by a repair-phase timeout must be resumable.
# Regression for PR #785 review-2: ``tool_name="seed_repairer"`` was missing
# from the recoverable-phase mapping, so ``--resume`` after a transient
# timeout would refuse to recover and turn the timeout into a permanent dead
# end. Resume must restart the REVIEW phase, which re-invokes the bounded
# repairer.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_recovers_session_blocked_by_repair_timeout(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", "interview_1", seed_ready=True, completed=True)

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("completed interview should not need another answer")

    seed_calls = {"count": 0}

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        seed_calls["count"] += 1
        return _seed()

    # First run: sleepy reviewer trips wait_for and blocks the session.
    timeout = 1
    sleepy = _SleepyReviewer(sleep_seconds=timeout * 2)
    blocking_repairer = SeedRepairer(reviewer=sleepy)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.timeout_seconds_by_phase[AutoPhase.REPAIR.value] = timeout
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()

    store = AutoStore(tmp_path)
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=store, max_rounds=1)
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        store=store,
        repairer=blocking_repairer,
        skip_run=True,
    )

    first_result = await pipeline.run(state)
    assert first_result.status == "blocked"
    assert state.phase == AutoPhase.BLOCKED
    assert state.last_tool_name == "seed_repairer"
    # Wait for the sleepy reviewer thread so it doesn't bleed into the resume run.
    assert sleepy.thread_finished.wait(timeout=timeout * 4)

    # Second run: clean reviewer should let the resumed session converge to A
    # and reach COMPLETE. Without the recoverable-phase mapping for
    # ``seed_repairer`` the pipeline would short-circuit at the BLOCKED check
    # and return the prior blocker untouched (no repairer call, last_grade
    # stays None).
    class _CleanReviewer:
        def __init__(self) -> None:
            self.calls = 0

        def review(self, seed: Seed, *, ledger: SeedDraftLedger | None = None) -> SeedReview:  # noqa: ARG002 — protocol shape
            self.calls += 1
            return SeedReview(
                grade_result=GradeResult(
                    grade=SeedGrade.A,
                    scores={
                        "coverage": 0.95,
                        "ambiguity": 0.95,
                        "testability": 0.95,
                        "execution_feasibility": 0.95,
                        "risk": 0.05,
                    },
                    findings=[],
                    blockers=[],
                    may_run=True,
                ),
                findings=(),
            )

    clean_reviewer = _CleanReviewer()
    resume_repairer = SeedRepairer(reviewer=clean_reviewer)
    resume_pipeline = AutoPipeline(
        driver,
        generate_seed,
        store=store,
        repairer=resume_repairer,
        skip_run=True,
    )

    resumed = await resume_pipeline.run(state)

    # Pipeline recovered: REVIEW re-ran, persisted a clean grade, and reached
    # COMPLETE. The resumed reviewer was actually invoked (proving the
    # repair phase resumed, not that we returned the prior blocker untouched).
    assert clean_reviewer.calls >= 1
    assert state.phase == AutoPhase.COMPLETE
    assert state.last_grade == "A"
    assert state.findings == []
    assert resumed.status == AutoPhase.COMPLETE.value
