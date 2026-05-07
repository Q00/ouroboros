"""Tests for the auto-pipeline blocker attribution helper (#690)."""

from __future__ import annotations

import pytest

from ouroboros.auto.blocker_attribution import authoring_backend_label, label_blocker
from ouroboros.auto.state import AutoPipelineState


def _state(runtime: str | None, opencode_mode: str | None = None) -> AutoPipelineState:
    state = AutoPipelineState(goal="goal", cwd="/tmp")
    state.runtime_backend = runtime
    state.opencode_mode = opencode_mode
    return state


@pytest.mark.parametrize(
    "runtime,mode,expected",
    [
        ("claude", None, "in-process (claude)"),
        ("codex", None, "in-process (codex)"),
        ("hermes", None, "in-process (hermes)"),
        ("gemini", None, "in-process (gemini)"),
        ("kiro", None, "in-process (kiro)"),
        ("opencode", "subprocess", "in-process (opencode)"),
        ("opencode", None, "in-process (opencode)"),
        ("opencode", "", "in-process (opencode)"),
        (None, None, "in-process (unspecified)"),
        ("opencode", "plugin", "dispatched (opencode bridge plugin)"),
        ("opencode_cli", "plugin", "dispatched (opencode bridge plugin)"),
        ("OpenCode", "PLUGIN", "dispatched (opencode bridge plugin)"),
    ],
)
def test_authoring_backend_label_truth_table(
    runtime: str | None, mode: str | None, expected: str
) -> None:
    assert authoring_backend_label(_state(runtime, mode)) == expected


def test_label_blocker_appends_phase_and_backend() -> None:
    state = _state("codex")
    out = label_blocker(state, "interview.start timed out after 60s", phase="interview.start")
    assert out == (
        "interview.start timed out after 60s "
        "[phase=interview.start, authoring_backend=in-process (codex)]"
    )


def test_label_blocker_is_idempotent() -> None:
    state = _state("codex")
    once = label_blocker(state, "boom", phase="interview.start")
    twice = label_blocker(state, once, phase="interview.start")
    assert once == twice


def test_label_blocker_handles_none_message() -> None:
    state = _state("codex")
    out = label_blocker(state, None, phase="seed_generator")
    assert out.startswith(" [phase=seed_generator")
    assert "in-process (codex)" in out


def test_label_blocker_marks_dispatched_for_opencode_plugin() -> None:
    state = _state("opencode", "plugin")
    out = label_blocker(state, "interview.start timed out", phase="interview.start")
    assert "dispatched (opencode bridge plugin)" in out
