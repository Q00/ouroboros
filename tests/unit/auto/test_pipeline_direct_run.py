"""Pipeline routing tests for the direct operational path (#689 PR-C).

These tests exercise the routing decision in isolation: the pipeline
either keeps the existing interview-first flow or short-circuits to a
``BLOCKED`` phase with explicit "PR-D/E pending" guidance.  The
operational executor itself is wired in PR-D/E, so a successful
direct path here means a recognizable BLOCKED message — not a green
run.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from ouroboros.auto.goal_classifier import classify_goal
from ouroboros.auto.interview_driver import AutoInterviewDriver, AutoInterviewResult
from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.auto.pipeline import AutoPipeline
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.core.seed import Seed


class _StubBackend:
    async def start(self, goal: str, *, cwd: str):  # pragma: no cover - never invoked
        msg = "interview backend should not be invoked when direct path is taken"
        raise AssertionError(msg)

    async def answer(self, session_id: str, answer: str):  # pragma: no cover
        raise AssertionError("answer must not be called")

    async def resume(self, session_id: str):  # pragma: no cover
        raise AssertionError("resume must not be called")


@dataclass(slots=True)
class _RecordingDriver:
    """Driver double that records whether interview was attempted."""

    called: bool = False

    async def run(self, state: AutoPipelineState, ledger: SeedDraftLedger) -> AutoInterviewResult:
        self.called = True
        return AutoInterviewResult(
            status="blocked",
            session_id=None,
            ledger=ledger,
            rounds=0,
            blocker="interview should not be reached in direct-path tests",
        )


def _make_pipeline(
    *,
    operational_env_override: bool | None,
) -> tuple[AutoPipeline, _RecordingDriver]:
    driver = _RecordingDriver()
    seed_calls: list[str] = []

    async def _seed_generator(session_id: str) -> Seed:  # pragma: no cover
        seed_calls.append(session_id)
        msg = "seed_generator must not be called when direct path blocks early"
        raise AssertionError(msg)

    pipeline = AutoPipeline(
        interview_driver=driver,  # type: ignore[arg-type]
        seed_generator=_seed_generator,
        operational_env_override=operational_env_override,
    )
    return pipeline, driver


def _operational_goal() -> str:
    return "https://github.com/Q00/ouroboros/pull/689 review please"


def _idea_goal() -> str:
    return "Build a CLI tool that tracks daily habits"


def test_strategy_always_keeps_interview_path_for_operational_goal(tmp_path) -> None:
    pipeline, driver = _make_pipeline(operational_env_override=True)
    state = AutoPipelineState(goal=_operational_goal(), cwd=str(tmp_path))
    state.interview_strategy = "always"
    asyncio.run(pipeline.run(state))

    assert driver.called is True, "strategy=always must reach the interview driver"
    # The driver double returns blocked, but the pipeline did consult it.
    assert state.classification is not None
    assert state.classification["direct_run_allowed"] is True
    assert state.direct_path_reason == state.classification["reason"]


def test_strategy_auto_with_env_blocks_with_pending_executor_guidance(tmp_path) -> None:
    pipeline, driver = _make_pipeline(operational_env_override=True)
    state = AutoPipelineState(goal=_operational_goal(), cwd=str(tmp_path))
    state.interview_strategy = "auto"

    asyncio.run(pipeline.run(state))

    assert driver.called is False
    assert state.phase is AutoPhase.BLOCKED
    assert "PR-D/E" in (state.last_error or "")
    assert state.last_tool_name == "goal_classifier"


def test_strategy_auto_without_env_keeps_interview_path(tmp_path) -> None:
    pipeline, driver = _make_pipeline(operational_env_override=False)
    state = AutoPipelineState(goal=_operational_goal(), cwd=str(tmp_path))
    state.interview_strategy = "auto"

    asyncio.run(pipeline.run(state))

    assert driver.called is True
    # Even without env enabled, classification is still recorded so the
    # CLI can surface why the direct path was not taken.
    assert state.classification is not None
    assert state.classification["direct_run_allowed"] is True


def test_strategy_never_with_eligible_goal_blocks_pending_executor(tmp_path) -> None:
    pipeline, driver = _make_pipeline(operational_env_override=False)
    state = AutoPipelineState(goal=_operational_goal(), cwd=str(tmp_path))
    state.interview_strategy = "never"

    asyncio.run(pipeline.run(state))

    assert driver.called is False
    assert state.phase is AutoPhase.BLOCKED
    assert "PR-D/E" in (state.last_error or "")


def test_strategy_never_with_idea_goal_blocks_with_actionable_message(tmp_path) -> None:
    pipeline, driver = _make_pipeline(operational_env_override=True)
    state = AutoPipelineState(goal=_idea_goal(), cwd=str(tmp_path))
    state.interview_strategy = "never"

    asyncio.run(pipeline.run(state))

    assert driver.called is False
    assert state.phase is AutoPhase.BLOCKED
    assert "interview-strategy" in (state.last_error or "").lower()
    assert state.classification is not None
    assert state.classification["direct_run_allowed"] is False


def test_classification_persists_on_resume(tmp_path) -> None:
    pipeline, driver = _make_pipeline(operational_env_override=True)
    store = AutoStore(tmp_path)
    pipeline.store = store
    state = AutoPipelineState(goal=_operational_goal(), cwd=str(tmp_path))
    state.interview_strategy = "auto"

    asyncio.run(pipeline.run(state))

    reloaded = store.load(state.auto_session_id)
    assert reloaded.classification is not None
    assert reloaded.classification == classify_goal(_operational_goal()).to_dict()
    assert reloaded.direct_path_reason == reloaded.classification["reason"]
    # Ledger stores the same explanation for resume.
    assert reloaded.ledger
    ledger = SeedDraftLedger.from_dict(reloaded.ledger)
    assert ledger.direct_path_reason == reloaded.direct_path_reason


def test_resume_does_not_reclassify_after_goal_drift(tmp_path) -> None:
    """A persisted classification is sticky: tampering with goal must not flip routing."""
    pipeline, _ = _make_pipeline(operational_env_override=True)
    store = AutoStore(tmp_path)
    pipeline.store = store
    state = AutoPipelineState(goal=_operational_goal(), cwd=str(tmp_path))
    state.interview_strategy = "always"

    asyncio.run(pipeline.run(state))

    # Mutate goal field on the persisted state and rerun.  The classifier
    # must NOT be reconsulted for a sticky classification — operators
    # must not be able to flip routing after the fact.
    reloaded = store.load(state.auto_session_id)
    reloaded.goal = "Build a CLI"
    classification_before = dict(reloaded.classification or {})
    reloaded.transition(AutoPhase.BLOCKED, "force re-entry")
    reloaded.recover(AutoPhase.INTERVIEW, "resume after manual drift")
    # Use a fresh pipeline that would have classified differently if it
    # were free to do so:
    pipeline_fresh, _ = _make_pipeline(operational_env_override=True)
    pipeline_fresh.store = store
    asyncio.run(pipeline_fresh.run(reloaded))

    assert reloaded.classification == classification_before
