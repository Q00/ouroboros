"""``_connect_readonly`` must URI-encode the DB path so ``?``/``#`` in a path
can't be misparsed as the SQLite URI's query/fragment."""

from __future__ import annotations

import json
import sqlite3
from statistics import median
import time

import pytest
from sqlalchemy import create_engine

from ouroboros.dashboard_web import reader as reader_module
from ouroboros.dashboard_web.reader import (
    _RELEVANT_EVENT_TYPES,
    EventTail,
    PickerIndexContractError,
    _connect_readonly,
    list_recent_executions,
)
from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.events import create_workflow_progress_event
from ouroboros.persistence.event_store import EventStore
from ouroboros.persistence.picker_index_provisioning import provision_picker_indexes
from ouroboros.persistence.picker_indexes import (
    DIRECT_EVENT_INDEX,
    PICKER_CONTRACT_DDL_BY_NAME,
    PICKER_DIRECT_EVENT_TYPES,
    PICKER_GAP_INDEX,
    PICKER_META_TABLE,
    PICKER_PROGRESS_SCOPE_SQL,
    PICKER_PROGRESS_TABLE,
    PICKER_PROJECTION_SCOPE_SQL,
    PICKER_PROJECTION_VERSION,
    PICKER_RELEVANT_EVENT_TYPES,
    PICKER_START_EXECUTION_ID_SQL,
    PICKER_START_EXECUTION_INDEX,
    PICKER_START_SCOPE_SQL,
    PICKER_START_SESSION_ID_SQL,
    PICKER_START_SESSION_TABLE,
    PICKER_START_TABLE,
    RUNNING_PROGRESS_SQL,
    VALID_JSON_SQL,
    WORKFLOW_SNAPSHOT_SQL,
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

    # Production installs and backfills the projection only from a writable
    # connection.  These reader fixtures deliberately insert legacy history
    # first, then use that same provisioning path instead of relying on the
    # triggers that the application-owned projection replaced.
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            provision_picker_indexes(connection)
    finally:
        engine.dispose()


def _install_picker_contract(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute('PRAGMA table_info("events")')}
    if "picker_projection_version" not in columns:
        conn.execute("ALTER TABLE events ADD COLUMN picker_projection_version INTEGER")
    for statement in PICKER_CONTRACT_DDL_BY_NAME.values():
        conn.execute(statement)
    conn.execute(
        f"INSERT INTO {PICKER_META_TABLE} "
        "(contract_version, backfilled_through_rowid) VALUES (?, 0)",
        (PICKER_PROJECTION_VERSION,),
    )


def _refresh_picker_contract(conn: sqlite3.Connection) -> None:
    """Backfill fixtures using the production projection's stored-row rules."""
    conn.execute(f"DELETE FROM {PICKER_START_TABLE}")
    conn.execute(f"DELETE FROM {PICKER_START_SESSION_TABLE}")
    conn.execute(f"DELETE FROM {PICKER_PROGRESS_TABLE}")
    conn.execute(f"DELETE FROM {PICKER_META_TABLE}")
    conn.execute(
        f"INSERT INTO {PICKER_START_TABLE} "
        "(event_rowid, execution_id, session_id) "
        f"SELECT rowid, {PICKER_START_EXECUTION_ID_SQL}, "
        f"{PICKER_START_SESSION_ID_SQL} FROM events WHERE {PICKER_START_SCOPE_SQL}"
    )
    conn.execute(
        f"INSERT OR IGNORE INTO {PICKER_START_SESSION_TABLE} "
        "(session_id, execution_id, event_rowid) "
        "SELECT session_id, coalesce(execution_id, ''), MIN(event_rowid) "
        f"FROM {PICKER_START_TABLE} WHERE session_id IS NOT NULL "
        "GROUP BY session_id, execution_id"
    )
    conn.execute(
        f"INSERT INTO {PICKER_PROGRESS_TABLE} ("
        "aggregate_id, event_type, latest_valid_rowid, latest_running_rowid, "
        "latest_snapshot_rowid) SELECT aggregate_id, event_type, MAX(rowid), "
        f"MAX(CASE WHEN {RUNNING_PROGRESS_SQL} THEN rowid END), "
        "MAX(CASE WHEN event_type = 'workflow.progress.updated' "
        f"AND {WORKFLOW_SNAPSHOT_SQL} THEN rowid END) FROM events "
        f"WHERE {PICKER_PROGRESS_SCOPE_SQL} AND {VALID_JSON_SQL} "
        "GROUP BY aggregate_id, event_type"
    )
    backfilled_through = conn.execute(
        "SELECT CASE WHEN MAX(rowid) > 0 THEN MAX(rowid) ELSE 0 END FROM events"
    ).fetchone()[0]
    conn.execute(
        f"INSERT INTO {PICKER_META_TABLE} "
        "(contract_version, backfilled_through_rowid) VALUES (?, ?)",
        (PICKER_PROJECTION_VERSION, int(backfilled_through)),
    )
    conn.execute(
        f"UPDATE events SET picker_projection_version = ? WHERE {PICKER_PROJECTION_SCOPE_SQL}",
        (PICKER_PROJECTION_VERSION,),
    )


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
    assert _RELEVANT_EVENT_TYPES == PICKER_RELEVANT_EVENT_TYPES
    assert set(PICKER_DIRECT_EVENT_TYPES) == set(PICKER_RELEVANT_EVENT_TYPES) - {
        "orchestrator.session.started",
        "orchestrator.progress.updated",
        "workflow.progress.updated",
    }


@pytest.mark.parametrize("wrong_same_name", [False, True])
def test_picker_fails_before_history_reads_without_exact_index_contract(
    tmp_path, monkeypatch, wrong_same_name: bool
) -> None:
    db = tmp_path / f"legacy-{wrong_same_name}.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
        if wrong_same_name:
            conn.execute(f"CREATE TABLE {PICKER_START_TABLE} (aggregate_id TEXT)")
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
        conn.execute("ALTER TABLE events ADD COLUMN picker_projection_version INTEGER")
        for statement in PICKER_CONTRACT_DDL_BY_NAME.values():
            conn.execute(
                statement.replace("'workflow.progress.updated'", "'workflow.progress. updated'")
            )
        conn.execute(
            f"INSERT INTO {PICKER_META_TABLE} "
            "(contract_version, backfilled_through_rowid) VALUES (?, 0)",
            (PICKER_PROJECTION_VERSION,),
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


def test_picker_pins_snapshot_before_gap_probe_and_history_reads(tmp_path, monkeypatch) -> None:
    db = tmp_path / "gap-snapshot-race.db"
    _make_events_db(
        db,
        [
            (
                "orch-before",
                "orchestrator.session.started",
                {"execution_id": "exec-before"},
            )
        ],
    )
    writer = sqlite3.connect(db)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.commit()
    finally:
        writer.close()

    original_connect = reader_module._connect_readonly
    observed = {"in_transaction": False, "injected": False}

    class InjectingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def execute(self, statement, parameters=()):
            cursor = self._connection.execute(statement, parameters)
            if f"INDEXED BY {PICKER_GAP_INDEX}" in statement and not observed["injected"]:
                observed["in_transaction"] = self._connection.in_transaction
                concurrent_writer = sqlite3.connect(db)
                try:
                    concurrent_writer.execute(
                        "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
                        (
                            "orch-after-gap",
                            "orchestrator.session.started",
                            json.dumps({"execution_id": "exec-after-gap"}),
                        ),
                    )
                    concurrent_writer.commit()
                finally:
                    concurrent_writer.close()
                observed["injected"] = True
            return cursor

        def __getattr__(self, name):
            return getattr(self._connection, name)

    def injecting_connect(db_path):
        return InjectingConnection(original_connect(db_path))

    monkeypatch.setattr(reader_module, "_connect_readonly", injecting_connect)

    runs = list_recent_executions(db)

    assert observed == {"in_transaction": True, "injected": True}
    assert [run["execution_id"] for run in runs] == ["exec-before"]
    monkeypatch.setattr(reader_module, "_connect_readonly", original_connect)
    with pytest.raises(PickerIndexContractError, match="contains unprojected relevant events"):
        list_recent_executions(db)


def test_picker_fails_closed_for_dangling_start_projection(tmp_path) -> None:
    db = tmp_path / "dangling-start.db"
    _make_events_db(
        db,
        [("orch_target", "orchestrator.session.started", {"execution_id": "exec_target"})],
    )
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            f"INSERT INTO {PICKER_START_TABLE} (event_rowid) VALUES (?)",
            (999_999,),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(PickerIndexContractError, match="start pointer mismatch"):
        list_recent_executions(db)


def test_picker_fails_closed_for_wrong_type_start_projection(tmp_path) -> None:
    db = tmp_path / "wrong-type-start.db"
    _make_events_db(
        db,
        [("orch_target", "orchestrator.session.started", {"execution_id": "exec_target"})],
    )
    conn = sqlite3.connect(db)
    try:
        cursor = conn.execute(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            ("unrelated", "telemetry.unrelated", "{}"),
        )
        conn.execute(
            f"INSERT INTO {PICKER_START_TABLE} (event_rowid) VALUES (?)",
            (int(cursor.lastrowid),),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(PickerIndexContractError, match="start pointer mismatch"):
        list_recent_executions(db)


def test_picker_fails_closed_for_mismatched_progress_projection(tmp_path) -> None:
    db = tmp_path / "mismatched-progress.db"
    _make_events_db(
        db,
        [
            ("orch_target", "orchestrator.session.started", {"execution_id": "exec_target"}),
            (
                "exec_target",
                "workflow.progress.updated",
                {"execution_id": "exec_target", "acceptance_criteria": []},
            ),
        ],
    )
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            f"UPDATE {PICKER_PROGRESS_TABLE} SET latest_valid_rowid = 1, "
            "latest_running_rowid = NULL, latest_snapshot_rowid = NULL "
            "WHERE aggregate_id = ? AND event_type = ?",
            ("exec_target", "workflow.progress.updated"),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(PickerIndexContractError, match="pointer mismatch"):
        list_recent_executions(db)


@pytest.mark.parametrize(
    ("column", "missing_rowid", "kind"),
    [
        ("latest_valid_rowid", 999_997, "valid"),
        ("latest_running_rowid", -1, "running"),
        ("latest_snapshot_rowid", -2, "snapshot"),
    ],
)
def test_picker_fails_closed_for_dangling_progress_projection(
    tmp_path,
    column: str,
    missing_rowid: int,
    kind: str,
) -> None:
    db = tmp_path / f"dangling-progress-{kind}.db"
    _make_events_db(
        db,
        [
            ("orch_target", "orchestrator.session.started", {"execution_id": "exec_target"}),
            (
                "exec_target",
                "workflow.progress.updated",
                {"execution_id": "exec_target", "acceptance_criteria": []},
            ),
        ],
    )
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            f"UPDATE {PICKER_PROGRESS_TABLE} SET {column} = ? "
            "WHERE aggregate_id = ? AND event_type = ?",
            (missing_rowid, "exec_target", "workflow.progress.updated"),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(PickerIndexContractError, match=rf"/{kind}/{missing_rowid}$"):
        list_recent_executions(db)


@pytest.mark.parametrize(
    "column,kind", [("latest_running_rowid", "running"), ("latest_snapshot_rowid", "snapshot")]
)
def test_picker_fails_closed_for_wrong_predicate_progress_projection(
    tmp_path,
    column: str,
    kind: str,
) -> None:
    db = tmp_path / f"wrong-predicate-progress-{kind}.db"
    _make_events_db(
        db,
        [
            ("orch_target", "orchestrator.session.started", {"execution_id": "exec_target"}),
            (
                "exec_target",
                "workflow.progress.updated",
                {"execution_id": "exec_target", "acceptance_criteria": []},
            ),
        ],
    )
    conn = sqlite3.connect(db)
    try:
        cursor = conn.execute(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            (
                "exec_target",
                "workflow.progress.updated",
                json.dumps({"execution_id": "exec_target", "runtime_status": "completed"}),
            ),
        )
        corrupt_rowid = int(cursor.lastrowid)
        conn.execute(
            "UPDATE events SET picker_projection_version = ? WHERE rowid = ?",
            (PICKER_PROJECTION_VERSION, corrupt_rowid),
        )
        conn.execute(
            f"UPDATE {PICKER_PROGRESS_TABLE} SET latest_valid_rowid = ?, {column} = ? "
            "WHERE aggregate_id = ? AND event_type = ?",
            (corrupt_rowid, corrupt_rowid, "exec_target", "workflow.progress.updated"),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(PickerIndexContractError, match=rf"/{kind}/{corrupt_rowid}$"):
        list_recent_executions(db)


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


@pytest.mark.parametrize(
    ("event_type", "aggregate_id", "payload"),
    [
        (
            "execution.tool.started",
            "child-session",
            {"execution_id": "exec-low", "session_id": "native-child", "node_id": "n"},
        ),
        (
            "execution.coordinator.tool.started",
            "child-session",
            {"execution_id": "exec-low", "session_id": "native-child", "node_id": "n"},
        ),
        ("orchestrator.tool.called", "orch-low", {"tool_name": "Read"}),
        (
            "execution.session.completed",
            "child-session",
            {
                "execution_id": "exec-low",
                "session_id": "native-child",
                "goal": "Recovered goal",
            },
        ),
        (
            "execution.ac.model_routed",
            "child-session",
            {
                "execution_id": "exec-low",
                "session_id": "native-child",
                "node_id": "n",
                "model": "gpt-test",
            },
        ),
        (
            "execution.ac.token_attribution.reported",
            "child-session",
            {
                "execution_id": "exec-low",
                "session_id": "native-child",
                "node_id": "n",
                "token_spend": 1234,
            },
        ),
        (
            "execution.frugality_proof.evaluated",
            "child-session",
            {
                "execution_id": "exec-low",
                "session_id": "native-child",
                "status": "proven",
            },
        ),
        (
            "execution.frugality_retrospective.reported",
            "child-session",
            {
                "execution_id": "exec-low",
                "session_id": "native-child",
                "baseline_tokens": 2000,
                "actual_tokens": 1234,
            },
        ),
    ],
)
def test_picker_preserves_every_low_volume_summary_family(
    tmp_path, event_type: str, aggregate_id: str, payload: dict
) -> None:
    db = tmp_path / f"summary-family-{event_type}.db"
    _make_events_db(
        db,
        [
            ("orch-low", "orchestrator.session.started", {"execution_id": "exec-low"}),
            (aggregate_id, event_type, payload),
        ],
    )

    runs = list_recent_executions(db)

    assert len(runs) == 1
    assert runs[0]["last_row"] == 2
    if event_type == "execution.ac.token_attribution.reported":
        assert runs[0]["total_tokens"] == 1234
    if event_type == "execution.session.completed":
        assert runs[0]["goal"] == "Recovered goal"


@pytest.mark.parametrize(
    ("execution_id", "session_id"),
    [
        ("", "exec-fallback"),
        (" \t\n", "exec-fallback"),
        (0, "exec-fallback"),
        (None, "exec-fallback"),
        (0, None),
    ],
)
def test_canonical_link_falls_back_to_session_then_aggregate(
    tmp_path, execution_id, session_id
) -> None:
    db = tmp_path / "canonical-fallback.db"
    aggregate_id = "exec-fallback" if session_id is None else "child-session"
    _make_events_db(
        db,
        [
            (
                "orch-fallback",
                "orchestrator.session.started",
                {"execution_id": "exec-fallback"},
            ),
            (
                aggregate_id,
                "execution.ac.token_attribution.reported",
                {
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "node_id": "n",
                    "token_spend": 1234,
                },
            ),
        ],
    )

    runs = list_recent_executions(db)

    assert runs[0]["total_tokens"] == 1234
    assert runs[0]["last_row"] == 2


def test_canonical_link_rejects_conflicting_selected_run_ids(tmp_path) -> None:
    db = tmp_path / "canonical-conflict.db"
    _make_events_db(
        db,
        [
            ("orch-a", "orchestrator.session.started", {"execution_id": "exec-a"}),
            ("orch-b", "orchestrator.session.started", {"execution_id": "exec-b"}),
            (
                "native-child",
                "execution.ac.token_attribution.reported",
                {
                    "execution_id": "exec-a",
                    "session_id": "orch-b",
                    "node_id": "n",
                    "token_spend": 1234,
                },
            ),
        ],
    )

    with pytest.raises(PickerIndexContractError, match="conflicting selected payload ids"):
        list_recent_executions(db, limit=2)


def test_run_identity_fails_closed_for_cross_namespace_collision(tmp_path) -> None:
    db = tmp_path / "cross-namespace-collision.db"
    _make_events_db(
        db,
        [
            ("orch-a", "orchestrator.session.started", {"execution_id": "shared"}),
            ("shared", "orchestrator.session.started", {"execution_id": "exec-b"}),
        ],
    )

    for selected_id in ("shared", "exec-b"):
        with pytest.raises(PickerIndexContractError, match="ambiguous selected run ids"):
            EventTail(db, selected_id).fetch_new()
    with pytest.raises(PickerIndexContractError, match="ambiguous selected run ids"):
        list_recent_executions(db, limit=1)


def test_run_identity_allows_one_token_in_both_namespaces_for_same_cluster(tmp_path) -> None:
    db = tmp_path / "same-cluster-identity.db"
    _make_events_db(
        db,
        [("shared", "orchestrator.session.started", {"execution_id": "shared"})],
    )

    assert [event["event_type"] for event in EventTail(db, "shared").fetch_new()] == [
        "orchestrator.session.started"
    ]
    assert [
        (run["execution_id"], run["session_id"]) for run in list_recent_executions(db, limit=1)
    ] == [("shared", "shared")]


def test_canonical_link_does_not_cross_link_foreign_execution_to_selected_session(
    tmp_path,
) -> None:
    db = tmp_path / "canonical-foreign-execution.db"
    _make_events_db(
        db,
        [
            ("orch-local", "orchestrator.session.started", {"execution_id": "exec-local"}),
            (
                "native-child",
                "execution.ac.token_attribution.reported",
                {
                    "execution_id": "exec-foreign",
                    "session_id": "orch-local",
                    "node_id": "n",
                    "token_spend": 1234,
                },
            ),
        ],
    )

    runs = list_recent_executions(db)

    assert runs[0]["total_tokens"] == 0.0
    assert runs[0]["last_row"] == 1


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
        _install_picker_contract(conn)
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
                (
                    "exec_good",
                    "execution.ac.token_attribution.reported",
                    "{selected-malformed",
                ),
                ("orch_good", "orchestrator.progress.updated", "{local-not-json"),
            ],
        )
        _refresh_picker_contract(conn)
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
        _install_picker_contract(conn)
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
                for index in range(20)
            ],
        )
        _refresh_picker_contract(conn)
        conn.commit()
    finally:
        conn.close()

    runs = list_recent_executions(db, limit=1)

    assert [(run["execution_id"], run["goal"]) for run in runs] == [
        ("exec_visible", "Still listed")
    ]


