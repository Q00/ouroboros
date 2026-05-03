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


def test_store_load_wraps_semantically_invalid_state(tmp_path) -> None:
    store = AutoStore(tmp_path)
    path = store.path_for("auto_badstate")
    tmp_path.mkdir(parents=True, exist_ok=True)
    path.write_text('{"goal": "x", "cwd": ".", "phase": "bogus"}', encoding="utf-8")

    with pytest.raises(ValueError, match="Auto session state is invalid"):
        store.load("auto_badstate")


def test_store_load_wraps_invalid_timestamps_and_timeouts(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    data = state.to_dict()
    data["last_progress_at"] = "not-a-timestamp"
    path = store.path_for(state.auto_session_id)
    path.write_text(__import__("json").dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="Auto session state is invalid"):
        store.load(state.auto_session_id)

    data = state.to_dict()
    data["timeout_seconds_by_phase"] = {AutoPhase.RUN.value: "sixty"}
    path.write_text(__import__("json").dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="Auto session state is invalid"):
        store.load(state.auto_session_id)


def test_store_load_wraps_naive_timestamps(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    data = state.to_dict()
    data["last_progress_at"] = "2026-05-01T12:00:00"
    path = store.path_for(state.auto_session_id)
    path.write_text(__import__("json").dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="Auto session state is invalid"):
        store.load(state.auto_session_id)


def test_store_load_wraps_malformed_container_and_counter_fields(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    path = store.path_for(state.auto_session_id)

    for field_name, value in (
        ("ledger", []),
        ("findings", "oops"),
        ("repair_round", "1"),
        ("current_round", -1),
    ):
        data = state.to_dict()
        data[field_name] = value
        path.write_text(__import__("json").dumps(data), encoding="utf-8")

        with pytest.raises(ValueError, match="Auto session state is invalid"):
            store.load(state.auto_session_id)


def test_store_load_rejects_malformed_nested_ledger(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    data = state.to_dict()
    data["ledger"] = {
        "sections": {
            "goal": {
                "name": "goal",
                "entries": [
                    {
                        "key": "goal.primary",
                        "value": "Build a CLI",
                        "source": "not-a-source",
                        "confidence": 0.9,
                        "status": "confirmed",
                    }
                ],
            }
        },
        "question_history": [],
    }
    path = store.path_for(state.auto_session_id)
    path.write_text(__import__("json").dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="valid Seed Draft Ledger"):
        store.load(state.auto_session_id)


def test_store_load_rejects_dropped_ledger_sections_and_history(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    data = state.to_dict()
    data["ledger"] = {"sections": {"goal": []}, "question_history": {}}
    path = store.path_for(state.auto_session_id)
    path.write_text(__import__("json").dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="valid Seed Draft Ledger"):
        store.load(state.auto_session_id)


def test_store_load_rejects_ledger_question_history_with_non_qa_entries(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    data = state.to_dict()
    data["ledger"] = {
        "sections": {"goal": {"name": "goal", "entries": []}},
        "question_history": [{"question": "What?"}],
    }
    path = store.path_for(state.auto_session_id)
    path.write_text(__import__("json").dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="valid Seed Draft Ledger"):
        store.load(state.auto_session_id)


def test_store_save_rejects_malformed_nested_ledger_before_writing(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    state.ledger = {
        "sections": {"goal": {"name": "goal", "entries": [{"key": "missing fields"}]}},
        "question_history": [],
    }

    with pytest.raises(ValueError, match="valid Seed Draft Ledger"):
        store.save(state)

    assert not store.path_for(state.auto_session_id).exists()


def test_store_load_rejects_empty_optional_resume_identifiers(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    path = store.path_for(state.auto_session_id)

    for field_name in (
        "interview_session_id",
        "seed_id",
        "seed_path",
        "execution_id",
        "job_id",
        "run_session_id",
        "last_grade",
        "pending_question",
        "last_tool_name",
        "last_error",
    ):
        data = state.to_dict()
        data[field_name] = ""
        path.write_text(__import__("json").dumps(data), encoding="utf-8")

        with pytest.raises(ValueError, match="Auto session state is invalid"):
            store.load(state.auto_session_id)


def test_store_load_wraps_malformed_seed_artifact(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    data = state.to_dict()
    data["seed_artifact"] = {"goal": "missing required seed fields"}
    path = store.path_for(state.auto_session_id)
    path.write_text(__import__("json").dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="Auto session state is invalid"):
        store.load(state.auto_session_id)


def test_store_load_rejects_truncated_state_without_default_backfill(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    data = state.to_dict()
    data.pop("phase_started_at")
    path = store.path_for(state.auto_session_id)
    path.write_text(__import__("json").dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="missing required fields"):
        store.load(state.auto_session_id)


def test_store_load_rejects_session_id_mismatch(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    data = state.to_dict()
    data["auto_session_id"] = "auto_other"
    path = store.path_for(state.auto_session_id)
    path.write_text(__import__("json").dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="session id mismatch"):
        store.load(state.auto_session_id)


def test_store_load_rejects_partial_timeout_map(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    data = state.to_dict()
    data["timeout_seconds_by_phase"] = {AutoPhase.RUN.value: 60}
    path = store.path_for(state.auto_session_id)
    path.write_text(__import__("json").dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="missing required phases"):
        store.load(state.auto_session_id)


def test_store_load_rejects_malformed_optional_strings(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    path = store.path_for(state.auto_session_id)

    for field_name, value in (
        ("seed_path", {"path": "seed.json"}),
        ("seed_id", ""),
        ("execution_id", []),
        ("last_progress_message", []),
    ):
        data = state.to_dict()
        data[field_name] = value
        path.write_text(__import__("json").dumps(data), encoding="utf-8")

        with pytest.raises(ValueError, match="Auto session state is invalid"):
            store.load(state.auto_session_id)


def test_store_save_rejects_invalid_state_before_writing(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    state.timeout_seconds_by_phase = {AutoPhase.RUN.value: 60}

    with pytest.raises(ValueError, match="missing required phases"):
        store.save(state)

    assert not store.path_for(state.auto_session_id).exists()


def test_store_load_rejects_malformed_run_subagent(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    data = state.to_dict()
    data["run_subagent"] = []
    path = store.path_for(state.auto_session_id)
    path.write_text(__import__("json").dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="Auto session state is invalid"):
        store.load(state.auto_session_id)


def test_store_load_rejects_falsey_non_object_seed_artifacts(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    path = store.path_for(state.auto_session_id)

    for value in (None, [], "", 0):
        data = state.to_dict()
        data["seed_artifact"] = value
        path.write_text(__import__("json").dumps(data), encoding="utf-8")

        with pytest.raises(ValueError, match="Auto session state is invalid"):
            store.load(state.auto_session_id)


def test_store_load_rejects_unknown_required_grade(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    data = state.to_dict()
    data["required_grade"] = "D"
    path = store.path_for(state.auto_session_id)
    path.write_text(__import__("json").dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="required_grade"):
        store.load(state.auto_session_id)


def test_recover_rejects_terminal_phase_from_blocked_state() -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd="/repo")
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.mark_blocked("needs user input")

    with pytest.raises(ValueError, match="blocked -> complete"):
        state.recover(AutoPhase.COMPLETE, "do not skip work")

    assert state.phase is AutoPhase.BLOCKED
    assert state.last_error == "needs user input"


def test_recover_uses_transition_table_from_failed_state() -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd="/repo")
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.mark_failed("tool failed")

    state.recover(AutoPhase.REVIEW, "retry review")

    assert state.phase is AutoPhase.REVIEW
    assert state.last_error is None
