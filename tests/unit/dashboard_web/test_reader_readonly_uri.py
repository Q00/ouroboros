"""``_connect_readonly`` must URI-encode the DB path so ``?``/``#`` in a path
can't be misparsed as the SQLite URI's query/fragment."""

from __future__ import annotations

import json
import sqlite3
from statistics import median
import time

import pytest

from ouroboros.dashboard_web import reader as reader_module
from ouroboros.dashboard_web.reader import (
    _RELEVANT_EVENT_TYPES,
    EventTail,
    PickerIndexContractError,
    _connect_readonly,
    list_recent_executions,
)
from ouroboros.orchestrator.events import create_workflow_progress_event
from ouroboros.persistence.picker_indexes import (
    AGGREGATE_EVENT_INDEX,
    DIRECT_EVENT_INDEX,
    PICKER_INDEX_DDL,
    RUNNING_PROGRESS_INDEX,
    START_EVENT_INDEX,
    WORKFLOW_SNAPSHOT_INDEX,
)


def _make_db(path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.execute("INSERT INTO t (v) VALUES ('ok')")
        conn.commit()
    finally:
        conn.close()


def _make_events_db(path, rows: list[tuple[str, str, dict]]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
        conn.execute("CREATE INDEX ix_events_aggregate_id ON events (aggregate_id)")
        conn.execute("CREATE INDEX ix_events_event_type ON events (event_type)")
        _install_picker_indexes(conn)
        conn.executemany(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            [
                (aggregate_id, event_type, json.dumps(payload))
                for aggregate_id, event_type, payload in rows
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _install_picker_indexes(conn: sqlite3.Connection) -> None:
    for statement in PICKER_INDEX_DDL:
        conn.execute(statement)


def test_readonly_connect_handles_question_mark_in_path(tmp_path) -> None:
    # A directory whose name contains ``?`` — the raw path would truncate the URI
    # at the ``?`` and open the wrong (or a new empty) DB.
    weird_dir = tmp_path / "a?b#c"
    weird_dir.mkdir()
    db = weird_dir / "ouroboros.db"
    _make_db(db)

    conn = _connect_readonly(db)
    try:
        assert conn.execute("SELECT v FROM t").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_readonly_connect_is_actually_read_only(tmp_path) -> None:
    db = tmp_path / "plain.db"
    _make_db(db)
    conn = _connect_readonly(db)
    try:
        # mode=ro: any write must fail fast rather than corrupt a live run's DB.
        with_error = False
        try:
            conn.execute("INSERT INTO t (v) VALUES ('nope')")
        except sqlite3.OperationalError:
            with_error = True
        assert with_error
    finally:
        conn.close()


def test_reader_includes_execution_frugality_retrospective() -> None:
    assert "execution.frugality_retrospective.reported" in _RELEVANT_EVENT_TYPES


@pytest.mark.parametrize("wrong_same_name", [False, True])
def test_picker_fails_before_history_reads_without_exact_index_contract(
    tmp_path, monkeypatch, wrong_same_name: bool
) -> None:
    db = tmp_path / f"legacy-{wrong_same_name}.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
        if wrong_same_name:
            for name in (AGGREGATE_EVENT_INDEX, RUNNING_PROGRESS_INDEX, WORKFLOW_SNAPSHOT_INDEX):
                conn.execute(
                    f"CREATE INDEX \"{name}\" ON events (aggregate_id) WHERE event_type = 'other'"
                )
        conn.execute(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            (
                "orch_target",
                "orchestrator.session.started",
                json.dumps({"execution_id": "exec_target"}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    statements: list[str] = []
    vm_steps = 0
    original_connect = reader_module._connect_readonly

    def traced_connect(db_path):
        nonlocal vm_steps
        traced = original_connect(db_path)

        def count_step():
            nonlocal vm_steps
            vm_steps += 1
            return 0

        traced.set_trace_callback(statements.append)
        traced.set_progress_handler(count_step, 1)
        return traced

    monkeypatch.setattr(reader_module, "_connect_readonly", traced_connect)

    with pytest.raises(PickerIndexContractError):
        list_recent_executions(db)

    assert not any("FROM events" in statement for statement in statements)
    assert vm_steps < 500


def test_picker_fails_before_history_read_for_literal_drift_indexes(tmp_path, monkeypatch) -> None:
    db = tmp_path / "literal-drift.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
        for statement in PICKER_INDEX_DDL:
            conn.execute(
                statement.replace("'workflow.progress.updated'", "'workflow.progress. updated'")
            )
        conn.execute(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            (
                "orch_target",
                "orchestrator.session.started",
                json.dumps({"execution_id": "exec_target"}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    statements: list[str] = []
    vm_steps = 0
    original_connect = reader_module._connect_readonly

    def traced_connect(db_path):
        nonlocal vm_steps
        traced = original_connect(db_path)

        def count_step():
            nonlocal vm_steps
            vm_steps += 1
            return 0

        traced.set_trace_callback(statements.append)
        traced.set_progress_handler(count_step, 1)
        return traced

    monkeypatch.setattr(reader_module, "_connect_readonly", traced_connect)

    with pytest.raises(PickerIndexContractError):
        list_recent_executions(db)

    assert not any("FROM events" in statement for statement in statements)
    assert vm_steps < 500


def test_list_recent_executions_preserves_goal_and_reports_concurrent_runs(tmp_path) -> None:
    db = tmp_path / "runs.db"
    _make_events_db(
        db,
        [
            (
                "orch_two",
                "orchestrator.session.started",
                {
                    "execution_id": "exec_two",
                    "seed_goal": "  Keep  leading\nline  ",
                    "start_time": "t2",
                },
            ),
            (
                "exec_two",
                "workflow.progress.updated",
                {
                    "execution_id": "exec_two",
                    "completed_count": 1,
                    "total_count": 2,
                    "current_phase": "Run",
                    "acceptance_criteria": [
                        {"node_id": "two_1", "content": "AC", "status": "completed"},
                        {"node_id": "two_2", "content": "AC 2", "status": "executing"},
                    ],
                },
            ),
            (
                "orch_one",
                "orchestrator.session.started",
                {"execution_id": "exec_one", "seed_goal": "Second goal"},
            ),
        ],
    )

    runs = list_recent_executions(db, limit=10)

    assert [run["execution_id"] for run in runs] == ["exec_one", "exec_two"]
    two = runs[1]
    assert two["goal"] == "  Keep  leading\nline  "
    assert two["status"] == "running"
    assert two["completed_count"] == 1
    assert two["total_count"] == 2
    assert two["executing_count"] == 1


def test_list_recent_executions_preserves_cancelled_terminal_status(tmp_path) -> None:
    db = tmp_path / "cancelled.db"
    _make_events_db(
        db,
        [
            (
                "orch_cancelled",
                "orchestrator.session.started",
                {"execution_id": "exec_cancelled", "seed_goal": "Stop"},
            ),
            (
                "orch_cancelled",
                "workflow.progress.updated",
                {
                    "execution_id": "exec_cancelled",
                    "acceptance_criteria": [
                        {"node_id": "ac_cancelled", "status": "failed"},
                    ],
                },
            ),
            (
                "orch_cancelled",
                "orchestrator.session.cancelled",
                {"execution_id": "exec_cancelled", "reason": "user"},
            ),
        ],
    )

    runs = list_recent_executions(db)

    assert len(runs) == 1
    assert runs[0]["status"] == "cancelled"
    assert runs[0]["failed_count"] == 1


def test_list_recent_executions_preserves_paused_status_and_pause_row(tmp_path) -> None:
    db = tmp_path / "paused.db"
    _make_events_db(
        db,
        [
            (
                "orch_paused",
                "orchestrator.session.started",
                {"execution_id": "exec_paused", "seed_goal": "Resume later"},
            ),
            (
                "orch_paused",
                "workflow.progress.updated",
                {
                    "execution_id": "exec_paused",
                    "acceptance_criteria": [
                        {"node_id": "ac_done", "status": "completed"},
                    ],
                },
            ),
            (
                "orch_paused",
                "orchestrator.session.paused",
                {"execution_id": "exec_paused", "pause_reason": "quota"},
            ),
        ],
    )

    runs = list_recent_executions(db)

    assert len(runs) == 1
    assert runs[0]["status"] == "paused"
    assert runs[0]["last_row"] == 3


def test_list_recent_executions_workflow_last_update_resumes_paused_run(tmp_path) -> None:
    db = tmp_path / "resumed.db"
    resumed = create_workflow_progress_event(
        execution_id="exec_resumed",
        session_id="orch_resumed",
        acceptance_criteria=[{"node_id": "ac_done", "status": "completed"}],
        completed_count=1,
        total_count=1,
        last_update={"runtime_status": "running"},
    )
    _make_events_db(
        db,
        [
            (
                "orch_resumed",
                "orchestrator.session.started",
                {"execution_id": "exec_resumed", "seed_goal": "Continue"},
            ),
            (
                "orch_resumed",
                "orchestrator.session.paused",
                {"execution_id": "exec_resumed"},
            ),
            (resumed.aggregate_id, resumed.type, resumed.data),
        ],
    )

    runs = list_recent_executions(db)

    assert len(runs) == 1
    assert runs[0]["status"] == "running"
    assert runs[0]["last_row"] == 3


def test_list_recent_executions_nested_running_checkpoint_resumes_paused_run(tmp_path) -> None:
    db = tmp_path / "nested-resume.db"
    _make_events_db(
        db,
        [
            (
                "orch_nested_resume",
                "orchestrator.session.started",
                {"execution_id": "exec_nested_resume", "seed_goal": "Continue"},
            ),
            (
                "orch_nested_resume",
                "orchestrator.session.paused",
                {"execution_id": "exec_nested_resume"},
            ),
            (
                "orch_nested_resume",
                "orchestrator.progress.updated",
                {
                    "execution_id": "exec_nested_resume",
                    "progress": {"runtime_status": "running"},
                },
            ),
        ],
    )

    runs = list_recent_executions(db)

    assert len(runs) == 1
    assert runs[0]["status"] == "running"
    assert runs[0]["last_row"] == 3


def test_list_recent_executions_later_running_suppresses_inferred_completion(tmp_path) -> None:
    db = tmp_path / "post-ac-running.db"
    _make_events_db(
        db,
        [
            (
                "orch_post_ac",
                "orchestrator.session.started",
                {"execution_id": "exec_post_ac", "seed_goal": "Finish synthesis"},
            ),
            (
                "orch_post_ac",
                "workflow.progress.updated",
                {
                    "execution_id": "exec_post_ac",
                    "acceptance_criteria": [{"node_id": "ac_done", "status": "completed"}],
                },
            ),
            (
                "orch_post_ac",
                "orchestrator.progress.updated",
                {
                    "execution_id": "exec_post_ac",
                    "progress": {"runtime_status": "running"},
                },
            ),
        ],
    )

    runs = list_recent_executions(db)

    assert len(runs) == 1
    assert runs[0]["status"] == "running"
    assert runs[0]["completed_count"] == 1
    assert runs[0]["last_row"] == 3


def test_list_recent_executions_newer_ac_state_supersedes_older_running(tmp_path) -> None:
    db = tmp_path / "running-before-settled.db"
    _make_events_db(
        db,
        [
            (
                "orch_settled",
                "orchestrator.session.started",
                {"execution_id": "exec_settled"},
            ),
            (
                "orch_settled",
                "orchestrator.progress.updated",
                {
                    "execution_id": "exec_settled",
                    "progress": {"runtime_status": "running"},
                },
            ),
            (
                "orch_settled",
                "workflow.progress.updated",
                {
                    "execution_id": "exec_settled",
                    "acceptance_criteria": [{"node_id": "ac_done", "status": "completed"}],
                },
            ),
        ],
    )

    runs = list_recent_executions(db)

    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
    assert runs[0]["last_row"] == 3


@pytest.mark.parametrize(
    "acceptance_criteria",
    [
        [{"node_id": "ac_failed", "status": "failed"}],
        [
            {"node_id": "ac_done", "status": "completed"},
            {"node_id": "ac_failed", "status": "failed"},
        ],
    ],
)
def test_list_recent_executions_newer_failed_ac_state_supersedes_older_running(
    tmp_path,
    acceptance_criteria: list[dict[str, str]],
) -> None:
    db = tmp_path / "running-before-failed.db"
    _make_events_db(
        db,
        [
            ("orch_failed", "orchestrator.session.started", {"execution_id": "exec_failed"}),
            (
                "orch_failed",
                "orchestrator.progress.updated",
                {
                    "execution_id": "exec_failed",
                    "progress": {"runtime_status": "running"},
                },
            ),
            (
                "orch_failed",
                "workflow.progress.updated",
                {
                    "execution_id": "exec_failed",
                    "acceptance_criteria": acceptance_criteria,
                },
            ),
        ],
    )

    runs = list_recent_executions(db)

    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert runs[0]["failed_count"] == 1
    assert runs[0]["last_row"] == 3


def test_list_recent_executions_newer_running_supersedes_settled_failure(tmp_path) -> None:
    db = tmp_path / "running-after-failed.db"
    _make_events_db(
        db,
        [
            ("orch_retry", "orchestrator.session.started", {"execution_id": "exec_retry"}),
            (
                "orch_retry",
                "orchestrator.progress.updated",
                {"execution_id": "exec_retry", "progress": {"runtime_status": "running"}},
            ),
            (
                "orch_retry",
                "workflow.progress.updated",
                {
                    "execution_id": "exec_retry",
                    "acceptance_criteria": [{"node_id": "ac_failed", "status": "failed"}],
                },
            ),
            (
                "orch_retry",
                "orchestrator.progress.updated",
                {"execution_id": "exec_retry", "progress": {"runtime_status": "running"}},
            ),
        ],
    )

    runs = list_recent_executions(db)

    assert len(runs) == 1
    assert runs[0]["status"] == "running"
    assert runs[0]["failed_count"] == 1
    assert runs[0]["last_row"] == 4


def test_list_recent_executions_keeps_running_evidence_before_newer_turn_completion(
    tmp_path,
) -> None:
    db = tmp_path / "running-before-turn-completed.db"
    _make_events_db(
        db,
        [
            (
                "orch_turn",
                "orchestrator.session.started",
                {"execution_id": "exec_turn"},
            ),
            (
                "orch_turn",
                "workflow.progress.updated",
                {
                    "execution_id": "exec_turn",
                    "acceptance_criteria": [{"node_id": "ac_done", "status": "completed"}],
                },
            ),
            (
                "orch_turn",
                "orchestrator.progress.updated",
                {
                    "execution_id": "exec_turn",
                    "progress": {"runtime_status": "running"},
                },
            ),
            (
                "orch_turn",
                "orchestrator.progress.updated",
                {
                    "execution_id": "exec_turn",
                    "progress": {"runtime_status": "completed"},
                },
            ),
        ],
    )

    runs = list_recent_executions(db)

    assert len(runs) == 1
    assert runs[0]["status"] == "running"
    assert runs[0]["last_row"] == 4


@pytest.mark.parametrize(
    ("terminal_event", "expected"),
    [
        ("orchestrator.session.completed", "completed"),
        ("orchestrator.session.cancelled", "cancelled"),
        ("orchestrator.session.failed", "failed"),
    ],
)
def test_list_recent_executions_terminal_absorbs_running_on_both_sides(
    tmp_path,
    terminal_event: str,
    expected: str,
) -> None:
    db = tmp_path / f"{expected}-absorbs-running.db"
    _make_events_db(
        db,
        [
            ("orch_terminal_running", "orchestrator.session.started", {"execution_id": "exec"}),
            (
                "orch_terminal_running",
                "workflow.progress.updated",
                {
                    "execution_id": "exec",
                    "acceptance_criteria": [{"node_id": "ac", "status": "completed"}],
                },
            ),
            (
                "orch_terminal_running",
                "orchestrator.progress.updated",
                {"execution_id": "exec", "progress": {"runtime_status": "running"}},
            ),
            ("orch_terminal_running", terminal_event, {"execution_id": "exec"}),
            (
                "orch_terminal_running",
                "orchestrator.progress.updated",
                {"execution_id": "exec", "progress": {"runtime_status": "running"}},
            ),
        ],
    )

    runs = list_recent_executions(db)

    assert len(runs) == 1
    assert runs[0]["status"] == expected
    assert runs[0]["last_row"] == 5


def test_list_recent_executions_true_terminal_absorbs_later_resume_noise(tmp_path) -> None:
    db = tmp_path / "terminal-absorbs.db"
    _make_events_db(
        db,
        [
            (
                "orch_absorbed",
                "orchestrator.session.started",
                {"execution_id": "exec_absorbed", "seed_goal": "Finish"},
            ),
            (
                "orch_absorbed",
                "orchestrator.session.completed",
                {"execution_id": "exec_absorbed"},
            ),
            (
                "orch_absorbed",
                "orchestrator.session.paused",
                {"execution_id": "exec_absorbed"},
            ),
            (
                "orch_absorbed",
                "workflow.progress.updated",
                {"execution_id": "exec_absorbed", "runtime_status": "running"},
            ),
        ],
    )

    runs = list_recent_executions(db)

    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
    assert runs[0]["last_row"] == 4


def test_list_recent_executions_turn_completion_cannot_finish_pending_workflow(tmp_path) -> None:
    db = tmp_path / "turn-completed-pending.db"
    pending = create_workflow_progress_event(
        execution_id="exec_pending",
        session_id="orch_pending",
        acceptance_criteria=[{"node_id": "ac_pending", "status": "pending"}],
        completed_count=0,
        total_count=1,
    )
    _make_events_db(
        db,
        [
            (
                "orch_pending",
                "orchestrator.session.started",
                {"execution_id": "exec_pending", "seed_goal": "Keep working"},
            ),
            (pending.aggregate_id, pending.type, pending.data),
            (
                "orch_pending",
                "orchestrator.progress.updated",
                {
                    "execution_id": "exec_pending",
                    "progress": {"runtime_status": "completed"},
                },
            ),
        ],
    )

    runs = list_recent_executions(db)

    assert len(runs) == 1
    assert runs[0]["status"] == "running"
    assert runs[0]["pending_count"] == 1
    assert runs[0]["last_row"] == 3


def test_list_recent_executions_turn_completion_cannot_terminalize_paused_run(tmp_path) -> None:
    db = tmp_path / "turn-completed-paused.db"
    pending = create_workflow_progress_event(
        execution_id="exec_paused_turn",
        session_id="orch_paused_turn",
        acceptance_criteria=[{"node_id": "ac_pending", "status": "pending"}],
        completed_count=0,
        total_count=1,
    )
    _make_events_db(
        db,
        [
            (
                "orch_paused_turn",
                "orchestrator.session.started",
                {"execution_id": "exec_paused_turn", "seed_goal": "Resume later"},
            ),
            (
                "orch_paused_turn",
                "orchestrator.session.paused",
                {"execution_id": "exec_paused_turn"},
            ),
            (pending.aggregate_id, pending.type, pending.data),
            (
                "orch_paused_turn",
                "orchestrator.progress.updated",
                {
                    "execution_id": "exec_paused_turn",
                    "progress": {"runtime_status": "completed"},
                },
            ),
        ],
    )

    runs = list_recent_executions(db)

    assert len(runs) == 1
    assert runs[0]["status"] == "paused"
    assert runs[0]["pending_count"] == 1
    assert runs[0]["last_row"] == 4


def test_list_recent_executions_failed_ac_wins_over_completed_recovery(tmp_path) -> None:
    db = tmp_path / "mixed.db"
    _make_events_db(
        db,
        [
            (
                "orch_mixed",
                "orchestrator.session.started",
                {"execution_id": "exec_mixed", "seed_goal": "Mixed"},
            ),
            (
                "orch_mixed",
                "workflow.progress.updated",
                {
                    "execution_id": "exec_mixed",
                    "acceptance_criteria": [
                        {"node_id": "ac_done", "status": "completed"},
                        {"node_id": "ac_failed", "status": "failed"},
                    ],
                },
            ),
        ],
    )

    runs = list_recent_executions(db)

    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert runs[0]["completed_count"] == 1
    assert runs[0]["failed_count"] == 1


def test_list_recent_executions_failed_terminal_wins_over_completion(tmp_path) -> None:
    db = tmp_path / "terminal-conflict.db"
    _make_events_db(
        db,
        [
            (
                "orch_terminal",
                "orchestrator.session.started",
                {"execution_id": "exec_terminal", "seed_goal": "Recover"},
            ),
            (
                "orch_terminal",
                "orchestrator.session.failed",
                {"execution_id": "exec_terminal", "error": "boom"},
            ),
            (
                "orch_terminal",
                "orchestrator.session.completed",
                {"execution_id": "exec_terminal"},
            ),
        ],
    )

    runs = list_recent_executions(db)

    assert len(runs) == 1
    assert runs[0]["status"] == "failed"


def test_list_recent_executions_unknown_ac_status_stays_running(tmp_path) -> None:
    db = tmp_path / "unknown.db"
    _make_events_db(
        db,
        [
            (
                "orch_unknown",
                "orchestrator.session.started",
                {"execution_id": "exec_unknown", "seed_goal": "Unknown"},
            ),
            (
                "orch_unknown",
                "workflow.progress.updated",
                {
                    "execution_id": "exec_unknown",
                    "acceptance_criteria": [
                        {"node_id": "ac_unknown", "status": "future-status"},
                    ],
                },
            ),
        ],
    )

    runs = list_recent_executions(db)

    assert len(runs) == 1
    assert runs[0]["status"] == "running"
    assert runs[0]["completed_count"] == 0


def test_malformed_unrelated_json_is_ignored_by_picker_and_tail(tmp_path) -> None:
    db = tmp_path / "malformed.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
        _install_picker_indexes(conn)
        conn.executemany(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            [
                (
                    "orch_good",
                    "orchestrator.session.started",
                    json.dumps({"execution_id": "exec_good", "seed_goal": "Still visible"}),
                ),
                ("foreign", "orchestrator.progress.updated", "{not-json"),
                ("foreign-node", "execution.node.updated", "{also-not-json"),
                ("orch_good", "orchestrator.progress.updated", "{local-not-json"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    runs = list_recent_executions(db)
    tail = EventTail(db, "exec_good")

    assert [(run["execution_id"], run["status"]) for run in runs] == [("exec_good", "running")]
    assert [event["event_type"] for event in tail.fetch_new()] == ["orchestrator.session.started"]
    assert tail.fetch_new() == []


def test_picker_limit_applies_after_malformed_start_rows_are_skipped(tmp_path) -> None:
    db = tmp_path / "malformed-starts.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
        _install_picker_indexes(conn)
        conn.execute(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            (
                "orch_visible",
                "orchestrator.session.started",
                json.dumps({"execution_id": "exec_visible", "seed_goal": "Still listed"}),
            ),
        )
        conn.executemany(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            [
                (f"orch_malformed_{index}", "orchestrator.session.started", "{not-json")
                for index in range(10)
            ],
        )
        conn.commit()
    finally:
        conn.close()

    runs = list_recent_executions(db, limit=1)

    assert [(run["execution_id"], run["goal"]) for run in runs] == [
        ("exec_visible", "Still listed")
    ]


def test_picker_limit_counts_distinct_valid_execution_ids(tmp_path) -> None:
    db = tmp_path / "duplicate-starts.db"
    _make_events_db(
        db,
        [
            ("orch_old", "orchestrator.session.started", {"execution_id": "exec_old"}),
            ("orch_new_1", "orchestrator.session.started", {"execution_id": "exec_new"}),
            ("orch_new_2", "orchestrator.session.started", {"execution_id": "exec_new"}),
            ("orch_new_3", "orchestrator.session.started", {"execution_id": "exec_new"}),
        ],
    )

    runs = list_recent_executions(db, limit=2)

    assert [run["execution_id"] for run in runs] == ["exec_new", "exec_old"]


def test_picker_progress_queries_are_aggregate_bounded_at_scale(
    tmp_path,
    monkeypatch,
) -> None:
    db = tmp_path / "progress-scale.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
        conn.execute("CREATE INDEX ix_events_aggregate_id ON events (aggregate_id)")
        conn.execute("CREATE INDEX ix_events_event_type ON events (event_type)")
        _install_picker_indexes(conn)
        foreign_payload = json.dumps(
            {"execution_id": "exec_foreign", "progress": {"runtime_status": "running"}}
        )
        conn.executemany(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            (
                (f"orch_foreign_{index % 100}", "orchestrator.progress.updated", foreign_payload)
                for index in range(100_000)
            ),
        )
        conn.executemany(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            [
                (
                    f"orch_{index}",
                    "orchestrator.session.started",
                    json.dumps({"execution_id": f"exec_{index}"}),
                )
                for index in range(10)
            ],
        )
        conn.commit()
        conn.execute("ANALYZE")
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT rowid FROM events "
            "WHERE aggregate_id = ? AND event_type = 'orchestrator.progress.updated' "
            "ORDER BY rowid DESC LIMIT 1",
            ["orch_9"],
        ).fetchall()
    finally:
        conn.close()

    statements: list[str] = []
    original_connect = reader_module._connect_readonly

    def traced_connect(db_path):
        traced = original_connect(db_path)
        traced.set_trace_callback(statements.append)
        return traced

    monkeypatch.setattr(reader_module, "_connect_readonly", traced_connect)
    started = time.perf_counter()
    runs = list_recent_executions(db, limit=10)
    picker_elapsed = time.perf_counter() - started
    picker_statements = list(statements)

    statements.clear()
    started = time.perf_counter()
    tail_events = EventTail(db, "exec_9").fetch_new()
    tail_elapsed = time.perf_counter() - started
    tail_statements = list(statements)

    progress_queries = [
        statement
        for statement in picker_statements
        if "event_type = 'orchestrator.progress.updated'" in statement
    ]
    direct_tail_query = next(
        statement
        for statement in tail_statements
        if "event_type IN" in statement and "aggregate_id =" in statement
    )
    linked_tail_query = next(
        statement
        for statement in tail_statements
        if "event_type = 'execution.node.created'" in statement
    )
    conn = sqlite3.connect(db)
    try:
        direct_tail_plan = conn.execute("EXPLAIN QUERY PLAN " + direct_tail_query).fetchall()
        linked_tail_plan = conn.execute("EXPLAIN QUERY PLAN " + linked_tail_query).fetchall()
    finally:
        conn.close()

    assert len(runs) == 10
    assert [event["event_type"] for event in tail_events] == ["orchestrator.session.started"]
    assert picker_elapsed < 0.5
    assert tail_elapsed < 0.5
    assert len(progress_queries) == 20
    assert all("aggregate_id =" in statement for statement in progress_queries)
    assert any("ix_events_aggregate_id" in str(row[3]) for row in plan)
    assert all("TEMP B-TREE" not in str(row[3]) for row in plan)
    assert any("ix_events_aggregate_id" in str(row[3]) for row in direct_tail_plan)
    assert any("ix_events_event_type" in str(row[3]) for row in linked_tail_plan)
    assert all("TEMP B-TREE" not in str(row[3]) for row in direct_tail_plan + linked_tail_plan)


def test_workflow_progress_queries_are_aggregate_bounded_at_scale(
    tmp_path,
    monkeypatch,
) -> None:
    db = tmp_path / "workflow-progress-scale.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
        conn.execute("CREATE INDEX ix_events_aggregate_id ON events (aggregate_id)")
        conn.execute("CREATE INDEX ix_events_event_type ON events (event_type)")
        _install_picker_indexes(conn)
        foreign_payload = json.dumps(
            {
                "execution_id": "exec_foreign",
                "acceptance_criteria": [{"node_id": "foreign", "status": "executing"}],
            }
        )
        conn.executemany(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            (
                (f"orch_foreign_{index % 100}", "workflow.progress.updated", foreign_payload)
                for index in range(100_000)
            ),
        )
        conn.executemany(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            [
                (
                    "orch_target",
                    "orchestrator.session.started",
                    json.dumps({"execution_id": "exec_target"}),
                ),
                (
                    "exec_target",
                    "workflow.progress.updated",
                    json.dumps(
                        {
                            "execution_id": "exec_target",
                            "acceptance_criteria": [{"node_id": "target", "status": "completed"}],
                        }
                    ),
                ),
            ],
        )
        conn.commit()
        conn.execute("ANALYZE")
    finally:
        conn.close()

    statements: list[str] = []
    original_connect = reader_module._connect_readonly

    def traced_connect(db_path):
        traced = original_connect(db_path)
        traced.set_trace_callback(statements.append)
        return traced

    monkeypatch.setattr(reader_module, "_connect_readonly", traced_connect)
    started = time.perf_counter()
    runs = list_recent_executions(db, limit=1)
    picker_elapsed = time.perf_counter() - started
    picker_statements = list(statements)

    statements.clear()
    started = time.perf_counter()
    tail_events = EventTail(db, "exec_target").fetch_new()
    tail_elapsed = time.perf_counter() - started
    tail_statements = list(statements)

    picker_workflow_queries = [
        statement
        for statement in picker_statements
        if "event_type = 'workflow.progress.updated'" in statement
    ]
    tail_workflow_queries = [
        statement for statement in tail_statements if "'workflow.progress.updated'" in statement
    ]
    workflow_queries = picker_workflow_queries + tail_workflow_queries
    conn = sqlite3.connect(db)
    try:
        workflow_plans = [
            conn.execute("EXPLAIN QUERY PLAN " + statement).fetchall()
            for statement in workflow_queries
        ]
    finally:
        conn.close()

    assert [(run["execution_id"], run["status"]) for run in runs] == [("exec_target", "completed")]
    assert [event["event_type"] for event in tail_events] == [
        "orchestrator.session.started",
        "workflow.progress.updated",
    ]
    assert picker_elapsed < 0.5
    assert tail_elapsed < 0.5
    assert len(picker_workflow_queries) == 6
    assert len(tail_workflow_queries) == 2
    assert all("aggregate_id =" in statement for statement in workflow_queries)
    assert all("json_extract" not in statement for statement in tail_workflow_queries)
    assert all(
        any(
            "ix_events_aggregate_id" in str(row[3]) or "ix_events_picker_" in str(row[3])
            for row in plan
        )
        for plan in workflow_plans
    )
    assert all("TEMP B-TREE" not in str(row[3]) for plan in workflow_plans for row in plan)


def test_picker_bounds_selected_workflow_progress_history(tmp_path, monkeypatch) -> None:
    db = tmp_path / "selected-workflow-progress-scale.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
        conn.execute("CREATE INDEX ix_events_aggregate_id ON events (aggregate_id)")
        conn.execute("CREATE INDEX ix_events_event_type ON events (event_type)")
        _install_picker_indexes(conn)
        conn.execute(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            (
                "orch_target",
                "orchestrator.session.started",
                json.dumps({"execution_id": "exec_target"}),
            ),
        )
        historical = json.dumps(
            {
                "execution_id": "exec_target",
                "acceptance_criteria": [{"node_id": "target", "status": "executing"}],
            }
        )
        conn.executemany(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            (("exec_target", "workflow.progress.updated", historical) for _ in range(99_999)),
        )
        latest = json.dumps(
            {
                "execution_id": "exec_target",
                "acceptance_criteria": [{"node_id": "target", "status": "completed"}],
            }
        )
        conn.execute(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            ("exec_target", "workflow.progress.updated", latest),
        )
        conn.commit()
        conn.execute("ANALYZE")
    finally:
        conn.close()

    original_connect = reader_module._connect_readonly
    original_decode = reader_module._decode_payload

    # Warm filesystem and SQLite page caches before measuring the picker itself.
    warmup_runs = list_recent_executions(db, limit=1)
    assert [(run["status"], run["completed_count"]) for run in warmup_runs] == [("completed", 1)]

    decode_calls = 0

    def counted_decode(payload):
        nonlocal decode_calls
        decode_calls += 1
        return original_decode(payload)

    monkeypatch.setattr(reader_module, "_decode_payload", counted_decode)
    cpu_samples: list[float] = []
    wall_samples: list[float] = []
    decode_samples: list[int] = []
    runs = []
    for _ in range(3):
        decode_calls = 0
        wall_started = time.perf_counter()
        cpu_started = time.process_time()
        runs = list_recent_executions(db, limit=1)
        cpu_samples.append(time.process_time() - cpu_started)
        wall_samples.append(time.perf_counter() - wall_started)
        decode_samples.append(decode_calls)

    # Count SQLite VM work separately. A Python callback on every opcode is
    # intentionally excluded from the elapsed-time budget: under xdist+coverage
    # that instrumentation measures scheduler/tracer overhead, not picker cost.
    monkeypatch.setattr(reader_module, "_decode_payload", original_decode)
    statements: list[str] = []
    vm_steps = 0

    def traced_connect(db_path):
        traced = original_connect(db_path)

        def count_step():
            nonlocal vm_steps
            vm_steps += 1
            return 0

        traced.set_trace_callback(statements.append)
        traced.set_progress_handler(count_step, 1)
        return traced

    monkeypatch.setattr(reader_module, "_connect_readonly", traced_connect)
    instrumented_runs = list_recent_executions(db, limit=1)

    workflow_queries = [
        statement
        for statement in statements
        if "event_type = 'workflow.progress.updated'" in statement
    ]
    direct_queries = [
        statement
        for statement in statements
        if f"INDEXED BY {DIRECT_EVENT_INDEX}" in statement and "aggregate_id =" in statement
    ]
    start_queries = [
        statement for statement in statements if f"INDEXED BY {START_EVENT_INDEX}" in statement
    ]
    conn = sqlite3.connect(db)
    try:
        plans = [
            conn.execute("EXPLAIN QUERY PLAN " + query).fetchall() for query in workflow_queries
        ]
        plans.extend(
            conn.execute("EXPLAIN QUERY PLAN " + query).fetchall() for query in direct_queries
        )
        start_plans = [
            conn.execute("EXPLAIN QUERY PLAN " + query).fetchall() for query in start_queries
        ]
    finally:
        conn.close()

    # Negative control: the removed direct-history shape really does perform
    # work proportional to all 100k selected rows and would violate the bounded
    # VM-step gate. Sample every 1,000 opcodes to avoid perturbing wall timing.
    unbounded_progress_callbacks = 0

    def count_unbounded_steps():
        nonlocal unbounded_progress_callbacks
        unbounded_progress_callbacks += 1
        return 0

    conn = sqlite3.connect(db)
    try:
        conn.set_progress_handler(count_unbounded_steps, 1_000)
        unbounded_rows = conn.execute(
            "SELECT rowid, aggregate_id, event_type, payload FROM events "
            "WHERE aggregate_id = ? AND event_type = ?",
            ("exec_target", "workflow.progress.updated"),
        ).fetchall()
    finally:
        conn.close()

    assert [(run["status"], run["completed_count"]) for run in runs] == [("completed", 1)]
    assert [(run["status"], run["completed_count"]) for run in instrumented_runs] == [
        ("completed", 1)
    ]
    assert runs[0]["last_row"] == 100_001
    assert median(cpu_samples) < 0.5
    assert max(wall_samples) < 5.0
    assert vm_steps < 5_000
    assert len(unbounded_rows) == 100_000
    assert unbounded_progress_callbacks >= 100
    assert max(decode_samples) <= 4
    assert len(workflow_queries) == 6
    assert len(direct_queries) == 18
    assert len(start_queries) == 1
    assert all("TEMP B-TREE" not in str(row[3]) for plan in (*plans, *start_plans) for row in plan)
    used_indexes = {str(row[3]) for plan in (*plans, *start_plans) for row in plan}
    assert any(AGGREGATE_EVENT_INDEX in value for value in used_indexes)
    assert any(DIRECT_EVENT_INDEX in value for value in used_indexes)
    assert any(START_EVENT_INDEX in value for value in used_indexes)
    assert any(RUNNING_PROGRESS_INDEX in value for value in used_indexes)
    assert any(WORKFLOW_SNAPSHOT_INDEX in value for value in used_indexes)


def test_picker_bounds_large_start_history(tmp_path, monkeypatch) -> None:
    db = tmp_path / "large-start-history.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
        conn.execute("CREATE INDEX ix_events_event_type ON events (event_type)")
        _install_picker_indexes(conn)
        conn.executemany(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            (
                (
                    f"orch_old_{index}",
                    "orchestrator.session.started",
                    json.dumps({"execution_id": f"exec_old_{index}"}),
                )
                for index in range(99_999)
            ),
        )
        conn.execute(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            (
                "orch_target",
                "orchestrator.session.started",
                json.dumps({"execution_id": "exec_target", "runtime_backend": "codex"}),
            ),
        )
        conn.commit()
        conn.execute("ANALYZE")
    finally:
        conn.close()

    original_connect = reader_module._connect_readonly
    statements: list[str] = []
    start_vm_steps = 0
    counting_start = False

    def traced_connect(db_path):
        traced = original_connect(db_path)

        def trace_statement(statement):
            nonlocal counting_start
            statements.append(statement)
            counting_start = f"INDEXED BY {START_EVENT_INDEX}" in statement

        def count_step():
            nonlocal start_vm_steps
            if counting_start:
                start_vm_steps += 1
            return 0

        traced.set_trace_callback(trace_statement)
        traced.set_progress_handler(count_step, 1)
        return traced

    monkeypatch.setattr(reader_module, "_connect_readonly", traced_connect)
    runs = list_recent_executions(db, limit=1)
    start_queries = [
        statement for statement in statements if f"INDEXED BY {START_EVENT_INDEX}" in statement
    ]
    conn = sqlite3.connect(db)
    try:
        start_plans = [
            conn.execute("EXPLAIN QUERY PLAN " + query).fetchall() for query in start_queries
        ]
    finally:
        conn.close()

    assert [(run["execution_id"], run["provider"]) for run in runs] == [("exec_target", "codex")]
    assert runs[0]["last_row"] == 100_000
    assert start_vm_steps < 500
    assert len(start_queries) == 1
    assert all("TEMP B-TREE" not in str(row[3]) for plan in start_plans for row in plan)
    assert any(START_EVENT_INDEX in str(row[3]) for plan in start_plans for row in plan)


@pytest.mark.parametrize(
    ("old_running_checkpoint", "expected_status"),
    [(False, "paused"), (True, "running")],
)
def test_picker_bounds_producer_split_progress_with_absent_family_lookup(
    tmp_path,
    monkeypatch,
    old_running_checkpoint: bool,
    expected_status: str,
) -> None:
    db = tmp_path / f"producer-split-{old_running_checkpoint}.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
        conn.execute("CREATE INDEX ix_events_aggregate_id ON events (aggregate_id)")
        conn.execute("CREATE INDEX ix_events_event_type ON events (event_type)")
        _install_picker_indexes(conn)
        conn.executemany(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            [
                (
                    "orch_target",
                    "orchestrator.session.started",
                    json.dumps({"execution_id": "exec_target"}),
                ),
                (
                    "orch_target",
                    "orchestrator.session.paused",
                    json.dumps({"execution_id": "exec_target"}),
                ),
                (
                    "exec_target",
                    "workflow.progress.updated",
                    json.dumps(
                        {
                            "execution_id": "exec_target",
                            "acceptance_criteria": [{"node_id": "target", "status": "completed"}],
                        }
                    ),
                ),
            ],
        )
        first_status = "running" if old_running_checkpoint else "completed"
        first = json.dumps(
            {"execution_id": "exec_target", "progress": {"runtime_status": first_status}}
        )
        completed = json.dumps(
            {"execution_id": "exec_target", "progress": {"runtime_status": "completed"}}
        )
        conn.execute(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            ("orch_target", "orchestrator.progress.updated", first),
        )
        conn.executemany(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            (("orch_target", "orchestrator.progress.updated", completed) for _ in range(99_999)),
        )
        conn.commit()
        conn.execute("ANALYZE")
    finally:
        conn.close()

    vm_steps = 0
    decode_calls = 0
    original_connect = reader_module._connect_readonly
    original_decode = reader_module._decode_payload

    def counted_connect(db_path):
        traced = original_connect(db_path)

        def count_step():
            nonlocal vm_steps
            vm_steps += 1
            return 0

        traced.set_progress_handler(count_step, 1)
        return traced

    def counted_decode(payload):
        nonlocal decode_calls
        decode_calls += 1
        return original_decode(payload)

    monkeypatch.setattr(reader_module, "_connect_readonly", counted_connect)
    monkeypatch.setattr(reader_module, "_decode_payload", counted_decode)
    started = time.perf_counter()
    runs = list_recent_executions(db, limit=1)
    elapsed = time.perf_counter() - started

    assert [(run["status"], run["completed_count"]) for run in runs] == [(expected_status, 1)]
    assert runs[0]["last_row"] == 100_003
    assert elapsed < 0.5
    assert vm_steps < 5_000
    assert decode_calls <= 7


def test_picker_keeps_latest_usable_workflow_snapshot_before_poison_rows(tmp_path) -> None:
    db = tmp_path / "workflow-poison.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
        conn.execute("CREATE INDEX ix_events_aggregate_id ON events (aggregate_id)")
        conn.execute("CREATE INDEX ix_events_event_type ON events (event_type)")
        _install_picker_indexes(conn)
        conn.executemany(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            [
                (
                    "orch_target",
                    "orchestrator.session.started",
                    json.dumps({"execution_id": "exec_target"}),
                ),
                (
                    "exec_target",
                    "workflow.progress.updated",
                    json.dumps(
                        {
                            "execution_id": "exec_target",
                            "acceptance_criteria": [{"node_id": "target", "status": "completed"}],
                        }
                    ),
                ),
                (
                    "exec_target",
                    "workflow.progress.updated",
                    json.dumps(
                        {
                            "execution_id": "exec_target",
                            "last_update": {"runtime_status": "completed"},
                        }
                    ),
                ),
                ("exec_target", "workflow.progress.updated", "{malformed"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    runs = list_recent_executions(db, limit=1)

    assert [(run["status"], run["completed_count"]) for run in runs] == [("completed", 1)]
    assert runs[0]["last_row"] == 3


def test_picker_status_only_turn_cannot_erase_running_after_settled_snapshot(tmp_path) -> None:
    db = tmp_path / "workflow-status-only-turn.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
        conn.execute("CREATE INDEX ix_events_aggregate_id ON events (aggregate_id)")
        conn.execute("CREATE INDEX ix_events_event_type ON events (event_type)")
        _install_picker_indexes(conn)
        conn.executemany(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            [
                (
                    "orch_target",
                    "orchestrator.session.started",
                    json.dumps({"execution_id": "exec_target"}),
                ),
                (
                    "exec_target",
                    "workflow.progress.updated",
                    json.dumps(
                        {
                            "execution_id": "exec_target",
                            "acceptance_criteria": [{"node_id": "target", "status": "completed"}],
                        }
                    ),
                ),
                (
                    "exec_target",
                    "workflow.progress.updated",
                    json.dumps(
                        {
                            "execution_id": "exec_target",
                            "last_update": {"runtime_status": "running"},
                        }
                    ),
                ),
                (
                    "exec_target",
                    "workflow.progress.updated",
                    json.dumps(
                        {
                            "execution_id": "exec_target",
                            "last_update": {"runtime_status": "completed"},
                        }
                    ),
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    runs = list_recent_executions(db, limit=1)

    assert [(run["status"], run["completed_count"]) for run in runs] == [("running", 1)]
    assert runs[0]["last_row"] == 4


@pytest.mark.parametrize("container_key", ["progress", "last_update", None])
@pytest.mark.parametrize("whitespace", [" ", "\t", "\n", "\r", "\v", "\f"])
def test_picker_running_whitespace_matches_python_reducer_and_partial_index(
    tmp_path,
    container_key,
    whitespace,
) -> None:
    db = tmp_path / f"running-whitespace-{container_key}-{ord(whitespace)}.db"
    running_payload = {"execution_id": "exec_target"}
    wrapped_status = f"{whitespace}running{whitespace}"
    if container_key is None:
        running_payload["runtime_status"] = wrapped_status
    else:
        running_payload[container_key] = {"runtime_status": wrapped_status}

    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
        conn.execute("CREATE INDEX ix_events_aggregate_id ON events (aggregate_id)")
        conn.execute("CREATE INDEX ix_events_event_type ON events (event_type)")
        _install_picker_indexes(conn)
        conn.executemany(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            [
                (
                    "orch_target",
                    "orchestrator.session.started",
                    json.dumps({"execution_id": "exec_target"}),
                ),
                ("orch_target", "orchestrator.session.paused", "{}"),
                (
                    "exec_target",
                    "workflow.progress.updated",
                    json.dumps(
                        {
                            "execution_id": "exec_target",
                            "acceptance_criteria": [{"node_id": "target", "status": "completed"}],
                        }
                    ),
                ),
                ("exec_target", "workflow.progress.updated", json.dumps(running_payload)),
                (
                    "exec_target",
                    "workflow.progress.updated",
                    json.dumps(
                        {
                            "execution_id": "exec_target",
                            "last_update": {"runtime_status": "completed"},
                        }
                    ),
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    runs = list_recent_executions(db, limit=1)

    assert [(run["status"], run["completed_count"]) for run in runs] == [("running", 1)]


def test_picker_unicode_whitespace_is_not_a_running_acknowledgement(tmp_path) -> None:
    db = tmp_path / "running-nbsp.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
        conn.execute("CREATE INDEX ix_events_aggregate_id ON events (aggregate_id)")
        conn.execute("CREATE INDEX ix_events_event_type ON events (event_type)")
        _install_picker_indexes(conn)
        conn.executemany(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            [
                (
                    "orch_target",
                    "orchestrator.session.started",
                    json.dumps({"execution_id": "exec_target"}),
                ),
                ("orch_target", "orchestrator.session.paused", "{}"),
                (
                    "exec_target",
                    "workflow.progress.updated",
                    json.dumps(
                        {
                            "execution_id": "exec_target",
                            "acceptance_criteria": [{"node_id": "target", "status": "completed"}],
                        }
                    ),
                ),
                (
                    "exec_target",
                    "workflow.progress.updated",
                    json.dumps(
                        {
                            "execution_id": "exec_target",
                            "progress": {"runtime_status": "\u00a0running\u00a0"},
                        }
                    ),
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    runs = list_recent_executions(db, limit=1)

    assert [(run["status"], run["completed_count"]) for run in runs] == [("paused", 1)]