async def test_recent_runs_skip_blank_identity_and_preserve_resolvable_padded_identity(
    tmp_path,
) -> None:
    db = tmp_path / "start-identity-whitespace.db"
    store = EventStore(f"sqlite+aiosqlite:///{db}")
    await store.initialize()
    blank_execution_id = " \t\n"
    padded_execution_id = "  exec-padded \t"
    blank = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id="orch-blank",
        data={"execution_id": blank_execution_id, "seed_goal": "Never advertised"},
    )
    padded = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id="orch-padded",
        data={"execution_id": padded_execution_id, "seed_goal": "Still resolvable"},
    )
    await store.append(blank)
    await store.append(padded)
    await store.close()

    runs = list_recent_executions(db, limit=10)

    assert [(run["execution_id"], run["goal"]) for run in runs] == [
        (padded_execution_id, "Still resolvable")
    ]
    events = EventTail(db, runs[0]["execution_id"]).fetch_new()
    assert [(event["event_type"], event["payload"]["execution_id"]) for event in events] == [
        ("orchestrator.session.started", padded_execution_id)
    ]
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            f"SELECT execution_id, session_id FROM {PICKER_START_TABLE} ORDER BY event_rowid"
        ).fetchall() == [(None, "orch-blank"), (padded_execution_id, "orch-padded")]
    finally:
        conn.close()


