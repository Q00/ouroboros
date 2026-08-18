"""Disposable Memory contracts over the SQLite artifact store.

These tests pin behaviour, not storage: identical republication collapses
into the stored row, a conflicting republication is refused, a miss creates
no state, and pruning is an explicit terminal tombstone rather than a
deletion a caller could mistake for absence.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from ouroboros.core.disposable_memory import MAX_DISPOSABLE_ARTIFACT_BYTES
from ouroboros.persistence.artifact_store import (
    ArtifactContractConflictError,
    ArtifactNotFoundError,
    ArtifactStore,
    ArtifactStoreError,
    ArtifactTombstonedError,
    ArtifactTooLargeError,
    canonical_artifact_bytes,
)

NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)


def _store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts")


def _put(
    store: ArtifactStore,
    contract_id: str,
    body: object,
    *,
    duration_ms: int = 12,
    runtime_id: str = "test-runtime",
    events_emitted_count: int = 3,
    precommit_check: Any = None,
    commit_check: Any = None,
):
    return store.put_for_contract(
        contract_id=contract_id,
        body=body,
        runtime_id=runtime_id,
        duration_ms=duration_ms,
        events_emitted_count=events_emitted_count,
        precommit_check=precommit_check,
        commit_check=commit_check,
    )


def _database_path(store: ArtifactStore) -> Path:
    return store.root / "artifacts.db"


def _row(store: ArtifactStore, contract_id: str) -> tuple[Any, ...] | None:
    """Read one raw row so a test can assert what a call did or did not write."""
    with closing(sqlite3.connect(_database_path(store))) as connection:
        return connection.execute(
            "SELECT body, created_at, updated_at, duration_ms, pruned_reason"
            " FROM artifacts WHERE contract_id = ?",
            (contract_id,),
        ).fetchone()


def _age(store: ArtifactStore, contract_id: str, *, days: int) -> None:
    """Backdate one publication relative to ``NOW``.

    The store trusts its own ``created_at`` column, so aging a contract for
    prune tests means rewriting that stored clock rather than any file time.
    """
    with closing(sqlite3.connect(_database_path(store))) as connection, connection:
        connection.execute(
            "UPDATE artifacts SET created_at = ? WHERE contract_id = ?",
            ((NOW - timedelta(days=days)).isoformat(), contract_id),
        )


def test_identical_republication_returns_stored_envelope_and_writes_nothing(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first = _put(store, "CONTRACT1", {"version": 1}, duration_ms=12)
    stored_row = _row(store, "CONTRACT1")

    # The arriving call measured a different duration; the stored publication
    # won, so its duration is the one on record and the row stays untouched.
    second = _put(store, "CONTRACT1", {"version": 1}, duration_ms=99)

    assert second == first
    assert second.duration_ms == 12
    assert _row(store, "CONTRACT1") == stored_row


def test_publisher_provenance_is_returned_rather_than_replaced(tmp_path: Path) -> None:
    """What the publisher handed over comes back, on the write and on every read.

    ``runtime_id`` and ``events_emitted_count`` arrive on the same call as the
    body, and ``docs/events.md`` defines the first as the runtime that produced
    the artifact.  A store that answers with a name of its own is answering a
    question it was not asked.
    """
    store = _store(tmp_path)
    written = _put(
        store,
        "CONTRACT1",
        {"version": 1},
        runtime_id="mcp:fanout-synthesis:v2",
        events_emitted_count=9,
    )

    assert written.runtime_id == "mcp:fanout-synthesis:v2"
    assert written.events_emitted_count == 9
    for read in (
        store.fetch("CONTRACT1").envelope,
        store.envelope_if_exists("CONTRACT1"),
        store.replay("CONTRACT1").envelope,
    ):
        assert read == written

    # A second identical publication collapses into the stored one, so the
    # provenance on record wins over whatever this arrival claims to be.
    collapsed = _put(
        store,
        "CONTRACT1",
        {"version": 1},
        runtime_id="some-other-runtime",
        events_emitted_count=0,
    )
    assert collapsed == written


def test_applied_prune_returns_the_space_rather_than_only_the_row(tmp_path: Path) -> None:
    """A report that says bytes were removed has to mean the disk got them back.

    Clearing a column frees pages inside the database and leaves the file the
    size it was, which would make the CLI's byte count a claim about space no
    one can spend.
    """
    store = _store(tmp_path)
    _put(store, "CONTRACT1", {"payload": "x" * 400_000})
    _age(store, "CONTRACT1", days=200)
    size_before = _database_path(store).stat().st_size

    report = store.prune(apply=True, now=NOW)

    assert report.removed_bytes > 300_000
    assert _database_path(store).stat().st_size < size_before - report.removed_bytes // 2


def test_a_database_whose_table_was_never_committed_reads_as_absence(tmp_path: Path) -> None:
    """A file holding nothing is a miss, and a miss has to stay recoverable.

    SQLite creates the file on connect and the table is committed after, so a
    full disk on the very first publication leaves the file behind empty. Read
    as an error this is unrecoverable in a way no other miss is: the write that
    would create the table is reached through a read, so nothing could ever
    initialize the store again.
    """
    store = _store(tmp_path)
    store.root.mkdir(parents=True, exist_ok=True)
    _database_path(store).write_bytes(b"")

    assert store.envelope_if_exists("CONTRACT1") is None
    assert store.fetch_if_exists("CONTRACT1") is None
    assert store.published_contracts(since=NOW - timedelta(days=1), until=NOW) == []

    envelope = _put(store, "CONTRACT1", {"published": True})

    assert store.fetch("CONTRACT1").envelope == envelope


def test_a_failed_compaction_does_not_report_the_prune_as_failed(tmp_path: Path) -> None:
    """The bodies are gone by then, and a tombstone is terminal.

    Reporting failure would tell a caller nothing happened when the one
    irreversible part already did — and the likeliest way to fail is a full
    disk, which is the state a caller prunes to escape.
    """

    class NoVacuum(sqlite3.Connection):
        def execute(self, sql: str, *args: Any, **kwargs: Any):  # noqa: ANN202
            if sql.strip().upper().startswith("VACUUM"):
                raise sqlite3.OperationalError("unable to open database file")
            return super().execute(sql, *args, **kwargs)

    store = _store(tmp_path)
    _put(store, "CONTRACT1", {"payload": "x" * 1000})
    _age(store, "CONTRACT1", days=200)
    store._connect_for_write = lambda: sqlite3.connect(  # type: ignore[method-assign]
        _database_path(store), factory=NoVacuum
    )

    report = store.prune(apply=True, now=NOW)

    assert report.removed_contract_ids == ("CONTRACT1",)
    with pytest.raises(ArtifactTombstonedError):
        store.fetch("CONTRACT1")


def test_prune_reports_encoded_bytes_not_characters(tmp_path: Path) -> None:
    """Every name this number travels under says bytes, so it has to be bytes.

    ``length()`` on a TEXT column counts characters.  A body of accented text
    is two bytes per character and was reported as one, so the CLI's ``B`` was
    an undercount for any body that was not ASCII.
    """
    store = _store(tmp_path)
    body = {"note": "é" * 1_000}
    encoded = len(canonical_artifact_bytes(body))
    _put(store, "CONTRACT1", body)
    _age(store, "CONTRACT1", days=200)

    report = store.prune(apply=True, now=NOW)

    assert report.removed_bytes == encoded
    assert report.candidates[0].body_bytes == encoded


def test_contract_id_cannot_be_reused_for_different_content(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _put(store, "CONTRACT1", {"version": 1})
    assert _put(store, "CONTRACT1", {"version": 1}) == first

    with pytest.raises(ArtifactContractConflictError, match="different artifact"):
        _put(store, "CONTRACT1", {"version": 2})
    assert store.fetch("CONTRACT1").body == {"version": 1}


def test_json_native_values_round_trip_without_normalization(tmp_path: Path) -> None:
    store = _store(tmp_path)
    body = {
        "null": None,
        "boolean": True,
        "integer": 42,
        "number": 1.25,
        "string": "value",
        "nested": [False, {"items": [1, 2, 3]}],
    }

    _put(store, "CONTRACT1", body)

    assert store.fetch("CONTRACT1").body == body
    assert store.replay("CONTRACT1").body == body


def test_stored_json_null_body_is_a_publication_not_a_tombstone(tmp_path: Path) -> None:
    # JSON ``null`` is stored as the four-byte text ``null``, so it must never
    # collide with the SQL NULL that pruning writes to remove a body.
    store = _store(tmp_path)
    envelope = _put(store, "CONTRACT1", None)

    fetched = store.fetch("CONTRACT1")
    assert fetched.body is None
    assert fetched.envelope == envelope
    assert store.envelope_if_exists("CONTRACT1") == envelope
    assert _put(store, "CONTRACT1", None) == envelope


@pytest.mark.parametrize(
    "body",
    [
        {"tuple": (1, 2)},
        {1: "integer-key"},
        {True: "boolean-key"},
    ],
    ids=["tuple", "integer-key", "boolean-key"],
)
def test_lossy_non_json_native_values_are_rejected(tmp_path: Path, body: object) -> None:
    store = _store(tmp_path)

    with pytest.raises(ArtifactStoreError, match="JSON-native|JSON strings"):
        _put(store, "CONTRACT1", body)
    # Rejection precedes every write, so a fresh store gains no state at all.
    assert not store.root.exists()


def test_exact_one_mib_encoded_body_is_allowed_and_one_byte_more_is_rejected(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    empty_size = len(canonical_artifact_bytes({"output": ""}))
    exact = {"output": "x" * (MAX_DISPOSABLE_ARTIFACT_BYTES - empty_size)}
    oversized = {"output": "x" * (MAX_DISPOSABLE_ARTIFACT_BYTES - empty_size + 1)}
    assert len(canonical_artifact_bytes(exact)) == MAX_DISPOSABLE_ARTIFACT_BYTES

    _put(store, "CONTRACT1", exact)
    assert store.fetch("CONTRACT1").body == exact

    untouched = _store(tmp_path / "fresh")
    with pytest.raises(ArtifactTooLargeError, match="exceeds"):
        _put(untouched, "CONTRACT2", oversized)
    # The size check fires before anything is written, so the refused store
    # never even creates its database.
    assert not untouched.root.exists()

    with pytest.raises(ValueError, match="1 MiB hard cap"):
        ArtifactStore(
            tmp_path / "too-large-cap",
            max_artifact_bytes=MAX_DISPOSABLE_ARTIFACT_BYTES + 1,
        )


def test_missing_contract_reads_do_not_create_store_state(tmp_path: Path) -> None:
    store = ArtifactStore.for_project(tmp_path)

    assert store.envelope_if_exists("MISSING1") is None
    assert store.fetch_if_exists("MISSING1") is None
    with pytest.raises(ArtifactNotFoundError):
        store.fetch("MISSING1")

    assert not (tmp_path / ".ouroboros").exists()


@pytest.mark.parametrize(
    "contract_id",
    [f"fanout:{'a' * 64}", "with:colons:inside", "trailing.", "../not-a-path"],
)
def test_caller_shaped_contract_ids_round_trip(tmp_path: Path, contract_id: str) -> None:
    # Contract ids stopped being filenames with the directory store, so the
    # shapes callers actually mint -- fanout digests, colons, trailing dots,
    # even path-looking text -- are plain keys that must round-trip verbatim.
    store = _store(tmp_path)
    envelope = _put(store, contract_id, {"portable": contract_id})

    assert envelope.contract_id == contract_id
    assert store.fetch(contract_id).body == {"portable": contract_id}
    assert store.replay(contract_id).body == {"portable": contract_id}


def test_contract_id_length_bounds_are_the_whole_identity_rule(tmp_path: Path) -> None:
    store = _store(tmp_path)

    for invalid in ("", "x" * 129):
        with pytest.raises(ValueError, match="1-128"):
            _put(store, invalid, {"unbounded": True})
    assert not store.root.exists()


def test_case_distinct_contract_ids_never_alias(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _put(store, "CaseSensitive", {"owner": "upper"})
    _put(store, "casesensitive", {"owner": "lower"})

    assert store.fetch("CaseSensitive").body == {"owner": "upper"}
    assert store.fetch("casesensitive").body == {"owner": "lower"}
    assert store.replay("CaseSensitive").body == {"owner": "upper"}
    assert store.replay("casesensitive").body == {"owner": "lower"}


def test_prune_is_dry_run_by_default_and_applied_prune_is_terminal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _put(store, "CONTRACT1", {"expired": 1})
    _put(store, "CONTRACT2", {"expired": 2})
    _age(store, "CONTRACT1", days=100)
    _age(store, "CONTRACT2", days=100)

    plan = store.prune(ttl=timedelta(days=90), now=NOW)
    assert plan.applied is False
    assert [candidate.contract_id for candidate in plan.candidates] == ["CONTRACT1", "CONTRACT2"]
    assert store.fetch("CONTRACT1").body == {"expired": 1}

    applied = store.prune(ttl=timedelta(days=90), apply=True, now=NOW)
    assert applied.applied is True
    assert applied.removed_contract_ids == ("CONTRACT1", "CONTRACT2")
    assert applied.removed_bytes > 0
    for contract_id in ("CONTRACT1", "CONTRACT2"):
        with pytest.raises(ArtifactTombstonedError, match="force-rerun"):
            store.replay(contract_id)
        with pytest.raises(ArtifactTombstonedError, match="force-rerun"):
            store.envelope_if_exists(contract_id)
        # Terminal means terminal: replaying the same contract id can never
        # resurrect the body, only a fresh identity can rerun the work.
        with pytest.raises(ArtifactTombstonedError, match="new contract id"):
            _put(store, contract_id, {"expired": "rerun attempt"})

    # A second applied prune finds only tombstones and removes nothing again.
    again = store.prune(ttl=timedelta(days=90), apply=True, now=NOW)
    assert again.removed_contract_ids == ()
    assert again.candidates == ()


def test_replay_retention_protects_contracts_unless_operator_opts_in(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _put(store, "RETAINED1", {"kind": "retained"})
    _age(store, "RETAINED1", days=10)

    # Past the TTL but inside the replay-retention window: default pruning
    # must keep the body so replay stays answerable.
    default_plan = store.prune(ttl=timedelta(days=1), now=NOW)
    assert default_plan.candidates == ()

    opted_in = store.prune(ttl=timedelta(days=1), allow_replay_tombstone=True, now=NOW)
    assert [candidate.contract_id for candidate in opted_in.candidates] == ["RETAINED1"]
    assert "operator allowed" in opted_in.candidates[0].reason


def test_commit_gate_immediately_precedes_first_publication_mutation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    observed: list[object] = []

    def commit_gate() -> None:
        # At gate time the existing-contract lookup has already run and found
        # nothing, and the row insert has not happened yet; recording the
        # store's visible state proves both without touching any internals.
        observed.append(store.fetch_if_exists("CONTRACT-GATE"))

    _put(
        store,
        "CONTRACT-GATE",
        {"durable": True},
        precommit_check=lambda: observed.append("precommit"),
        commit_check=commit_gate,
    )

    assert observed == ["precommit", None]
    assert store.fetch("CONTRACT-GATE").body == {"durable": True}


def test_commit_gate_does_not_fire_when_the_lookup_already_decides(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _put(store, "CONTRACT1", {"version": 1})
    fired: list[str] = []

    # Both a collapsing republication and a refused conflict are decided by
    # the existing row before any mutation, so the gate must never open.
    assert (
        _put(store, "CONTRACT1", {"version": 1}, commit_check=lambda: fired.append("commit"))
        == first
    )
    with pytest.raises(ArtifactContractConflictError):
        _put(store, "CONTRACT1", {"version": 2}, commit_check=lambda: fired.append("commit"))

    assert fired == []


def test_commit_gate_refusal_leaves_the_contract_unpublished(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def refuse() -> None:
        raise RuntimeError("caller cancelled at the gate")

    with pytest.raises(RuntimeError, match="cancelled at the gate"):
        _put(store, "CONTRACT1", {"never": "stored"}, commit_check=refuse)

    assert store.fetch_if_exists("CONTRACT1") is None
    with pytest.raises(ArtifactNotFoundError):
        store.fetch("CONTRACT1")


def test_sqlite_failure_surfaces_as_store_error_not_sqlite_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _put(store, "CONTRACT1", {"stored": True})
    # Overwriting the database with garbage makes every later SQLite call
    # fail, which must reach callers as the store's own error type.
    _database_path(store).write_bytes(b"this is not a sqlite database")

    with pytest.raises(ArtifactStoreError) as read_error:
        store.fetch("CONTRACT1")
    assert isinstance(read_error.value.__cause__, sqlite3.Error)

    with pytest.raises(ArtifactStoreError) as write_error:
        _put(store, "CONTRACT2", {"stored": True})
    assert isinstance(write_error.value.__cause__, sqlite3.Error)

    with pytest.raises(ArtifactStoreError):
        store.prune(now=NOW)


def test_envelope_read_never_materializes_the_body_column(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    envelope = _put(store, "CONTRACT1", {"large": "body"})
    real_connect = sqlite3.connect

    def deny_body_reads(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)

        def authorize(action: int, arg1: Any, arg2: Any, _dbname: Any, _source: Any) -> int:
            if action == sqlite3.SQLITE_READ and arg1 == "artifacts" and arg2 == "body":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr(sqlite3, "connect", deny_body_reads)

    # With body reads denied at the SQLite authorizer, a metadata read can
    # only succeed if its SELECT never names the column.
    assert store.envelope_if_exists("CONTRACT1") == envelope
    # The probe must be able to catch a body read, or the success above
    # proves nothing: an explicit fetch under the same denial has to fail.
    with pytest.raises(ArtifactStoreError):
        store.fetch_if_exists("CONTRACT1")
