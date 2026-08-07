"""``_connect_readonly`` must URI-encode the DB path so ``?``/``#`` in a path
can't be misparsed as the SQLite URI's query/fragment."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from ouroboros.dashboard_web import reader as reader_module
from ouroboros.dashboard_web.reader import (
    _RELEVANT_EVENT_TYPES,
    EventTail,
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