async def test_recent_runs_skip_blank_session_and_preserve_resolvable_padded_session(
    tmp_path,
) -> None:
    db = tmp_path / "start-session-whitespace.db"
    store = EventStore(f"sqlite+aiosqlite:///{db}")
    await store.initialize()
    blank_session_id = " \t\n"
    padded_session_id = "  orch-padded \t"
    blank = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id=blank_session_id,
        data={"execution_id": "exec-blank-session", "seed_goal": "Never advertised"},
    )
    padded = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id=padded_session_id,
        data={"execution_id": "exec-padded-session", "seed_goal": "Still resolvable"},
    )
    await store.append(blank)
    await store.append(padded)
    await store.close()

    runs = list_recent_executions(db, limit=10)

    assert [(run["execution_id"], run["session_id"], run["goal"]) for run in runs] == [
        ("exec-padded-session", padded_session_id, "Still resolvable")
    ]
    for selected_id in ("exec-padded-session", padded_session_id):
        events = EventTail(db, selected_id).fetch_new()
        assert [event["event_type"] for event in events] == ["orchestrator.session.started"]
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            f"SELECT execution_id, session_id FROM {PICKER_START_TABLE} ORDER BY event_rowid"
        ).fetchall() == [
            ("exec-blank-session", None),
            ("exec-padded-session", padded_session_id),
        ]
    finally:
        conn.close()


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


