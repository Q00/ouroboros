"""Tests for canonical stop_reason_code on pipeline blockers.

Covers the two interview-layer canonical codes and the two grade-gate codes
surfaced via ``AutoPipelineResult.stop_reason_code`` and
``AutoPipelineState.last_error_code``, plus a regression guard that blockers
without a canonical code leave ``stop_reason_code`` at ``None`` -- the guard is
what keeps "typed the terminals we meant to type" from drifting into "every
blocker gets a code whether or not anyone chose one".
"""

from __future__ import annotations

import asyncio

import pytest

from ouroboros.auto.adapters import EvaluateResult
from ouroboros.auto.grading import GradeGate, GradeResult, SeedGrade
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

# test_interview_max_rounds_exhausted_sets_stop_reason_code relies on the
# unsafe-context matcher firing (an unsafe goal blocks safe-default closure),
# which is disabled by default under the freedom policy (empty production
# bank); re-inject the historical bank. See tests/unit/auto/conftest.py.
pytestmark = pytest.mark.usefixtures("_legacy_unsafe_bank")

# The grade-gate codes are asserted as literals, not imported from
# ``ouroboros.auto.grade_gate_terminals``. The import would be tidier and it is
# the wrong call here: the module is new in this round, so at the reviewed
# baseline it does not exist, and a module-level import of it turns the whole
# file into a collection error there. A collection error is reported against the
# file, not against any node, so every new case comes back *absent* rather than
# failing -- and "these tests fail without the fix" becomes unprovable for the
# entire file, including the cases that have nothing to do with the new module.
#
# The literals are also the better assertion. What this PR promises is a wire
# value that consumers read; pinning the const would only prove the tests and
# the pipeline agree about a name they both import, which they would still do
# after someone renamed the value. If a const is retargeted, these fail.

# ---------------------------------------------------------------------------
# Shared stubs
# ---------------------------------------------------------------------------


async def _blocked_seed_generator(session_id: str):  # noqa: ARG001
    raise AssertionError("seed generator must not be called in these tests")


# ---------------------------------------------------------------------------
# Test 1 — interview_max_rounds_exhausted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interview_max_rounds_exhausted_sets_stop_reason_code(tmp_path) -> None:
    """AutoPipeline result carries ``interview_max_rounds_exhausted`` when the
    driver exhausts ``max_rounds`` with an unsafe-context goal so safe-default
    finalization cannot close the ledger.
    """

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "session_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        # Backend never declares closure — seed_ready stays False.
        return InterviewTurn("What else?", session_id, seed_ready=False)

    # Unsafe-context goal prevents safe-default finalization from closing the
    # ledger, so the driver is forced to emit the max_rounds_exhausted blocker.
    state = AutoPipelineState(
        goal="Deploy the service to production and configure the required credentials",
        cwd=str(tmp_path),
    )
    store = AutoStore(tmp_path)

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=store,
        max_rounds=2,
        timeout_seconds=5,
    )
    pipeline = AutoPipeline(driver, _blocked_seed_generator, store=store)

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert result.stop_reason_code == "interview_max_rounds_exhausted"
    assert state.last_error_code == "interview_max_rounds_exhausted"
    assert result.blocker is not None
    assert "without closure" in result.blocker


# ---------------------------------------------------------------------------
# Test 2 — interview_phase_deadline no longer terminal (#1257 PR-B)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interview_phase_deadline_routes_into_closure_ladder(tmp_path) -> None:
    """#1257 PR-B: the per-phase interview deadline must NOT terminate as
    ``interview_phase_deadline`` BLOCKED anymore.

    Instead, the pipeline emits a degraded Seed via
    :func:`partial_seed_from_evidence` and transitions through
    SEED_GENERATION → REVIEW. The deadline may still surface a downstream
    blocker (e.g. a grade-gate C result before PR-C teaches the gate to
    respect ``metadata.degraded``), but the stop_reason_code MUST be
    something other than ``interview_phase_deadline`` and the persisted
    Seed MUST carry the degraded-recovery metadata.
    """

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        await asyncio.sleep(3600)
        raise AssertionError("interview.start should have been cancelled by phase timeout")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("answer should never be called")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.timeout_seconds_by_phase[AutoPhase.INTERVIEW.value] = 1
    import time

    state.deadline_at = time.monotonic() + 3600
    state.deadline_at_epoch = time.time() + 3600
    state.transition(AutoPhase.INTERVIEW, "starting interview")

    store = AutoStore(tmp_path)

    class _SlowDriver:
        progress_callback = None

        async def run(self, _state, _ledger):  # noqa: ARG002
            await asyncio.sleep(3600)
            raise AssertionError("must be cancelled by phase timeout")

    pipeline = AutoPipeline(_SlowDriver(), _blocked_seed_generator, store=store)

    result = await pipeline.run(state)

    # The per-phase deadline must no longer be the terminal stop reason.
    assert result.stop_reason_code != "interview_phase_deadline"
    assert state.last_error_code != "interview_phase_deadline"

    # A degraded Seed artifact must have been persisted by the closure ladder.
    assert state.seed_artifact is not None
    metadata = state.seed_artifact.get("metadata", {})
    assert metadata.get("generation_mode") == "partial_seed_from_evidence"
    assert metadata.get("degraded") is True
    assert metadata.get("recovery_reason") == "interview_phase_deadline"
    # The unresolved gaps must be surfaced verbatim so PR-C / PR-D can convert
    # them into next-step hints.
    assert metadata.get("unresolved_slots"), (
        "degraded seed must list unresolved ledger sections so downstream "
        "gates can convert them into next-step hints"
    )


