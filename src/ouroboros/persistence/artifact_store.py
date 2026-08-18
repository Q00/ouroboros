"""SQLite-backed storage and conservative GC for Disposable Memory.

Artifact bodies live outside the EventStore in one project-local database at
``.ouroboros/artifacts/artifacts.db``, keyed by contract id.  A publication is
one row insert, so an interrupted writer leaves nothing behind and readers have
nothing to repair; the contract-key constraint is what refuses a conflicting
republication, and a tombstone is the row with its body removed rather than a
separate record.

Every operation opens a fresh connection: the store is called from
``asyncio.to_thread`` workers, and a connection cached on the instance would
cross threads.  Reads open the database read-only and treat a missing file as
absence, so a miss creates no state on disk.  Reads also take no lock beyond
SQLite's own -- every read here tolerates a stale one, and the worst outcome
is work repeated.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Final

from ouroboros.core.disposable_memory import (
    MAX_DISPOSABLE_ARTIFACT_BYTES,
    DisposableResultEnvelope,
    DisposableResultStatus,
    DisposableResultSummary,
)
from ouroboros.persistence.artifact_errors import (
    ArtifactContractConflictError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStoreError,
    ArtifactTombstonedError,
    ArtifactTooLargeError,
)
from ouroboros.persistence.artifact_schema import as_utc as _as_utc
from ouroboros.persistence.artifact_schema import canonical_artifact_bytes
from ouroboros.persistence.artifact_schema import validate_contract_id as _validate_contract_id
from ouroboros.persistence.sqlite_connection import configure_writable_sqlite_connection

DEFAULT_ARTIFACT_TTL = timedelta(days=90)
DEFAULT_REPLAY_RETENTION = timedelta(days=90)

_DATABASE_FILENAME: Final[str] = "artifacts.db"

# ``COLLATE BINARY`` is load-bearing: contract ids differing only in case are
# two distinct contracts and must never alias to one row.  ``kind`` is derived
# from the body rather than written beside it so the two can never disagree,
# and a pruned row loses its kind with its body, which keeps a tombstone out
# of every kind-filtered answer.
_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS artifacts (
  contract_id          TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
  body                 TEXT,
  kind                 TEXT GENERATED ALWAYS AS (json_extract(body,'$.kind')) VIRTUAL,
  runtime_id           TEXT NOT NULL,
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL,
  duration_ms          INTEGER NOT NULL,
  events_emitted_count INTEGER NOT NULL,
  pruned_reason        TEXT
)
"""

# ``status`` is the one envelope field the store still assembles rather than
# stores: a failed publication is never written, so the column would hold one
# value forever.  ``runtime_id`` and ``events_emitted_count`` are not that
# case and are stored -- they are the caller's, handed over exactly as ``body``
# is, and returning what was handed over is the contract.  A store that
# substitutes its own name for the producing runtime's is answering a question
# it was not asked.
_ENVELOPE_STATUS: Final[DisposableResultStatus] = DisposableResultStatus.COMPLETED


@dataclass(frozen=True, slots=True)
class FetchedArtifact:
    """Explicit-fetch result.  This body never appears on the normal envelope."""

    envelope: DisposableResultEnvelope
    body: Any


@dataclass(frozen=True, slots=True)
class PublishedContract:
    """One contract and when this store recorded publishing it."""

    contract_id: str
    published_at: datetime


@dataclass(frozen=True, slots=True)
class ArtifactPruneCandidate:
    """One immutable prune decision, planned before any row is touched."""

    contract_id: str
    age_seconds: float
    body_bytes: int
    reason: str


@dataclass(frozen=True, slots=True)
class ArtifactPruneReport:
    """Dry-run or applied GC result."""

    applied: bool
    candidates: tuple[ArtifactPruneCandidate, ...]
    removed_contract_ids: tuple[str, ...] = ()
    removed_bytes: int = 0