def test_recent_summary_reduces_complete_duplicate_execution_session_cluster(tmp_path) -> None:
    db = tmp_path / "duplicate-start-lifecycle.db"
    _make_events_db(
        db,
        [
            ("orch-old", "orchestrator.session.started", {"execution_id": "exec-shared"}),
            ("orch-old", "orchestrator.session.failed", {"reason": "durable failure"}),
            ("orch-new", "orchestrator.session.started", {"execution_id": "exec-shared"}),
        ],
    )

    runs = list_recent_executions(db, limit=1)
    detail_events = EventTail(db, "exec-shared").fetch_new()

    assert [(run["execution_id"], run["session_id"], run["status"]) for run in runs] == [
        ("exec-shared", "orch-new", "failed")
    ]
    assert [event["event_type"] for event in detail_events] == [
        "orchestrator.session.started",
        "orchestrator.session.failed",
        "orchestrator.session.started",
    ]


def test_picker_paginates_zero_and_minimum_int64_start_rowids(tmp_path) -> None:
    db = tmp_path / "nonpositive-start-rowids.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
        conn.executemany(
            "INSERT INTO events (rowid, aggregate_id, event_type, payload) VALUES (?, ?, ?, ?)",
            [
                (
                    -(2**63),
                    "orch-min",
                    "orchestrator.session.started",
                    json.dumps({"execution_id": "exec-min"}),
                ),
                (
                    0,
                    "orch-zero",
                    "orchestrator.session.started",
                    json.dumps({"execution_id": "exec-zero"}),
                ),
            ],
        )
        _install_picker_contract(conn)
        _refresh_picker_contract(conn)
        conn.commit()
    finally:
        conn.close()

    runs = list_recent_executions(db, limit=10)

    assert [run["execution_id"] for run in runs] == ["exec-zero", "exec-min"]