# ---------------------------------------------------------------------------
# Test 3 — blockers without a canonical code leave stop_reason_code None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blockers_without_canonical_code_leave_stop_reason_code_none(tmp_path) -> None:
    """Blockers that do not have a canonical code must leave ``stop_reason_code``
    at ``None`` while still populating ``result.blocker``.

    Uses a pre-blocked state at SEED_GENERATION (grade_gate style) to exercise
    the ``_result()`` path without touching the interview-layer call sites.
    """

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("interview must not run when already blocked")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("interview must not run when already blocked")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))

    # Mark blocked WITHOUT an error_code. Use a tool_name that is not in the
    # recoverable-tool map (returns None from _recoverable_phase_for_tool) so
    # the pipeline returns this blocked state directly without trying to resume
    # into a subsequent phase.
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.mark_blocked(
        "pipeline budget exhausted before completion",
        tool_name="pipeline_deadline",
    )
    # No error_code set — last_error_code must stay None.
    assert state.last_error_code is None

    store = AutoStore(tmp_path)
    store.save(state)

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=store,
    )
    pipeline = AutoPipeline(driver, _blocked_seed_generator, store=store)

    result = await pipeline.run(state)

    # The blocked state is terminal — pipeline returns it directly.
    assert result.status == "blocked"
    assert result.stop_reason_code is None
    assert result.blocker is not None
    assert "pipeline budget exhausted" in result.blocker


def test_recovery_transition_clears_stale_stop_reason_code(tmp_path) -> None:
    """A recovered session must not keep reporting an old blocker code."""

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.mark_blocked(
        "interview phase exceeded 600s timeout",
        tool_name="interview.run",
        error_code="interview_phase_deadline",
    )

    state.recover(AutoPhase.INTERVIEW, "retrying interview")

    assert state.last_error is None
    assert state.last_error_code is None


def test_failed_transition_clears_stale_stop_reason_code(tmp_path) -> None:
    """A later hard failure must not inherit an earlier blocker code."""

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.last_error_code = "interview_phase_deadline"

    state.mark_failed("seed generation crashed", tool_name="seed_generator")

    assert state.last_error == "seed generation crashed"
    assert state.last_error_code is None


# ---------------------------------------------------------------------------
# Test 4 — grade-gate terminals carry canonical codes
# ---------------------------------------------------------------------------


def _ready_ledger(goal: str) -> SeedDraftLedger:
    ledger = SeedDraftLedger.from_goal(goal)
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
    return ledger


