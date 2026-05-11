"""Tests for AutoPipeline active domain profile wiring."""

from __future__ import annotations

import pytest

from ouroboros.auto.answerer import AutoAnswerer
from ouroboros.auto.pipeline import _apply_active_profile
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
