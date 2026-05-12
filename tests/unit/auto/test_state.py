from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ouroboros.auto.state import (
    DEFAULT_TIMEOUT_SECONDS_BY_PHASE,
    AutoPhase,
    AutoPipelineState,
    AutoResumeCapability,
    AutoStore,
)


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


def test_phase_timeout_seconds_returns_persisted_value() -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    state.timeout_seconds_by_phase[AutoPhase.INTERVIEW.value] = 175

    assert state.phase_timeout_seconds(AutoPhase.INTERVIEW) == 175.0


def test_phase_timeout_seconds_falls_back_to_canonical_default() -> None:
    """A missing or unusable entry must fall back to the dataclass default,
    not silently halve the operator's budget."""
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    state.timeout_seconds_by_phase.pop(AutoPhase.INTERVIEW.value)

    expected = float(DEFAULT_TIMEOUT_SECONDS_BY_PHASE[AutoPhase.INTERVIEW.value])
    assert expected == 120.0
    assert state.phase_timeout_seconds(AutoPhase.INTERVIEW) == expected


def test_phase_timeout_seconds_rejects_non_positive_persisted_value() -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    state.timeout_seconds_by_phase[AutoPhase.INTERVIEW.value] = 0

    assert state.phase_timeout_seconds(AutoPhase.INTERVIEW) == float(
        DEFAULT_TIMEOUT_SECONDS_BY_PHASE[AutoPhase.INTERVIEW.value]
    )


def test_default_policy_matches_dataclass_default() -> None:
    """Guards against drift between the module constant and dataclass default."""
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")

    assert state.timeout_seconds_by_phase == DEFAULT_TIMEOUT_SECONDS_BY_PHASE
    assert state.timeout_seconds_by_phase is not DEFAULT_TIMEOUT_SECONDS_BY_PHASE


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
        ("ralph_opencode_mode", []),
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


# ---------------------------------------------------------------------------
# resume_capability matrix (#688)
# ---------------------------------------------------------------------------


def _state(**overrides: object) -> AutoPipelineState:
    """Build an :class:`AutoPipelineState` with sensible defaults for matrix tests."""
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def test_resume_capability_complete_returns_none() -> None:
    state = _state()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.COMPLETE, "done")

    assert state.resume_capability() is AutoResumeCapability.NONE


def test_resume_capability_non_terminal_returns_resume() -> None:
    state = _state()
    state.transition(AutoPhase.INTERVIEW, "interview")

    assert state.resume_capability() is AutoResumeCapability.RESUME


def test_resume_capability_repair_phase_returns_resume() -> None:
    """Critic fix C2: REPAIR is non-terminal — resume transitions back to REVIEW."""
    state = _state()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.REPAIR, "repair")

    assert state.phase is AutoPhase.REPAIR
    assert state.resume_capability() is AutoResumeCapability.RESUME


def test_resume_capability_blocked_unmapped_tool_returns_none() -> None:
    state = _state()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.mark_blocked("internal guard fired", tool_name="auto_pipeline")

    assert state.resume_capability() is AutoResumeCapability.NONE


def test_resume_capability_interview_start_timeout_no_session_returns_retry() -> None:
    """The #688 case: interview.start blew up before persisting a session id."""
    state = _state()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.mark_blocked("interview.start timed out", tool_name="interview.start")

    assert state.interview_session_id is None
    assert state.resume_capability() is AutoResumeCapability.RETRY


def test_resume_capability_interview_start_with_session_returns_partial() -> None:
    state = _state()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_session_id = "interview_1"
    state.mark_blocked("interview.start blocked late", tool_name="interview.start")

    assert state.resume_capability() is AutoResumeCapability.PARTIAL_RESUME


def test_resume_capability_interview_resume_with_pending_returns_resume() -> None:
    state = _state()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_session_id = "interview_1"
    state.pending_question = "What is the deadline?"
    state.mark_blocked("interview.resume timed out", tool_name="interview.resume")

    assert state.resume_capability() is AutoResumeCapability.RESUME


def test_resume_capability_interview_answer_no_pending_returns_partial() -> None:
    state = _state()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_session_id = "interview_1"
    state.pending_question = None
    state.mark_blocked("interview.answer timed out", tool_name="interview.answer")

    assert state.resume_capability() is AutoResumeCapability.PARTIAL_RESUME


def test_resume_capability_auto_answerer_with_pending_returns_resume() -> None:
    state = _state()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_session_id = "interview_1"
    state.pending_question = "What is the deadline?"
    state.mark_blocked("auto_answerer timed out", tool_name="auto_answerer")

    assert state.resume_capability() is AutoResumeCapability.RESUME


def test_resume_capability_seed_generator_with_artifact_returns_resume() -> None:
    state = _state()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.seed_artifact = {"id": "seed_1"}
    state.mark_blocked("seed_generator timed out", tool_name="seed_generator")

    assert state.resume_capability() is AutoResumeCapability.RESUME


