"""Tests for canonical stop_reason_code on interview-layer blockers.

Covers the two interview-layer canonical codes surfaced via
``AutoPipelineResult.stop_reason_code`` and ``AutoPipelineState.last_error_code``,
plus a regression guard that blockers without a canonical code leave
``stop_reason_code`` at ``None``.
"""

from __future__ import annotations

import asyncio

import pytest

from ouroboros.auto.interview_driver import (
    AutoInterviewDriver,
    FunctionInterviewBackend,
    InterviewTurn,
)
from ouroboros.auto.pipeline import AutoPipeline
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore

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