def test_canonical_picker_is_independent_of_100k_foreign_token_history(
    tmp_path, monkeypatch
) -> None:
    db = tmp_path / "foreign-token-scale.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
        conn.execute("CREATE INDEX ix_events_aggregate_id ON events (aggregate_id)")
        conn.execute("CREATE INDEX ix_events_event_type ON events (event_type)")
        _install_picker_contract(conn)
        conn.execute(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            (
                "orch-target",
                "orchestrator.session.started",
                json.dumps({"execution_id": "exec-target"}),
            ),
        )
        foreign_payload = json.dumps(
            {
                "execution_id": "exec-foreign",
                "session_id": "native-foreign",
                "node_id": "n",
                "token_spend": 1,
            }
        )
        conn.executemany(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            (
                (
                    f"child-foreign-{index % 100}",
                    "execution.ac.token_attribution.reported",
                    foreign_payload,
                )
                for index in range(100_000)
            ),
        )
        conn.execute(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            (
                "child-target",
                "execution.ac.token_attribution.reported",
                json.dumps(
                    {
                        "execution_id": "exec-target",
                        "session_id": "native-target",
                        "node_id": "n",
                        "token_spend": 1234,
                    }
                ),
            ),
        )
        _refresh_picker_contract(conn)
        conn.commit()
        conn.execute("ANALYZE")
    finally:
        conn.close()

    statements: list[str] = []
    vm_steps = 0
    original_connect = reader_module._connect_readonly

    def counted_connect(db_path):
        nonlocal vm_steps
        counted = original_connect(db_path)

        def count_step():
            nonlocal vm_steps
            vm_steps += 1
            return 0

        counted.set_trace_callback(statements.append)
        counted.set_progress_handler(count_step, 1)
        return counted

    monkeypatch.setattr(reader_module, "_connect_readonly", counted_connect)
    runs = list_recent_executions(db)
    picker_vm_steps = vm_steps
    token_queries = [
        statement
        for statement in statements
        if "event_type = 'execution.ac.token_attribution.reported'" in statement
        and f"INDEXED BY {DIRECT_EVENT_INDEX}" in statement
    ]
    assert len(token_queries) == 1

    statements.clear()
    vm_steps = 0
    tail_events = EventTail(db, "exec-target").fetch_new()
    tail_vm_steps = vm_steps

    conn = sqlite3.connect(db)
    try:
        plan = conn.execute("EXPLAIN QUERY PLAN " + token_queries[0]).fetchall()
    finally:
        conn.close()

    assert runs[0]["total_tokens"] == 1234
    assert runs[0]["last_row"] == 100_002
    assert [event["event_type"] for event in tail_events] == [
        "orchestrator.session.started",
        "execution.ac.token_attribution.reported",
    ]
    assert picker_vm_steps < 5_000
    # EventTail performs one equality seek per selected id/family; this fixed
    # contract-validation/query fanout is independent of the 100k foreign rows.
    assert tail_vm_steps < 10_000
    assert any(
        f"SEARCH events USING INDEX {DIRECT_EVENT_INDEX} (<expr>=? AND event_type=?)" in str(row[3])
        for row in plan
    )
    assert all("SCAN events" not in str(row[3]) for row in plan)
    assert all("TEMP B-TREE" not in str(row[3]) for row in plan)


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
        _install_picker_contract(conn)
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
        _refresh_picker_contract(conn)
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
    assert len(progress_queries) == 10
    assert all("aggregate_id =" in statement for statement in progress_queries)
    assert any("ix_events_aggregate_id" in str(row[3]) for row in plan)
    assert all("TEMP B-TREE" not in str(row[3]) for row in plan)
    assert any("ix_events_aggregate_id" in str(row[3]) for row in direct_tail_plan)
    assert any(DIRECT_EVENT_INDEX in str(row[3]) for row in linked_tail_plan)
    assert all("SCAN events" not in str(row[3]) for row in direct_tail_plan + linked_tail_plan)
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
        _install_picker_contract(conn)
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
        _refresh_picker_contract(conn)
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
        statement
        for statement in tail_statements
        if "'workflow.progress.updated'" in statement
        and f"INDEXED BY {PICKER_GAP_INDEX}" not in statement
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
    assert len(picker_workflow_queries) == 4
    assert len(tail_workflow_queries) == 2
    assert all(
        "aggregate_id =" in statement or "FROM events WHERE rowid =" in statement
        for statement in workflow_queries
    )
    assert all("json_extract" not in statement for statement in tail_workflow_queries)
    assert all(
        any(
            "ix_events_aggregate_id" in str(row[3])
            or "ix_events_picker_" in str(row[3])
            or "PRIMARY KEY" in str(row[3])
            for row in plan
        )
        for plan in workflow_plans
    )
    assert all("SCAN events" not in str(row[3]) for plan in workflow_plans for row in plan)
    assert all("TEMP B-TREE" not in str(row[3]) for plan in workflow_plans for row in plan)