class ArtifactStore:
    """Project-local SQLite store with explicit replay and tombstoned GC."""

    def __init__(
        self,
        artifact_root: Path,
        *,
        max_artifact_bytes: int = MAX_DISPOSABLE_ARTIFACT_BYTES,
    ) -> None:
        if not 0 < max_artifact_bytes <= MAX_DISPOSABLE_ARTIFACT_BYTES:
            raise ValueError(
                "max_artifact_bytes must be positive and cannot exceed the 1 MiB hard cap"
            )
        self.root = Path(os.path.abspath(artifact_root.expanduser()))
        self.max_artifact_bytes = max_artifact_bytes
        self._database_path = self.root / _DATABASE_FILENAME

    @classmethod
    def for_project(
        cls,
        project_dir: Path,
        *,
        max_artifact_bytes: int = MAX_DISPOSABLE_ARTIFACT_BYTES,
    ) -> ArtifactStore:
        """Build the RFC-standard store below one project root."""
        return cls(
            project_dir.expanduser().resolve() / ".ouroboros" / "artifacts",
            max_artifact_bytes=max_artifact_bytes,
        )

    def initialize(self) -> None:
        """Create the database and its one table idempotently."""
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            with closing(self._connect_for_write()) as connection, connection:
                connection.execute(_SCHEMA)
        except sqlite3.Error as exc:
            raise ArtifactStoreError(
                "Artifact database could not be initialized",
                operation="write",
                details={"path": str(self._database_path)},
            ) from exc

    def put_for_contract(
        self,
        *,
        contract_id: str,
        body: Any,
        runtime_id: str,
        duration_ms: int,
        events_emitted_count: int,
        precommit_check: Callable[[], None] | None = None,
        commit_check: Callable[[], None] | None = None,
    ) -> DisposableResultEnvelope:
        """Publish a body and durably bind its bounded envelope to one contract.

        ``runtime_id`` and ``events_emitted_count`` are recorded beside the
        body, so the write return and every later read of the same contract
        answer with what the publisher supplied rather than with a value the
        store made up.
        """
        contract_id = _validate_contract_id(contract_id)
        payload = canonical_artifact_bytes(body)
        if len(payload) > self.max_artifact_bytes:
            raise ArtifactTooLargeError(
                "Disposable artifact exceeds the encoded output limit",
                operation="write",
                details={
                    "size_bytes": len(payload),
                    "max_artifact_bytes": self.max_artifact_bytes,
                },
            )
        envelope = _envelope(
            contract_id,
            runtime_id=runtime_id,
            duration_ms=duration_ms,
            events_emitted_count=events_emitted_count,
        )
        body_text = payload.decode("utf-8")
        self.initialize()
        if precommit_check is not None:
            precommit_check()
        timestamp = _as_utc(datetime.now(UTC)).isoformat()
        try:
            with closing(self._connect_for_write()) as connection:
                existing = _select_publication(connection, contract_id)
                if existing is not None:
                    return _resolve_existing_row(contract_id, existing, body_text)
                if commit_check is not None:
                    commit_check()
                try:
                    with connection:
                        connection.execute(
                            "INSERT INTO artifacts"
                            " (contract_id, body, runtime_id, created_at, updated_at,"
                            "  duration_ms, events_emitted_count)"
                            " VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                contract_id,
                                body_text,
                                runtime_id,
                                timestamp,
                                timestamp,
                                duration_ms,
                                events_emitted_count,
                            ),
                        )
                except sqlite3.IntegrityError:
                    # A racing publisher won the contract key between the
                    # SELECT and the INSERT.  The stored row decides, exactly
                    # as it would have had this call arrived after the race
                    # was already over.
                    raced = _select_publication(connection, contract_id)
                    if raced is None:
                        raise
                    return _resolve_existing_row(contract_id, raced, body_text)
        except sqlite3.Error as exc:
            raise ArtifactStoreError(
                "Artifact publication failed",
                operation="write",
                details={"contract_id": contract_id},
            ) from exc
        return envelope

    def fetch(self, contract_id: str) -> FetchedArtifact:
        """Explicitly fetch the body referenced by one contract."""
        fetched = self.fetch_if_exists(contract_id)
        if fetched is None:
            raise ArtifactNotFoundError(
                "Artifact contract does not exist",
                operation="read",
                details={"contract_id": contract_id},
            )
        return fetched

    def fetch_lane(self, contract_id: str, lane_id: str) -> FetchedArtifact:
        """Fetch one lane's output from a fan-out body, raising when absent."""
        fetched = self.fetch_lane_if_exists(contract_id, lane_id)
        if fetched is None:
            raise ArtifactNotFoundError(
                "Artifact contract does not exist",
                operation="read",
                details={"contract_id": contract_id, "lane_id": lane_id},
            )
        return fetched

    def fetch_lane_if_exists(self, contract_id: str, lane_id: str) -> FetchedArtifact | None:
        """Fetch one lane's output from a fan-out body, or ``None`` if absent.

        A lane is named by its own argument rather than packed into the contract
        id, so an ordinary id is never read as an address with a lane in it and
        this method is unreachable except by a caller that meant it.

        The whole filter is the ``WHERE`` below.  ``$.lane_id`` is the key the
        fan-out itself assigned from its dispatch roster, so it names the lane
        the server sent the work to; the ``lane_id`` a child happens to write
        inside its own output is not read here and is absent from some bodies.

        Selected with ``->`` rather than ``json_extract``, which returns a
        decoded SQL value: a lane whose whole output is prose would come back
        with its JSON quotes gone, ``true`` as ``1``, and ``null``
        indistinguishable from a missing row.  Fan-out submission accepts any
        JSON-native content, so an extraction that only survives objects and
        arrays loses the rest silently.  ``->`` returns JSON for every type.

        The lane match is an outer join, deliberately: an inner ``json_each``
        source removes the artifact's row itself whenever no lane matches --
        which is also what a pruned body produces -- so a tombstone became
        indistinguishable from a contract that never existed.  With the row
        kept, the three absences stay three: no row is no contract, a ``NULL``
        body is the tombstone ``fetch`` reports for the same id, and a ``NULL``
        lane match on a live body is a lane this fan-out never carried.

        Returns the lane's output as the body, so a caller that asked for one
        lane is handed one lane -- there is nothing left in it to select.
        """
        contract_id = _validate_contract_id(contract_id)
        row = self._read_one(
            "SELECT lane.value -> '$.output', runtime_id, duration_ms,"
            " events_emitted_count, updated_at, body"
            " FROM artifacts LEFT JOIN"
            " json_each(artifacts.body,'$.result.aggregated_outputs') AS lane"
            " ON json_extract(lane.value,'$.lane_id') = ?2"
            " WHERE contract_id = ?1",
            contract_id,
            lane_id,
        )
        if row is None:
            return None
        output_json, runtime_id, duration_ms, events_emitted_count, updated_at, body_text = row
        if body_text is None:
            raise _tombstoned_read_error(contract_id, tombstoned_at=updated_at)
        # ``->`` yields SQL NULL for a lane this body never carried and for a
        # lane entry with no ``output`` key; a JSON ``null`` output arrives as
        # the four-byte ``null``, exactly as a published body does in
        # ``fetch_if_exists``.
        if output_json is None:
            return None
        try:
            output = json.loads(output_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ArtifactIntegrityError(
                "Artifact body is not valid JSON",
                operation="read",
                details={"contract_id": contract_id, "lane_id": lane_id},
            ) from exc
        return FetchedArtifact(
            envelope=_envelope(
                contract_id,
                runtime_id=runtime_id,
                duration_ms=duration_ms,
                events_emitted_count=events_emitted_count,
            ),
            body=output,
        )

    def envelope_if_exists(self, contract_id: str) -> DisposableResultEnvelope | None:
        """Read only a contract's bounded envelope, never its artifact body.

        The SELECT never names ``body``; that it does not materialize the body
        is this method's contract.  ``pruned_reason`` stands in for the body's
        absence because pruning sets both in one statement.
        """
        contract_id = _validate_contract_id(contract_id)
        row = self._read_one(
            "SELECT pruned_reason, runtime_id, duration_ms, events_emitted_count, updated_at"
            " FROM artifacts WHERE contract_id = ?",
            contract_id,
        )
        if row is None:
            return None
        pruned_reason, runtime_id, duration_ms, events_emitted_count, updated_at = row
        if pruned_reason is not None:
            raise _tombstoned_read_error(contract_id, tombstoned_at=updated_at)
        return _envelope(
            contract_id,
            runtime_id=runtime_id,
            duration_ms=duration_ms,
            events_emitted_count=events_emitted_count,
        )

    def fetch_if_exists(self, contract_id: str) -> FetchedArtifact | None:
        """Fetch a durable contract, returning ``None`` only when none exists."""
        contract_id = _validate_contract_id(contract_id)
        row = self._read_one(
            "SELECT body, runtime_id, duration_ms, events_emitted_count, updated_at"
            " FROM artifacts WHERE contract_id = ?",
            contract_id,
        )
        if row is None:
            return None
        body_text, runtime_id, duration_ms, events_emitted_count, updated_at = row
        # An absent body means pruned, and nothing else can mean it: a
        # published body that is JSON ``null`` was stored as the four-byte
        # text ``null``, so SQL NULL here is only ever what pruning wrote.
        if body_text is None:
            raise _tombstoned_read_error(contract_id, tombstoned_at=updated_at)
        try:
            body = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise ArtifactIntegrityError(
                "Artifact body is not valid JSON",
                operation="read",
                details={"contract_id": contract_id},
            ) from exc
        return FetchedArtifact(
            envelope=_envelope(
                contract_id,
                runtime_id=runtime_id,
                duration_ms=duration_ms,
                events_emitted_count=events_emitted_count,
            ),
            body=body,
        )

    def replay(self, contract_id: str) -> FetchedArtifact:
        """Deterministically replay from storage without executing any work."""
        return self.fetch(contract_id)

    def published_contracts(
        self,
        *,
        since: datetime,
        until: datetime,
        kind: str | None = None,
        limit: int | None = None,
    ) -> list[PublishedContract]:
        """Return the contracts published inside ``[since, until]``, newest first.

        The window has two ends because a record carries whatever the clock read
        when it was written: a machine that ran ahead and was later corrected
        leaves records stamped in the future, and one of those is skipped rather
        than counted as published for as long as its lead lasts.

        Ties are broken by contract id descending, so two publications written
        in one clock tick order the same way on every call.

        ``kind`` filters on the generated column, which reads the body's own
        ``$.kind``; a pruned row has no body and therefore no kind, so it never
        answers a kind-filtered question.
        """
        query = (
            "SELECT contract_id, created_at FROM artifacts"
            " WHERE body IS NOT NULL AND created_at >= ? AND created_at <= ?"
        )
        parameters: list[Any] = [_as_utc(since).isoformat(), _as_utc(until).isoformat()]
        if kind is not None:
            query += " AND kind = ?"
            parameters.append(kind)
        query += " ORDER BY created_at DESC, contract_id DESC"
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        try:
            connection = self._connect_for_read()
            if connection is None:
                return []
            with closing(connection):
                rows = connection.execute(query, parameters).fetchall()
        except sqlite3.Error as exc:
            raise ArtifactStoreError(
                "Published-contract query failed",
                operation="read",
                details={"path": str(self._database_path)},
            ) from exc
        return [
            PublishedContract(
                contract_id=contract_id,
                published_at=datetime.fromisoformat(created_at),
            )
            for contract_id, created_at in rows
        ]

    def prune(
        self,
        *,
        ttl: timedelta = DEFAULT_ARTIFACT_TTL,
        apply: bool = False,
        allow_replay_tombstone: bool = False,
        now: datetime | None = None,
    ) -> ArtifactPruneReport:
        """Plan or apply TTL-bounded artifact pruning.

        A tombstone is the row with its body removed, so the ``body IS NOT
        NULL`` guard on the UPDATE makes applying idempotent under concurrent
        pruners: whichever writes first removes the bytes, and the other
        counts nothing.

        Clearing a column only frees its pages back to the database, so an
        applied prune that removed anything is followed by ``VACUUM`` -- best
        effort, since by then the bodies are gone and a failure would report
        as failed something that already happened.
        """
        if ttl.total_seconds() < 0:
            raise ValueError("ttl must not be negative")
        self.initialize()
        timestamp = _as_utc(now or datetime.now(UTC))
        try:
            with closing(self._connect_for_write()) as connection:
                rows = connection.execute(
                    # ``CAST(body AS BLOB)`` because ``length()`` on TEXT counts
                    # characters, and every name this number travels under --
                    # ``body_bytes``, ``removed_bytes``, the CLI's ``B`` -- says
                    # bytes.  A body of one accented character is two bytes and
                    # was being reported as one.
                    "SELECT contract_id, length(CAST(body AS BLOB)), created_at"
                    " FROM artifacts WHERE body IS NOT NULL"
                ).fetchall()
                candidates = _plan_prune(
                    rows,
                    ttl=ttl,
                    allow_replay_tombstone=allow_replay_tombstone,
                    now=timestamp,
                )
                if not apply:
                    return ArtifactPruneReport(applied=False, candidates=tuple(candidates))
                removed_contract_ids: list[str] = []
                removed_bytes = 0
                with connection:
                    for candidate in candidates:
                        cursor = connection.execute(
                            "UPDATE artifacts"
                            " SET body = NULL, pruned_reason = ?, updated_at = ?"
                            " WHERE contract_id = ? AND body IS NOT NULL",
                            (candidate.reason, timestamp.isoformat(), candidate.contract_id),
                        )
                        if cursor.rowcount == 1:
                            removed_contract_ids.append(candidate.contract_id)
                            removed_bytes += candidate.body_bytes
                if removed_contract_ids:
                    try:
                        # Outside the transaction: VACUUM cannot run in one.
                        # Outside the result too -- the bodies are already gone.
                        connection.execute("VACUUM")
                    except sqlite3.Error:
                        pass
        except sqlite3.Error as exc:
            raise ArtifactStoreError(
                "Artifact pruning failed",
                operation="write",
                details={"path": str(self._database_path)},
            ) from exc
        return ArtifactPruneReport(
            applied=True,
            candidates=tuple(candidates),
            removed_contract_ids=tuple(removed_contract_ids),
            removed_bytes=removed_bytes,
        )

    def _connect_for_write(self) -> sqlite3.Connection:
        """Open one fresh writer configured for WAL and a busy timeout."""
        connection = sqlite3.connect(self._database_path)
        try:
            configure_writable_sqlite_connection(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    def _connect_for_read(self) -> sqlite3.Connection | None:
        """Open one fresh read-only connection, or report absence.

        A database that was never written to does not exist, and a read must
        not create it; the existence probe keeps a miss from becoming an
        error, and read-only mode keeps it from becoming a file.

        A file whose table was never committed is the same absence.  SQLite
        creates the file on connect and ``initialize`` commits the table after,
        so anything interrupting that -- a full disk on the first publication,
        most plainly -- leaves a file holding nothing.  Reading it as an error
        rather than as nothing would be unrecoverable in a way no other miss
        is: the publication that would create the table is reached through a
        read, so the store could never initialize itself again.
        """
        if not self._database_path.exists():
            return None
        connection = sqlite3.connect(f"{self._database_path.as_uri()}?mode=ro", uri=True)
        try:
            found = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'artifacts'"
            ).fetchone()
        except BaseException:
            connection.close()
            raise
        if found:
            return connection
        connection.close()
        return None

    def _read_one(
        self,
        query: str,
        contract_id: str,
        *rest: str,
    ) -> tuple[Any, ...] | None:
        try:
            # Opening is inside the guard: a file holding no table is absence,
            # but a file that is not a database at all is still an error.
            connection = self._connect_for_read()
            if connection is None:
                return None
            with closing(connection):
                return connection.execute(query, (contract_id, *rest)).fetchone()
        except sqlite3.Error as exc:
            raise ArtifactStoreError(
                "Artifact read failed",
                operation="read",
                details={"contract_id": contract_id},
            ) from exc


def _select_publication(
    connection: sqlite3.Connection,
    contract_id: str,
) -> tuple[Any, ...] | None:
    return connection.execute(
        "SELECT body, pruned_reason, runtime_id, duration_ms, events_emitted_count"
        " FROM artifacts WHERE contract_id = ?",
        (contract_id,),
    ).fetchone()


def _resolve_existing_row(
    contract_id: str,
    row: tuple[Any, ...],
    body_text: str,
) -> DisposableResultEnvelope:
    """Decide what one already-published contract means for this arrival."""
    (
        stored_body,
        pruned_reason,
        stored_runtime_id,
        stored_duration_ms,
        stored_events_emitted_count,
    ) = row
    if pruned_reason is not None:
        raise ArtifactTombstonedError(
            "Contract artifact was pruned; allocate a new contract id to rerun",
            operation="write",
            details={"contract_id": contract_id},
        )
    if stored_body == body_text:
        # The stored publication won, so its provenance is the one on record --
        # its duration, its runtime and its event count, not whatever this
        # arriving call measured or claims to be.
        return _envelope(
            contract_id,
            runtime_id=stored_runtime_id,
            duration_ms=stored_duration_ms,
            events_emitted_count=stored_events_emitted_count,
        )
    raise ArtifactContractConflictError(
        "Contract id is already bound to a different artifact",
        operation="write",
        details={"contract_id": contract_id},
    )


def _envelope(
    contract_id: str,
    *,
    runtime_id: str,
    duration_ms: int,
    events_emitted_count: int,
) -> DisposableResultEnvelope:
    """Assemble the one envelope shape every store answer uses."""
    return DisposableResultEnvelope(
        contract_id=contract_id,
        result=DisposableResultSummary(status=_ENVELOPE_STATUS),
        runtime_id=runtime_id,
        duration_ms=duration_ms,
        events_emitted_count=events_emitted_count,
    )


def _tombstoned_read_error(contract_id: str, *, tombstoned_at: Any) -> ArtifactTombstonedError:
    return ArtifactTombstonedError(
        "Artifact was pruned; use an explicit force-rerun path to recompute it",
        operation="read",
        details={"contract_id": contract_id, "tombstoned_at": tombstoned_at},
    )


def _plan_prune(
    rows: list[tuple[Any, ...]],
    *,
    ttl: timedelta,
    allow_replay_tombstone: bool,
    now: datetime,
) -> list[ArtifactPruneCandidate]:
    """Plan prune decisions from the still-bodied rows, sorted for determinism.

    A row whose timestamp cannot be read ages as nothing rather than raising:
    unreadable here means externally rewritten, and pruning must fail toward
    keeping the body.
    """
    candidates: list[ArtifactPruneCandidate] = []
    for contract_id, body_bytes, created_at in rows:
        try:
            published_at = _as_utc(datetime.fromisoformat(created_at))
        except (TypeError, ValueError):
            continue
        age = now - published_at
        if age < ttl:
            continue
        if age < DEFAULT_REPLAY_RETENTION:
            if not allow_replay_tombstone:
                continue
            reason = "operator allowed replay tombstone before retention expiry"
        else:
            reason = "contract exceeded replay retention and artifact TTL"
        candidates.append(
            ArtifactPruneCandidate(
                contract_id=contract_id,
                age_seconds=max(0.0, age.total_seconds()),
                body_bytes=int(body_bytes),
                reason=reason,
            )
        )
    return sorted(candidates, key=lambda candidate: candidate.contract_id)


__all__ = [
    "DEFAULT_ARTIFACT_TTL",
    "DEFAULT_REPLAY_RETENTION",
    "ArtifactContractConflictError",
    "ArtifactIntegrityError",
    "ArtifactNotFoundError",
    "ArtifactPruneCandidate",
    "ArtifactPruneReport",
    "ArtifactStoreError",
    "ArtifactTombstonedError",
    "ArtifactTooLargeError",
    "ArtifactStore",
    "FetchedArtifact",
    "PublishedContract",
    "canonical_artifact_bytes",
]
