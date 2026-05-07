"""Tests for the auto-pipeline authoring-backend attribution module (#690)."""

from __future__ import annotations

import pytest

from ouroboros.auto.blocker_attribution import (
    authoring_backend_label,
    record_authoring_backend,
)
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
        ("copilot", None, "in-process (copilot)"),
        ("opencode", "subprocess", "in-process (opencode)"),
        ("opencode", None, "in-process (opencode)"),
        ("opencode", "", "in-process (opencode)"),
        (None, None, "in-process (unspecified)"),
        # Persisted plugin in state must still report in-process: both
        # auto entry points demote plugin → subprocess for authoring
        # handlers before constructing them, so the label must reflect
        # the effective handler config, not the raw persisted mode.
        ("opencode", "plugin", "in-process (opencode)"),
        ("opencode_cli", "plugin", "in-process (opencode_cli)"),
    ],
)
def test_authoring_backend_label_truth_table(
    runtime: str | None, mode: str | None, expected: str
) -> None:
    assert authoring_backend_label(_state(runtime, mode)) == expected


def test_record_authoring_backend_persists_label_on_state() -> None:
    """Helper writes the resolved backend label onto the state field."""
    state = _state("codex")
    assert state.last_authoring_backend is None

    record_authoring_backend(state)

    assert state.last_authoring_backend == "in-process (codex)"


def test_record_authoring_backend_does_not_touch_other_fields() -> None:
    """Helper only sets last_authoring_backend — never the message text."""
    state = _state("codex")
    state.last_error = "interview.start timed out after 60s for auto_xxx"
    state.last_tool_name = "interview.start"

    record_authoring_backend(state)

    assert state.last_error == "interview.start timed out after 60s for auto_xxx"
    assert state.last_tool_name == "interview.start"
    assert state.last_authoring_backend == "in-process (codex)"


def test_record_authoring_backend_marks_persisted_opencode_plugin_as_in_process() -> None:
    """Persisted plugin in state must still record in-process for authoring.

    Regression guard for #690 review feedback: both auto entry points
    demote plugin → subprocess for authoring handlers, so the recorded
    metadata must reflect the effective handler config (in-process),
    not the raw persisted opencode_mode.
    """
    state = _state("opencode", "plugin")
    record_authoring_backend(state)
    assert state.last_authoring_backend == "in-process (opencode)"
    assert "dispatched" not in state.last_authoring_backend


def test_authoring_backend_state_field_round_trips_through_persistence(tmp_path) -> None:
    """The new metadata field survives ``AutoStore`` save/load."""
    from ouroboros.auto.state import AutoStore

    state = AutoPipelineState(goal="round trip", cwd=str(tmp_path))
    state.runtime_backend = "codex"
    record_authoring_backend(state)
    store = AutoStore(tmp_path)
    store.save(state)

    reloaded = store.load(state.auto_session_id)
    assert reloaded.last_authoring_backend == "in-process (codex)"


def test_legacy_persisted_state_loads_with_default_attribution(tmp_path) -> None:
    """Older auto sessions saved before this field exists must still load."""
    import json

    from ouroboros.auto.state import AutoStore

    state = AutoPipelineState(goal="legacy", cwd=str(tmp_path))
    state.runtime_backend = "codex"
    store = AutoStore(tmp_path)
    store.save(state)

    # Simulate an older persisted file by stripping the new field.
    session_path = tmp_path / f"{state.auto_session_id}.json"
    payload = json.loads(session_path.read_text())
    payload.pop("last_authoring_backend", None)
    session_path.write_text(json.dumps(payload))

    reloaded = store.load(state.auto_session_id)
    assert reloaded.last_authoring_backend is None