def test_picker_bounds_selected_workflow_progress_history(tmp_path, monkeypatch) -> None:
    db = tmp_path / "selected-workflow-progress-scale.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
        conn.execute("CREATE INDEX ix_events_aggregate_id ON events (aggregate_id)")
        conn.execute("CREATE INDEX ix_events_event_type ON events (event_type)")
        _install_picker_contract(conn)
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
        _refresh_picker_contract(conn)
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
    current_statement = "<connection setup>"
    statement_vm_steps: dict[str, int] = {}

    def traced_connect(db_path):
        traced = original_connect(db_path)

        def trace_statement(statement):
            nonlocal current_statement
            statements.append(statement)
            current_statement = statement
            statement_vm_steps.setdefault(statement, 0)

        def count_step():
            nonlocal vm_steps
            vm_steps += 1
            statement_vm_steps[current_statement] = statement_vm_steps.get(current_statement, 0) + 1
            return 0

        traced.set_trace_callback(trace_statement)
        traced.set_progress_handler(count_step, 1)
        return traced

    monkeypatch.setattr(reader_module, "_connect_readonly", traced_connect)
    instrumented_runs = list_recent_executions(db, limit=1)

    head_queries = [
        statement for statement in statements if f"FROM {PICKER_PROGRESS_TABLE}" in statement
    ]
    pointer_queries = [
        statement for statement in statements if "FROM events WHERE rowid =" in statement
    ]
    direct_queries = [
        statement for statement in statements if f"INDEXED BY {DIRECT_EVENT_INDEX}" in statement
    ]
    linked_queries = [
        statement
        for statement in statements
        if f"INDEXED BY {DIRECT_EVENT_INDEX}" in statement and "aggregate_id NOT IN" in statement
    ]
    start_queries = [
        statement
        for statement in statements
        if f"FROM {PICKER_START_TABLE} AS projected" in statement
    ]
    conn = sqlite3.connect(db)
    try:
        plans = [
            conn.execute("EXPLAIN QUERY PLAN " + query).fetchall()
            for query in (*head_queries, *pointer_queries)
        ]
        plans.extend(
            conn.execute("EXPLAIN QUERY PLAN " + query).fetchall() for query in direct_queries
        )
        plans.extend(
            conn.execute("EXPLAIN QUERY PLAN " + query).fetchall() for query in linked_queries
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
    top_vm_statements = sorted(statement_vm_steps.items(), key=lambda item: item[1], reverse=True)[
        :10
    ]
    assert vm_steps < 5_000, top_vm_statements
    assert len(unbounded_rows) == 100_000
    assert unbounded_progress_callbacks >= 100
    assert max(decode_samples) <= 4
    assert len(head_queries) == 3
    assert len(pointer_queries) == 2
    assert len(direct_queries) == len(PICKER_DIRECT_EVENT_TYPES)
    assert len(linked_queries) == 0
    assert len(start_queries) == 3
    assert all("SCAN events" not in str(row[3]) for plan in (*plans, *start_plans) for row in plan)
    # This fixture has one projected start. SQLite 3.45 may sort that single row,
    # while the dedicated 100k-start regression below enforces the real no-sort
    # and VM-step contract for start pagination. Keep this test focused on the
    # selected 100k progress history that it constructs.
    assert all("TEMP B-TREE" not in str(row[3]) for plan in plans for row in plan)
    used_access_paths = {str(row[3]) for plan in (*plans, *start_plans) for row in plan}
    assert any(DIRECT_EVENT_INDEX in value for value in used_access_paths)
    assert any("USING PRIMARY KEY" in value for value in used_access_paths)
    assert any("USING INTEGER PRIMARY KEY" in value for value in used_access_paths)


def test_picker_bounds_large_start_history(tmp_path, monkeypatch) -> None:
    db = tmp_path / "large-start-history.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
        conn.execute("CREATE INDEX ix_events_event_type ON events (event_type)")
        _install_picker_contract(conn)
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
        _refresh_picker_contract(conn)
        conn.commit()
        conn.execute("ANALYZE")
    finally:
        conn.close()

    original_connect = reader_module._connect_readonly
    statements: list[str] = []
    start_vm_steps = 0
    current_statement = "<connection setup>"
    statement_vm_steps: dict[str, int] = {}

    def traced_connect(db_path):
        traced = original_connect(db_path)

        def trace_statement(statement):
            nonlocal current_statement
            statements.append(statement)
            current_statement = statement
            statement_vm_steps.setdefault(statement, 0)

        def count_step():
            nonlocal start_vm_steps
            statement_vm_steps[current_statement] = statement_vm_steps.get(current_statement, 0) + 1
            if f"FROM {PICKER_START_TABLE} AS projected" in current_statement:
                start_vm_steps += 1
            return 0

        traced.set_trace_callback(trace_statement)
        traced.set_progress_handler(count_step, 1)
        return traced

    monkeypatch.setattr(reader_module, "_connect_readonly", traced_connect)
    runs = list_recent_executions(db, limit=1)
    start_queries = [
        statement
        for statement in statements
        if f"FROM {PICKER_START_TABLE} AS projected" in statement
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
    top_vm_statements = sorted(statement_vm_steps.items(), key=lambda item: item[1], reverse=True)[
        :10
    ]
    assert start_vm_steps < 500, (top_vm_statements, start_plans)
    assert len(start_queries) == 3
    assert all("SCAN events" not in str(row[3]) for plan in start_plans for row in plan)
    assert all("TEMP B-TREE" not in str(row[3]) for plan in start_plans for row in plan)
    assert any("USING INTEGER PRIMARY KEY" in str(row[3]) for plan in start_plans for row in plan)


@pytest.mark.parametrize(
    ("run_id", "expected_index", "expected_queries"),
    [
        ("exec_target", PICKER_START_EXECUTION_INDEX, 4),
        ("orch_target", "PRIMARY KEY", 4),
    ],
)
def test_event_tail_bounds_identity_resolution_across_100k_starts(
    tmp_path,
    run_id: str,
    expected_index: str,
    expected_queries: int,
) -> None:
    db = tmp_path / f"tail-start-identity-{run_id}.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
        conn.execute("CREATE INDEX ix_events_event_type ON events (event_type)")
        _install_picker_contract(conn)
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
                json.dumps({"execution_id": "exec_target"}),
            ),
        )
        _refresh_picker_contract(conn)
        conn.commit()
        conn.execute("ANALYZE")
    finally:
        conn.close()

    traced = _connect_readonly(db)
    statements: list[str] = []
    vm_steps = 0

    def count_step() -> int:
        nonlocal vm_steps
        vm_steps += 1
        return 0

    traced.set_trace_callback(statements.append)
    traced.set_progress_handler(count_step, 1)
    try:
        resolved = EventTail(db, run_id)._resolve_ids(traced)
    finally:
        traced.close()

    identity_queries = [
        statement
        for statement in statements
        if f"FROM {PICKER_START_TABLE} AS projected" in statement
        or f"FROM {PICKER_START_SESSION_TABLE} AS lookup" in statement
    ]
    conn = sqlite3.connect(db)
    try:
        plans = [
            conn.execute("EXPLAIN QUERY PLAN " + statement).fetchall()
            for statement in identity_queries
        ]
    finally:
        conn.close()

    assert resolved == ["exec_target", "orch_target"]
    assert vm_steps < 500, (vm_steps, plans)
    assert len(identity_queries) == expected_queries
    access_paths = {str(row[3]) for plan in plans for row in plan}
    assert any(expected_index in path for path in access_paths)
    assert any("USING INTEGER PRIMARY KEY" in path for path in access_paths)
    assert all("SCAN projected" not in path for path in access_paths)
    assert all("SCAN events" not in path for path in access_paths)
    assert all("TEMP B-TREE" not in path for path in access_paths)


