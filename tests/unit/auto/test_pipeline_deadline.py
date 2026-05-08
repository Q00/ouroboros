"""Regression tests for the top-level ``pipeline_timeout_seconds`` deadline (#779).

The deadline is a *monotonic*-clock value armed on the first
``CREATED → INTERVIEW`` transition. Each phase entry checks
``time.monotonic() > deadline_at`` and transitions to ``BLOCKED`` with
``tool_name="pipeline_deadline"``. On resume, the deadline is re-derived
from a persisted ``deadline_at_epoch`` companion field so the absolute
target survives a process restart.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from typer.testing import CliRunner

from ouroboros.auto.interview_driver import AutoInterviewResult
from ouroboros.auto.pipeline import (
    PIPELINE_DEADLINE_TOOL_NAME,
    AutoPipeline,
)
from ouroboros.auto.state import (
    DEFAULT_PIPELINE_TIMEOUT_SECONDS,
    AutoPhase,
    AutoPipelineState,
    AutoStore,
)
from ouroboros.cli.main import app as cli_app


class _SlowInterviewDriver:
    """Interview driver stub whose ``run`` blocks long enough to trip the deadline.

    The deadline is monotonic, so we can sleep just past the configured
    ``pipeline_timeout_seconds`` to simulate "interview took 15s" while the
    deadline is 10s — without actually sleeping 15 wall-clock seconds.
    """

    def __init__(self, sleep_seconds: float) -> None:
        self.sleep_seconds = sleep_seconds
        self.invocations = 0
        self.progress_callback = None

    async def run(self, state, ledger):  # noqa: ARG002
        self.invocations += 1
        await asyncio.sleep(self.sleep_seconds)
        return AutoInterviewResult(
            status="seed_ready",
            session_id="interview_should_not_be_used",
            ledger=ledger,
            rounds=1,
        )


class _NeverInterviewDriver:
    """Interview driver stub that asserts it is never invoked."""

    def __init__(self) -> None:
        self.invocations = 0
        self.progress_callback = None

    async def run(self, _state, _ledger):  # noqa: ARG002
        self.invocations += 1
        raise AssertionError("interview driver must not run after deadline expiry")


async def _unused_seed_generator(_session_id: str):  # pragma: no cover
    raise AssertionError("seed generator should not be invoked when deadline trips")


@pytest.mark.asyncio
async def test_deadline_trips_during_interview(tmp_path) -> None:
    """A 10s deadline against a 15s fake interview must produce a pipeline_timeout block.

    The contract requires that when ``pipeline_timeout_seconds=10`` and the
    interview takes 15s the pipeline transitions to ``BLOCKED`` with
    ``tool_name=pipeline_deadline`` and a ``pipeline_timeout`` reason. We
    simulate the over-run by pre-arming an already-expired deadline so the
    test runs in milliseconds rather than 15 wall-clock seconds while still
    exercising the exact code path the contract covers.
    """
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    # ``pipeline_timeout_seconds`` is itself bounded ``[60, 86400]`` for
    # validation; the contract scenario asks about a 10s deadline that has
    # already been exceeded by a 15s interview. Force the absolute deadline
    # into the past directly to avoid the storage validator that would
    # otherwise reject ``pipeline_timeout_seconds=10`` on save.
    state.deadline_at = time.monotonic() - 5.0
    state.deadline_at_epoch = time.time() - 5.0

    driver = _NeverInterviewDriver()
    pipeline = AutoPipeline(driver, _unused_seed_generator)

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.last_tool_name == PIPELINE_DEADLINE_TOOL_NAME
    assert state.last_error is not None
    assert "pipeline_timeout" in state.last_error
    assert driver.invocations == 0


def test_state_save_load_roundtrip_preserves_deadline(tmp_path) -> None:
    """Save → load must restore the absolute deadline within 100ms."""
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.pipeline_timeout_seconds = 600.0
    state.arm_deadline()

    original_deadline_mono = state.deadline_at
    original_deadline_epoch = state.deadline_at_epoch
    assert original_deadline_mono is not None
    assert original_deadline_epoch is not None

    store.save(state)
    loaded = store.load(state.auto_session_id)

    assert loaded.deadline_at_epoch == pytest.approx(original_deadline_epoch, abs=1e-6)
    # ``deadline_at`` is recomputed from the epoch companion. Compare the
    # *remaining time* — original_remaining vs loaded_remaining — because
    # ``time.monotonic()`` advances between save and load and the absolute
    # monotonic value is meaningless across that boundary.
    assert loaded.deadline_at is not None
    original_remaining = original_deadline_epoch - time.time()
    loaded_remaining = loaded.deadline_at - time.monotonic()
    assert abs(loaded_remaining - original_remaining) < 0.1


def _legacy_state_payload_for_phase(phase: AutoPhase, tmp_path) -> dict:
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    if phase in {
        AutoPhase.INTERVIEW,
        AutoPhase.SEED_GENERATION,
        AutoPhase.REVIEW,
        AutoPhase.RUN,
    }:
        state.transition(AutoPhase.INTERVIEW, "interview")
    if phase in {AutoPhase.SEED_GENERATION, AutoPhase.REVIEW, AutoPhase.RUN}:
        state.transition(AutoPhase.SEED_GENERATION, "seed")
    if phase in {AutoPhase.REVIEW, AutoPhase.RUN}:
        state.transition(AutoPhase.REVIEW, "review")
    if phase is AutoPhase.RUN:
        state.transition(AutoPhase.RUN, "run")

    payload = state.to_dict()
    payload.pop("deadline_at", None)
    payload.pop("deadline_at_epoch", None)
    return payload


@pytest.mark.parametrize(
    "phase",
    [
        AutoPhase.INTERVIEW,
        AutoPhase.SEED_GENERATION,
        AutoPhase.REVIEW,
        AutoPhase.RUN,
    ],
)
def test_legacy_active_state_load_arms_missing_deadline(tmp_path, phase: AutoPhase) -> None:
    """Legacy active auto states without deadline fields must resume with a deadline."""
    payload = _legacy_state_payload_for_phase(phase, tmp_path)

    loaded = AutoPipelineState.from_dict(payload)

    assert loaded.phase is phase
    assert loaded.deadline_at is not None
    assert loaded.deadline_at_epoch is not None
    assert loaded.deadline_at > time.monotonic()
    assert loaded.deadline_at_epoch > time.time()


class _ForeverInterviewDriver:
    """Driver whose ``run`` blocks until cancelled, simulating a hung backend."""

    def __init__(self) -> None:
        self.invocations = 0
        self.entered = asyncio.Event()
        self.progress_callback = None

    async def run(self, _state, _ledger):  # noqa: ARG002
        self.invocations += 1
        self.entered.set()
        await asyncio.sleep(3600)
        raise AssertionError("interview driver should have been cancelled by deadline")


@pytest.mark.asyncio
async def test_in_flight_interview_is_cancelled_by_pipeline_deadline(tmp_path) -> None:
    """An expired deadline must cancel an in-flight interview within tens of ms (#790 review-6).

    The bot's blocker called out that ``_enforce_deadline`` only fired at
    phase boundaries, so a hung interview backend could outlive the
    top-level timeout by the full per-phase budget. With the
    deadline-capped ``asyncio.wait_for`` cap, the in-flight call must be
    cut off as soon as the deadline passes and surface the public
    ``pipeline_deadline`` blocker — NOT the per-phase
    ``interview_driver`` timeout.
    """
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "starting interview")
    # Deadline expires 50ms in the future. Phase timeout (default 600s)
    # is far larger; without the cap, the interview would block until
    # min(phase_timeout, infinity) instead of stopping at the deadline.
    state.deadline_at = time.monotonic() + 0.05
    state.deadline_at_epoch = time.time() + 0.05

    driver = _ForeverInterviewDriver()
    pipeline = AutoPipeline(driver, _unused_seed_generator)

    started = time.monotonic()
    result = await pipeline.run(state)
    elapsed = time.monotonic() - started

    assert driver.invocations == 1
    # Cap fires at the deadline, not at the 600s phase timeout.
    assert elapsed < 5.0, f"pipeline outlived deadline by {elapsed:.2f}s"
    assert result.status == "blocked"
    assert state.last_tool_name == PIPELINE_DEADLINE_TOOL_NAME
    assert "pipeline_timeout" in (state.last_error or "")


@pytest.mark.asyncio
async def test_fresh_created_session_persists_deadline_before_interview(tmp_path) -> None:
    """The CREATED → INTERVIEW deadline must be saved BEFORE the driver runs (#790 review-5).

    If the process crashes during the first ``interview_driver.run()`` call
    on a fresh session, the persisted state must already carry the armed
    deadline. Otherwise ``from_dict()`` would arm a brand-new 2h deadline
    on resume — silently extending the pipeline past the user-requested
    timeout and breaking the "preserved across process restarts" contract.
    """
    captured: dict[str, AutoPipelineState] = {}

    class _CrashingDriver:
        """Driver that records the persisted state then raises mid-run."""

        def __init__(self) -> None:
            self.invocations = 0
            self.progress_callback = None

        async def run(self, state, ledger):  # noqa: ARG002
            self.invocations += 1
            store = AutoStore(tmp_path)
            captured["loaded"] = store.load(state.auto_session_id)
            raise RuntimeError("simulated crash during backend.start()")

    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    store.save(state)
    assert state.deadline_at is None
    assert state.deadline_at_epoch is None

    driver = _CrashingDriver()
    pipeline = AutoPipeline(driver, _unused_seed_generator, store=store)

    with pytest.raises(RuntimeError, match="simulated crash"):
        await pipeline.run(state)

    assert driver.invocations == 1
    persisted = captured["loaded"]
    assert persisted.deadline_at is not None
    assert persisted.deadline_at_epoch is not None
    assert persisted.deadline_at_epoch > time.time()


@pytest.mark.asyncio
async def test_legacy_blocked_session_arms_deadline_on_recovery(tmp_path) -> None:
    """Resuming a legacy BLOCKED session must arm the missing deadline (#790 review-4).

    Pre-#779 sessions stored as ``BLOCKED`` had no ``deadline_at_epoch``.
    ``from_dict()`` leaves both deadline fields ``None`` for terminal phases
    so the load is byte-for-byte stable, but ``pipeline.run()`` then
    recovers the state to a working phase and would otherwise execute the
    rest of the resume with no top-level timeout. After recovery the
    deadline must be armed so ``_enforce_deadline`` is not a silent no-op.
    """
    captured: dict[str, AutoPipelineState] = {}

    class _CapturingDriver:
        def __init__(self) -> None:
            self.invocations = 0
            self.progress_callback = None

        async def run(self, state, ledger):  # noqa: ARG002
            self.invocations += 1
            captured["state"] = state
            return AutoInterviewResult(
                status="needs_input",
                session_id="interview-stub",
                ledger=ledger,
                rounds=0,
            )

    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "starting interview")
    state.mark_blocked("interview.start failed", tool_name="interview.start")
    payload = state.to_dict()
    payload.pop("deadline_at", None)
    payload.pop("deadline_at_epoch", None)

    legacy = AutoPipelineState.from_dict(payload)
    # Confirm the legacy precondition the bot's blocker calls out: terminal
    # phase + no persisted deadline ⇒ ``from_dict()`` leaves the fields None.
    assert legacy.phase is AutoPhase.BLOCKED
    assert legacy.deadline_at is None
    assert legacy.deadline_at_epoch is None

    driver = _CapturingDriver()
    pipeline = AutoPipeline(driver, _unused_seed_generator, store=store)

    await pipeline.run(legacy)

    # Recovery happened — phase walked back to INTERVIEW — and the deadline
    # is now armed for both monotonic and epoch companions.
    assert driver.invocations == 1
    armed = captured["state"]
    assert armed.deadline_at is not None
    assert armed.deadline_at_epoch is not None
    assert armed.deadline_at > time.monotonic()
    assert armed.deadline_at_epoch > time.time()


@pytest.mark.asyncio
async def test_resume_after_deadline_expired_immediately_blocks(tmp_path) -> None:
    """Loading a state whose deadline already passed must transition to BLOCKED on entry."""
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.pipeline_timeout_seconds = 60.0
    # Persist a deadline that has already expired in epoch terms; the load
    # path will derive a monotonic ``deadline_at`` that is also already in
    # the past.
    expired_epoch = time.time() - 30.0
    state.deadline_at_epoch = expired_epoch
    state.deadline_at = time.monotonic() - 30.0
    state.transition(AutoPhase.INTERVIEW, "starting interview")
    store.save(state)

    loaded = store.load(state.auto_session_id)
    assert loaded.is_deadline_expired()

    driver = _NeverInterviewDriver()
    pipeline = AutoPipeline(driver, _unused_seed_generator, store=store)

    result = await pipeline.run(loaded)

    assert result.status == "blocked"
    assert loaded.last_tool_name == PIPELINE_DEADLINE_TOOL_NAME
    assert loaded.last_error == "pipeline_timeout (deadline expired before resume)"
    assert driver.invocations == 0


@pytest.mark.asyncio
async def test_legacy_non_created_resume_with_missing_deadline_gets_armed(tmp_path) -> None:
    """Legacy states past CREATED must not bypass the top-level deadline forever."""
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "legacy interview checkpoint")
    state.last_tool_name = "unknown_legacy_tool"
    state.mark_blocked("legacy blocked checkpoint", tool_name="unknown_legacy_tool")
    assert state.deadline_at is None
    assert state.deadline_at_epoch is None

    pipeline = AutoPipeline(_NeverInterviewDriver(), _unused_seed_generator)

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.last_error == "legacy blocked checkpoint"
    assert state.deadline_at is not None
    assert state.deadline_at_epoch is not None


def test_arm_deadline_is_idempotent() -> None:
    """Re-calling ``arm_deadline()`` must not silently shift the absolute target."""
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    state.pipeline_timeout_seconds = 300.0
    state.arm_deadline()
    first_mono = state.deadline_at
    first_epoch = state.deadline_at_epoch
    assert first_mono is not None
    assert first_epoch is not None

    # Sleep a tiny bit so any silent re-arm would be visible.
    time.sleep(0.01)
    state.arm_deadline()

    assert state.deadline_at == first_mono
    assert state.deadline_at_epoch == first_epoch


def test_default_pipeline_timeout_seconds_is_two_hours() -> None:
    """The contract default in the issue is 7200s (2h) — guard against drift."""
    assert DEFAULT_PIPELINE_TIMEOUT_SECONDS == 7200.0


def test_cli_rejects_timeout_below_floor(tmp_path) -> None:
    """``--timeout 30`` is below the 60s floor and must be rejected."""
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["auto", "Build a CLI", "--timeout", "30"],
        env={"HOME": str(tmp_path)},
    )
    assert result.exit_code != 0
    assert "--timeout must be between" in result.output


def test_cli_rejects_timeout_above_ceiling(tmp_path) -> None:
    """``--timeout 100000`` exceeds 86400s ceiling and must be rejected."""
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["auto", "Build a CLI", "--timeout", "100000"],
        env={"HOME": str(tmp_path)},
    )
    assert result.exit_code != 0
    assert "--timeout must be between" in result.output


@pytest.mark.asyncio
async def test_mcp_handler_rejects_timeout_below_floor() -> None:
    """The MCP handler rejects ``pipeline_timeout_seconds`` below the 60s floor."""
    from ouroboros.mcp.tools.auto_handler import AutoHandler

    handler = AutoHandler()
    outcome = await handler.handle({"goal": "Build a CLI", "pipeline_timeout_seconds": 30})
    assert outcome.is_err
    assert "pipeline_timeout_seconds must be between" in str(outcome.error)


@pytest.mark.asyncio
async def test_mcp_handler_rejects_timeout_above_ceiling() -> None:
    """The MCP handler rejects ``pipeline_timeout_seconds`` above the 86400s ceiling."""
    from ouroboros.mcp.tools.auto_handler import AutoHandler

    handler = AutoHandler()
    outcome = await handler.handle({"goal": "Build a CLI", "pipeline_timeout_seconds": 100000})
    assert outcome.is_err
    assert "pipeline_timeout_seconds must be between" in str(outcome.error)
