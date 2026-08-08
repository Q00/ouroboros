"""Bounded in-process transient retry around interview-backend calls.

One flaky ``interview.start`` / ``interview.resume`` / ``interview.answer``
round must not discard the remaining interview round budget (Vision #1157).
``ooo auto --resume`` already re-enters these calls idempotently
(``_recoverable_phase_for_tool`` routes ``interview.*`` back to
``AutoPhase.INTERVIEW``), which proves re-invoking them is safe — so a
bounded in-process retry is strictly safer than blocking on the first
hiccup. These tests pin the retry contract added around
``AutoInterviewDriver._with_transient_retry``.
"""

from __future__ import annotations

import pytest

from ouroboros.auto import interview_driver
from ouroboros.auto.interview_driver import (
    AutoInterviewDriver,
    FunctionInterviewBackend,
    InterviewTurn,
)
from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus, SeedDraftLedger
from ouroboros.auto.state import AutoPipelineState, AutoStore


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


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this module is a retry test — never sleep for real."""
    monkeypatch.setattr(interview_driver, "_INTERVIEW_TRANSIENT_BACKOFF_SECONDS", (0.0,))


async def _run_gap_closure_driver(tmp_path, answer_fn) -> tuple[object, object]:
    """Drive a 1-gap ledger to closure using a caller-supplied ``answer`` stub.

    ``"What else should we know?"`` matches ``_BROAD_PROMPT_RE`` so the
    driver's gap-steering path deterministically targets the single open
    ``runtime_context`` gap, keeping the number of interview ROUNDS needed
    to close identical across runs regardless of how many backend.answer
    ATTEMPTS a given round burns through.
    """

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn(
            "What else should we know?",
            "interview_retry_answer",
            seed_ready=False,
            completed=False,
            ambiguity_score=0.40,
        )

    state = AutoPipelineState(goal="Build a habit-tracker CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    ledger.sections["runtime_context"].entries.clear()
    assert ledger.open_gaps() == ["runtime_context"]

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer_fn),
        store=AutoStore(tmp_path),
        max_rounds=5,
        timeout_seconds=5,
    )
    result = await driver.run(state, ledger)
    return result, state


@pytest.mark.asyncio
async def test_answer_round_raises_once_then_succeeds_not_blocked(tmp_path) -> None:
    """A single transient ``backend.answer`` failure is absorbed by retry.

    The round must still complete (not ``blocked``), and the retried attempt
    must NOT be double-counted against the round budget: the number of
    interview ROUNDS consumed must match a failure-free baseline run exactly
    — only the number of backend.answer ATTEMPTS differs (by the one extra
    retry), never the round count.
    """

    async def baseline_answer(
        session_id: str, text: str, *, last_question: str | None = None
    ) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn(
            "done", session_id, seed_ready=True, completed=True, ambiguity_score=0.0
        )

    baseline_result, baseline_state = await _run_gap_closure_driver(
        tmp_path / "baseline", baseline_answer
    )
    assert baseline_result.status != "blocked", baseline_result.blocker

    answer_calls = 0

    async def flaky_answer(
        session_id: str, text: str, *, last_question: str | None = None
    ) -> InterviewTurn:  # noqa: ARG001
        nonlocal answer_calls
        answer_calls += 1
        if answer_calls == 1:
            raise RuntimeError("transient blip")
        return InterviewTurn(
            "done", session_id, seed_ready=True, completed=True, ambiguity_score=0.0
        )

    result, state = await _run_gap_closure_driver(tmp_path / "flaky", flaky_answer)

    assert result.status != "blocked", result.blocker
    # One extra backend.answer ATTEMPT (the failure + its retry) compared to
    # the baseline, but the ROUND count is identical — the failed attempt
    # was not double-counted against the interview round budget.
    assert answer_calls == baseline_state.current_round + 1
    assert state.current_round == baseline_state.current_round


@pytest.mark.asyncio
async def test_answer_round_exhausts_retries_and_blocks(tmp_path) -> None:
    """A persistently failing ``backend.answer`` blocks only after exhaustion."""
    answer_calls = 0

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn(
            "What is the primary goal?",
            "interview_retry_answer_exhausted",
            seed_ready=False,
            completed=False,
            ambiguity_score=0.40,
        )

    async def answer(
        session_id: str, text: str, *, last_question: str | None = None
    ) -> InterviewTurn:  # noqa: ARG001
        nonlocal answer_calls
        answer_calls += 1
        raise RuntimeError("persistent backend failure")

    state = AutoPipelineState(goal="Build a habit-tracker CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    ledger.sections["runtime_context"].entries.clear()

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=3,
        timeout_seconds=5,
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert answer_calls == interview_driver._INTERVIEW_TRANSIENT_ATTEMPTS
    assert state.last_error_code == "interview_round_transient_exhausted"
    assert state.last_tool_name == "interview.answer"
    # The failed round never advanced the round counter.
    assert state.current_round == 0


@pytest.mark.asyncio
async def test_backend_start_raises_once_then_succeeds(tmp_path) -> None:
    """A single transient ``backend.start`` failure is absorbed by retry."""
    start_calls = 0

    async def start(goal: str, cwd: str, *, interview_id: str | None = None) -> InterviewTurn:  # noqa: ARG001
        nonlocal start_calls
        start_calls += 1
        if start_calls == 1:
            raise RuntimeError("transient blip")
        return InterviewTurn("What should we verify?", interview_id or "interview_retry_start")

    async def answer(
        session_id: str, text: str, *, last_question: str | None = None
    ) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=3,
        timeout_seconds=5,
    )

    result = await driver.run(state, ledger)

    assert result.status != "blocked", result.blocker
    assert start_calls == 2
    assert state.interview_session_id is not None
    assert state.last_error_code is None


@pytest.mark.asyncio
async def test_backend_resume_persistent_allowlisted_failure_attempts_closure_fallback(
    tmp_path, monkeypatch
) -> None:
    """A persistently failing ``backend.resume`` attempts the closure fallback.

    Before this change, ``_try_close_after_backend_start_failure`` was only
    ever invoked from the ``interview.start`` failure branch. The resume
    branch is functionally identical (the helper closes from deterministic
    ledger evidence whenever ``_is_authoring_backend_unavailable`` matches
    the exception text) — it was simply never wired up. This pins that the
    SAME helper now runs for a resume-time failure too.
    """
    resume_calls = 0

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("start must not run when session_id is pre-seeded")

    async def resume(session_id: str) -> InterviewTurn:  # noqa: ARG001
        nonlocal resume_calls
        resume_calls += 1
        # "provider" matches `_is_authoring_backend_unavailable`'s allowlist,
        # making this failure closure-eligible.
        raise RuntimeError("provider unavailable")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("answer must not run when resume never lands")

    state = AutoPipelineState(goal="Build a habit-tracker CLI", cwd=str(tmp_path))
    state.interview_session_id = "interview_preexisting"
    state.pending_question = None
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    assert ledger.is_seed_ready()

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer, resume=resume),
        store=AutoStore(tmp_path),
        max_rounds=3,
        timeout_seconds=5,
    )

    # ``AutoInterviewDriver`` is ``@dataclass(slots=True)``, so an instance
    # can't shadow a method; patch the class instead (monkeypatch restores it
    # automatically).
    fallback_calls: list[Exception] = []
    original_fallback = AutoInterviewDriver._try_close_after_backend_start_failure

    async def spy_fallback(self, state_arg, ledger_arg, exc):
        fallback_calls.append(exc)
        return await original_fallback(self, state_arg, ledger_arg, exc)

    monkeypatch.setattr(AutoInterviewDriver, "_try_close_after_backend_start_failure", spy_fallback)

    result = await driver.run(state, ledger)

    assert resume_calls == interview_driver._INTERVIEW_TRANSIENT_ATTEMPTS
    assert len(fallback_calls) == 1, "closure fallback must be attempted on the RESUME branch"
    assert "provider unavailable" in str(fallback_calls[0])
    # The ledger was already complete, so the fallback closes cleanly
    # instead of falling through to a hard block.
    assert result.status == "seed_ready"
    assert state.interview_closure_mode == "ledger_only_no_backend"


@pytest.mark.asyncio
async def test_backend_resume_exhausted_non_allowlisted_failure_still_blocks(tmp_path) -> None:
    """A resume failure that isn't closure-eligible still blocks, with the new error code."""

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("start must not run when session_id is pre-seeded")

    async def resume(session_id: str) -> InterviewTurn:  # noqa: ARG001
        raise RuntimeError("totally unrelated failure")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("answer must not run when resume never lands")

    state = AutoPipelineState(goal="Build a habit-tracker CLI", cwd=str(tmp_path))
    state.interview_session_id = "interview_preexisting"
    state.pending_question = None
    ledger = SeedDraftLedger.from_goal(state.goal)

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer, resume=resume),
        store=AutoStore(tmp_path),
        max_rounds=3,
        timeout_seconds=5,
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert state.last_error_code == "interview_backend_transient_exhausted"
    assert state.last_tool_name == "interview.resume"