def test_resume_capability_seed_generator_seed_path_only_returns_partial() -> None:
    """Critic fix C3: ``seed_path``-only must return PARTIAL_RESUME."""
    state = _state()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.seed_path = "/tmp/seed.yaml"
    state.mark_blocked("seed_generator timed out", tool_name="seed_generator")

    assert not state.seed_artifact
    assert state.seed_path
    assert state.resume_capability() is AutoResumeCapability.PARTIAL_RESUME


def test_resume_capability_seed_generator_no_artifact_with_session_returns_retry() -> None:
    state = _state()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_session_id = "interview_1"
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.mark_blocked("seed_generator timed out", tool_name="seed_generator")

    assert not state.seed_artifact
    assert state.seed_path is None
    # Interview session carries forward, but seed generation itself re-runs
    # from scratch — RETRY semantics, not RESUME.
    assert state.resume_capability() is AutoResumeCapability.RETRY


def test_resume_capability_seed_generator_no_artifact_no_session_returns_none() -> None:
    state = _state()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.mark_blocked("seed_generator timed out", tool_name="seed_generator")

    assert state.interview_session_id is None
    assert state.resume_capability() is AutoResumeCapability.NONE


def test_resume_capability_grade_gate_with_artifact_returns_resume() -> None:
    state = _state()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.seed_artifact = {"id": "seed_1"}
    state.mark_blocked("Seed did not reach A-grade", tool_name="grade_gate")

    assert state.resume_capability() is AutoResumeCapability.RESUME


def test_resume_capability_grade_gate_with_seed_path_returns_partial() -> None:
    state = _state()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.seed_path = "/tmp/seed.yaml"
    state.mark_blocked("seed_loader could not read seed", tool_name="seed_loader")

    assert state.resume_capability() is AutoResumeCapability.PARTIAL_RESUME


def test_resume_capability_grade_gate_nothing_returns_none() -> None:
    state = _state()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.mark_blocked("seed_saver could not persist seed", tool_name="seed_saver")

    assert state.resume_capability() is AutoResumeCapability.NONE


def test_resume_capability_run_starter_with_handles_returns_resume() -> None:
    state = _state()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    state.job_id = "job_42"
    state.mark_blocked("run start timed out", tool_name="run_starter")

    assert state.resume_capability() is AutoResumeCapability.RESUME


def test_resume_capability_run_starter_with_artifact_returns_resume() -> None:
    state = _state()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    state.seed_artifact = {"id": "seed_1"}
    state.mark_blocked("run start timed out", tool_name="run_starter")

    assert state.resume_capability() is AutoResumeCapability.RESUME


def test_resume_capability_run_starter_seed_path_only_returns_partial() -> None:
    """Critic fix C4: ``run_starter`` + ``seed_path`` only → PARTIAL_RESUME."""
    state = _state()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    state.seed_path = "/tmp/seed.yaml"
    state.mark_blocked("run start timed out", tool_name="run_starter")

    assert not state.seed_artifact
    assert not any((state.job_id, state.execution_id, state.run_session_id))
    assert state.resume_capability() is AutoResumeCapability.PARTIAL_RESUME


def test_resume_capability_run_starter_attempted_without_handle_returns_none() -> None:
    """Bot finding (#724): a run_starter attempt that left no durable handle is
    NOT recoverable. ``AutoPipeline.run()`` short-circuits at
    ``state.run_start_attempted`` to refuse a duplicate execution, so
    ``--resume`` cannot make progress and capability must be NONE.
    """
    state = _state()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    state.seed_artifact = {"id": "seed_1"}  # would otherwise classify as RESUME
    state.run_start_attempted = True
    state.mark_blocked("run start timed out", tool_name="run_starter")

    assert state.run_start_attempted is True
    assert not any((state.job_id, state.execution_id, state.run_session_id))
    # Despite seed_artifact being present, the duplicate-execution guard means
    # --resume will immediately re-block. Classify as NONE.
    assert state.resume_capability() is AutoResumeCapability.NONE


def test_resume_capability_run_starter_attempted_seed_path_only_returns_none() -> None:
    """Same guard applies even when only seed_path is available."""
    state = _state()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    state.seed_path = "/tmp/seed.yaml"
    state.run_start_attempted = True
    state.mark_blocked("run start timed out", tool_name="run_starter")

    assert state.run_start_attempted is True
    assert not state.seed_artifact
    assert state.resume_capability() is AutoResumeCapability.NONE


def test_resume_capability_failed_mirrors_blocked() -> None:
    """FAILED states classify identically to BLOCKED for the same signals."""
    state = _state()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.mark_failed("interview.start timed out", tool_name="interview.start")

    assert state.phase is AutoPhase.FAILED
    assert state.interview_session_id is None
    assert state.resume_capability() is AutoResumeCapability.RETRY
