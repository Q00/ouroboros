"""``_connect_readonly`` must URI-encode the DB path so ``?``/``#`` in a path
can't be misparsed as the SQLite URI's query/fragment."""

from __future__ import annotations

import sqlite3

from ouroboros.dashboard_web.reader import _connect_readonly


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