def test_event_tail_identity_projection_resolves_duplicate_execution_starts_symmetrically(
    tmp_path,
) -> None:
    db = tmp_path / "tail-duplicate-starts.db"
    _make_events_db(
        db,
        [
            ("orch-one", "orchestrator.session.started", {"execution_id": "exec-shared"}),
            ("orch-two", "orchestrator.session.started", {"execution_id": "exec-shared"}),
            (
                "orch-two",
                "orchestrator.progress.updated",
                {"execution_id": "exec-shared", "progress": {"runtime_status": "running"}},
            ),
        ],
    )

    expected_ids = ["exec-shared", "orch-one", "orch-two"]
    expected_events = [
        "orchestrator.session.started",
        "orchestrator.session.started",
        "orchestrator.progress.updated",
    ]
    for selected_id in expected_ids:
        conn = _connect_readonly(db)
        try:
            assert EventTail(db, selected_id)._resolve_ids(conn) == expected_ids
        finally:
            conn.close()
        assert [event["event_type"] for event in EventTail(db, selected_id).fetch_new()] == (
            expected_events
        )


async def test_event_tail_refreshes_identity_cluster_after_first_fetch(tmp_path) -> None:
    db = tmp_path / "tail-live-duplicate-start.db"
    store = EventStore(f"sqlite+aiosqlite:///{db}")
    await store.initialize()
    try:
        await store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="orch-one",
                data={"execution_id": "exec-shared"},
            )
        )
        tail = EventTail(db, "orch-one")
        assert [event["event_type"] for event in tail.fetch_new()] == [
            "orchestrator.session.started"
        ]

        await store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="orch-two",
                data={"execution_id": "exec-shared"},
            )
        )
        await store.append(
            BaseEvent(
                type="orchestrator.progress.updated",
                aggregate_type="session",
                aggregate_id="orch-two",
                data={
                    "execution_id": "exec-shared",
                    "progress": {"runtime_status": "running"},
                },
            )
        )

        assert [event["event_type"] for event in tail.fetch_new()] == [
            "orchestrator.session.started",
            "orchestrator.progress.updated",
        ]
        assert [event["event_type"] for event in EventTail(db, "exec-shared").fetch_new()] == [
            "orchestrator.session.started",
            "orchestrator.session.started",
            "orchestrator.progress.updated",
        ]
    finally:
        await store.close()


