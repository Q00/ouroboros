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


def test_converge_does_not_review_again_after_bound_hit() -> None:
    """Once ``len(history) >= max_iterations`` the loop must NOT call reviewer again."""
    reviewer = _AlwaysVagueReviewer()
    repairer = SeedRepairer(reviewer=reviewer, max_iterations=3)
    seed = _seed()

    _, _, history = repairer.converge(seed)

    # 1 initial review + 1 review after each repair (skipping the trailing
    # post-bound call). Without the bound-skip the reviewer would be called
    # max_iterations + 1 == 4 times.
    assert len(history) == 3
    assert reviewer.calls == 3


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
