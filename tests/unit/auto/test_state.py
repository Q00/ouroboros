from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore


def test_state_transition_and_stale_detection() -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    state.transition(AutoPhase.INTERVIEW, "starting interview")

    assert state.phase == AutoPhase.INTERVIEW
    assert state.last_progress_message == "starting interview"

    future = datetime.fromisoformat(state.last_progress_at) + timedelta(seconds=121)
    assert state.is_stale(future)


def test_invalid_phase_transition_rejected() -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")

    with pytest.raises(ValueError, match="Invalid auto phase transition"):
        state.transition(AutoPhase.RUN, "skip ahead")


def test_store_roundtrip_and_corrupt_state(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    state.transition(AutoPhase.INTERVIEW, "starting interview")

    path = store.save(state)
    loaded = store.load(state.auto_session_id)

    assert path.exists()
    assert loaded.auto_session_id == state.auto_session_id
    assert loaded.phase == AutoPhase.INTERVIEW

    path.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt"):
        store.load(state.auto_session_id)


def test_terminal_state_is_not_stale() -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    state.transition(AutoPhase.INTERVIEW, "starting")
    state.transition(AutoPhase.BLOCKED, "need credential", error="need credential")

    future = datetime.now(UTC) + timedelta(days=1)
    assert not state.is_stale(future)


def test_run_phase_uses_run_timeout_key_for_staleness() -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    state.transition(AutoPhase.INTERVIEW, "starting")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")

    future = datetime.fromisoformat(state.last_progress_at) + timedelta(seconds=61)
    assert state.timeout_seconds_by_phase[AutoPhase.RUN.value] == 60
    assert state.is_stale(future)