def test_event_tail_fails_closed_for_mismatched_start_identity(tmp_path) -> None:
    db = tmp_path / "tail-mismatched-start-identity.db"
    _make_events_db(
        db,
        [("orch-target", "orchestrator.session.started", {"execution_id": "exec-target"})],
    )
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            f"UPDATE {PICKER_START_TABLE} SET session_id = ? WHERE event_rowid = 1",
            ("orch-corrupt",),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(PickerIndexContractError, match="start identity pointer mismatch"):
        EventTail(db, "orch-corrupt").fetch_new()


@pytest.mark.parametrize(
    ("corruption_sql", "run_id", "expected_detail"),
    [
        (
            f"DELETE FROM {PICKER_START_SESSION_TABLE}",
            "orch-target",
            "start identity pointer mismatch",
        ),
        (
            f"DELETE FROM {PICKER_START_SESSION_TABLE}",
            "exec-target",
            "start identity pointer mismatch",
        ),
        (
            f"DELETE FROM {PICKER_START_TABLE}",
            "orch-target",
            "start identity pointer mismatch",
        ),
        (
            f"UPDATE {PICKER_START_SESSION_TABLE} SET execution_id = 'exec-corrupt'",
            "orch-target",
            "start identity pointer mismatch",
        ),
    ],
)
def test_event_tail_fails_closed_for_missing_dangling_or_mismatched_session_map(
    tmp_path, corruption_sql: str, run_id: str, expected_detail: str
) -> None:
    db = tmp_path / "tail-corrupt-session-map.db"
    _make_events_db(
        db,
        [("orch-target", "orchestrator.session.started", {"execution_id": "exec-target"})],
    )
    conn = sqlite3.connect(db)
    try:
        conn.execute(corruption_sql)
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(PickerIndexContractError, match=expected_detail):
        EventTail(db, run_id).fetch_new()


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
        _install_picker_contract(conn)
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
        _refresh_picker_contract(conn)
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
        _install_picker_contract(conn)
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
        _refresh_picker_contract(conn)
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
        _install_picker_contract(conn)
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
        _refresh_picker_contract(conn)
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
        _install_picker_contract(conn)
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
        _refresh_picker_contract(conn)
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
        _install_picker_contract(conn)
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
        _refresh_picker_contract(conn)
        conn.commit()
    finally:
        conn.close()

    runs = list_recent_executions(db, limit=1)

    assert [(run["status"], run["completed_count"]) for run in runs] == [("paused", 1)]
