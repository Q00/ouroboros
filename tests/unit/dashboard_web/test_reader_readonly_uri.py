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


def _make_db(path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.execute("INSERT INTO t (v) VALUES ('ok')")
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
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
        rows = [
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
        ]
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

    runs = list_recent_executions(db, limit=10)

    assert [run["execution_id"] for run in runs] == ["exec_one", "exec_two"]
    two = runs[1]
    assert two["goal"] == "  Keep  leading\nline  "
    assert two["status"] == "running"
    assert two["completed_count"] == 1
    assert two["total_count"] == 2
    assert two["executing_count"] == 1