def _cli_seed() -> Seed:
    return Seed(
        goal="Build a local CLI",
        constraints=("Use existing project patterns",),
        acceptance_criteria=("`habit list` prints stable stdout containing created habits",),
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


class _FixedGradeGate(GradeGate):
    """Grade every Seed at a fixed grade / ``may_run``, ignoring its content."""

    def __init__(self, grade: SeedGrade, *, may_run: bool) -> None:
        self._grade = grade
        self._may_run = may_run

    def grade_seed(
        self,
        seed: Seed,  # noqa: ARG002
        *,
        ledger: SeedDraftLedger | None = None,  # noqa: ARG002
        closure_mode: str | None = None,  # noqa: ARG002
        degraded: bool | None = None,  # noqa: ARG002
    ) -> GradeResult:
        passing = self._grade is SeedGrade.A
        return GradeResult(
            grade=self._grade,
            scores={
                "coverage": 0.95 if passing else 0.5,
                "ambiguity": 0.05 if passing else 0.5,
                "testability": 0.95 if passing else 0.5,
                "execution_feasibility": 0.95 if passing else 0.4,
                "risk": 0.05 if passing else 0.4,
            },
            may_run=self._may_run,
        )


def _closed_interview_backend() -> FunctionInterviewBackend:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn(
            "done", "interview_grade_gate", seed_ready=True, completed=True, ambiguity_score=0.12
        )

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn(
            "done", session_id, seed_ready=True, completed=True, ambiguity_score=0.12
        )

    return FunctionInterviewBackend(start, answer)


async def _run_with_gate(tmp_path, gate: GradeGate, **kwargs):
    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _cli_seed()

    async def run_seed(seed: Seed, *, idempotency_key: str = ""):  # noqa: ARG001
        raise AssertionError("a grade-gated Seed must never reach the run starter")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.ledger = _ready_ledger(state.goal).to_dict()
    store = AutoStore(tmp_path)
    pipeline = AutoPipeline(
        AutoInterviewDriver(_closed_interview_backend(), store=store, max_rounds=1),
        generate_seed,
        run_starter=run_seed,
        store=store,
        grade_gate=gate,
        **kwargs,
    )
    return await pipeline.run(state), state


@pytest.mark.asyncio
async def test_grade_below_required_sets_canonical_stop_reason_code(tmp_path) -> None:
    """A Seed graded below ``required_grade`` must stop with a typed code.

    Without a code the only machine-readable signal is the English blocker
    prose, which is what every consumer (``ooo auto`` printing ``-``, the MCP
    envelope omitting ``stop_reason_code``, harness traces writing null) was
    forced to parse to tell this terminal apart from the ``may_run`` one.
    """
    result, state = await _run_with_gate(tmp_path, _FixedGradeGate(SeedGrade.C, may_run=True))

    assert result.status == "blocked"
    assert result.blocker is not None
    assert "did not meet required grade" in result.blocker
    assert result.stop_reason_code == "seed_grade_below_required"
    assert state.last_error_code == "seed_grade_below_required"


@pytest.mark.asyncio
async def test_review_withheld_run_sets_canonical_stop_reason_code(tmp_path) -> None:
    """A passing grade whose review withholds ``may_run`` gets its own code.

    Distinct from :data:`SEED_GRADE_BELOW_REQUIRED_STOP_REASON_CODE`: the Seed
    cleared the grade bar, so "raise the grade" is the wrong next step.
    """
    result, state = await _run_with_gate(tmp_path, _FixedGradeGate(SeedGrade.A, may_run=False))

    assert result.status == "blocked"
    assert result.blocker == "Seed review did not clear the Seed for execution"
    assert result.stop_reason_code == "seed_review_withheld_run"
    assert state.last_error_code == "seed_review_withheld_run"


class _WithholdAfterQaGate(GradeGate):
    """Grade A, then withhold the run once ``withhold_run`` is set.

    A gate with one fixed answer can never reach the post-QA review gate: the
    REVIEW-phase gate at ``pipeline.py:1170`` asks the same question strictly
    earlier and stops there. The second entry point only judges a review the
    first one never saw -- the one produced by re-reviewing a QA-repaired Seed
    -- so the gate has to be able to change its mind to reach it at all.
    """

    def __init__(self) -> None:
        self.withhold_run = False

    def grade_seed(
        self,
        seed: Seed,  # noqa: ARG002
        *,
        ledger: SeedDraftLedger | None = None,  # noqa: ARG002
        closure_mode: str | None = None,  # noqa: ARG002
        degraded: bool | None = None,  # noqa: ARG002
    ) -> GradeResult:
        return GradeResult(
            grade=SeedGrade.A,
            scores={
                "coverage": 0.95,
                "ambiguity": 0.05,
                "testability": 0.95,
                "execution_feasibility": 0.95,
                "risk": 0.05,
            },
            may_run=not self.withhold_run,
        )


@pytest.mark.asyncio
async def test_post_qa_review_gate_sets_canonical_stop_reason_code(tmp_path) -> None:
    """The QA-repair path re-reviews the Seed; that terminal must be typed too.

    ``seed_review_gate_blocker`` is the second entry point into the very same
    two terminals, so it must not drop the classification the gate computed.
    Reaching it takes a full repair round: QA fails, the Seed is repaired, the
    repaired Seed is re-reviewed, and the post-QA gate judges *that* review.
    """
    gate = _WithholdAfterQaGate()
    qa_calls: list[Seed] = []

    async def seed_qa(seed: Seed, ledger: SeedDraftLedger) -> EvaluateResult:  # noqa: ARG001
        qa_calls.append(seed)
        if len(qa_calls) == 1:
            # The repair this failure triggers yields a Seed the re-review
            # still grades A but no longer clears for execution.
            gate.withhold_run = True
            return EvaluateResult(
                passed=False,
                score=0.41,
                verdict="fail",
                differences=("The Seed omits the review-blocking post-QA constraint",),
            )
        return EvaluateResult(passed=True, score=0.93, verdict="pass")

    result, state = await _run_with_gate(tmp_path, gate, seed_qa_evaluator=seed_qa)

    # Without this the test would pass on the REVIEW-phase terminal instead and
    # assert nothing the preceding test does not already assert.
    assert len(qa_calls) == 2, "the post-QA gate is only reachable after a repair round"
    assert result.status == "blocked"
    assert result.blocker == "Seed review did not clear the Seed for execution"
    assert result.stop_reason_code == "seed_review_withheld_run"
    assert state.last_error_code == "seed_review_withheld_run"
