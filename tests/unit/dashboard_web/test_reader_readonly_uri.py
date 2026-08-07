"""``_connect_readonly`` must URI-encode the DB path so ``?``/``#`` in a path
can't be misparsed as the SQLite URI's query/fragment."""

from __future__ import annotations

import json
import sqlite3

from ouroboros.dashboard_web.reader import (
    _RELEVANT_EVENT_TYPES,
    _connect_readonly,
    list_recent_executions,
)
from ouroboros.orchestrator.events import create_workflow_progress_event


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
