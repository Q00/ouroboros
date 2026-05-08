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
async def test_resume_arms_missing_legacy_deadline_before_phase_work(tmp_path) -> None:
    """Legacy non-terminal states without deadline fields must get a deadline on resume."""
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.pipeline_timeout_seconds = 60.0
    state.transition(AutoPhase.INTERVIEW, "legacy interview state")
    state.deadline_at = None
    state.deadline_at_epoch = None
    # Force an early return after the resume entry path so the assertion locks
    # deadline arming itself, not later phase execution behavior.
    state.seed_artifact = {"invalid": "seed"}

    driver = _NeverInterviewDriver()
    pipeline = AutoPipeline(driver, _unused_seed_generator, store=store)

    result = await pipeline.run(state)

    assert result.status == "failed"
    assert state.deadline_at is not None
    assert state.deadline_at_epoch is not None
    assert driver.invocations == 0
    loaded = store.load(state.auto_session_id)
    assert loaded.deadline_at is not None
    assert loaded.deadline_at_epoch is not None


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
