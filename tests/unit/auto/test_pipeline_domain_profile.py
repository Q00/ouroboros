"""Tests for AutoPipeline active domain profile wiring."""

from __future__ import annotations

import pytest

from ouroboros.auto.answerer import AutoAnswerer
from ouroboros.auto.interview_driver import AutoInterviewResult
from ouroboros.auto.pipeline import AutoPipeline, _apply_active_profile
from ouroboros.auto.state import AutoPipelineState


def test_apply_active_profile_preserves_safety_hatch_when_none() -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    answerer = AutoAnswerer(active_profile=object())  # type: ignore[arg-type]

    _apply_active_profile(state, answerer)

    assert answerer.active_profile is None


def test_apply_active_profile_rejects_unknown_durable_profile_name() -> None:
    state = AutoPipelineState(
        goal="Build a CLI",
        cwd="/tmp/project",
        active_domain_profile_name="missing-profile",
    )
    answerer = AutoAnswerer()

    with pytest.raises(
        ValueError, match="active domain profile is not registered: missing-profile"
    ):
        _apply_active_profile(state, answerer)


class _DriverWithAnswerer:
    def __init__(self) -> None:
        self.answerer = AutoAnswerer()
        self.progress_callback = None
        self.invocations = 0

    async def run(self, state, ledger):  # noqa: ANN001
        self.invocations += 1
        return AutoInterviewResult(
            status="seed_ready",
            session_id="interview_should_not_run",
            ledger=ledger,
            rounds=1,
        )


async def _unused_seed_generator(_session_id: str):  # pragma: no cover
    raise AssertionError("seed generator should not run for invalid active profile")


@pytest.mark.asyncio
async def test_pipeline_blocks_cleanly_when_durable_profile_is_missing() -> None:
    state = AutoPipelineState(
        goal="Build a CLI",
        cwd="/tmp/project",
        active_domain_profile_name="missing-profile",
    )
    driver = _DriverWithAnswerer()
    pipeline = AutoPipeline(driver, _unused_seed_generator)

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.last_tool_name == "domain_profile_registry"
    assert state.last_error == "active domain profile is not registered: missing-profile"
    assert result.blocker == state.last_error
    assert driver.invocations == 0
