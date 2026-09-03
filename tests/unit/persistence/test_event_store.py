"""Unit tests for ouroboros.persistence.event_store module."""

import asyncio
from datetime import UTC, datetime, timedelta
import logging
from pathlib import Path
import shutil
import sqlite3

import pytest

from ouroboros.core.errors import PersistenceError
from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.execution_runtime_scope import normalize_execution_scope_id
from ouroboros.orchestrator.session import SessionRepository
from ouroboros.persistence.event_store import (
    EventStore,
    acceptance_generation_id_for_session,
)
from ouroboros.persistence.write_settlement import (
    await_sqlite_write_atomically as _await_sqlite_write_atomically,
)


@pytest.fixture
async def event_store(tmp_path):
    """Create an EventStore with an in-memory SQLite database."""
    db_path = tmp_path / "test_events.db"
    store = EventStore(f"sqlite+aiosqlite:///{db_path}")
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture
def sample_event() -> BaseEvent:
    """Create a sample event for testing."""
    return BaseEvent(
        type="ontology.concept.added",
        aggregate_type="ontology",
        aggregate_id="ont-123",
        data={"concept_name": "authentication", "weight": 1.0},
    )


class TestEventStoreInitialization:
    """Test EventStore initialization."""

    async def test_event_store_creates_tables(self, tmp_path) -> None:
        """EventStore.initialize() creates the events table."""
        db_path = tmp_path / "test_init.db"
        store = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await store.initialize()
        # If we get here without error, tables were created
        await store.close()

    async def test_sqlite_write_drain_survives_repeated_caller_cancellation(self) -> None:
        """A second cancellation must not cancel the shielded write cleanup task."""
        started = asyncio.Event()
        release = asyncio.Event()
        completed = False

        async def _write() -> str:
            nonlocal completed
            started.set()
            await release.wait()
            completed = True
            return "committed"

        task = asyncio.create_task(_await_sqlite_write_atomically(_write()))
        await asyncio.wait_for(started.wait(), timeout=1)

        task.cancel()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert completed is True

    async def test_event_store_can_be_initialized_multiple_times(self, tmp_path) -> None:
        """Calling initialize() multiple times is safe."""
        db_path = tmp_path / "test_multi_init.db"
        store = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await store.initialize()
        await store.initialize()  # Should not raise
        await store.close()

    async def test_durable_append_interrupts_lock_and_returns_after_rollback(
        self,
        tmp_path: Path,
        sample_event: BaseEvent,
    ) -> None:
        """The real SQLite path owns its deadline and cannot commit afterward."""
        db_path = tmp_path / "durable-deadline.db"
        store = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await store.initialize()
        blocker = sqlite3.connect(db_path, isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        started_at = asyncio.get_running_loop().time()
        try:
            with pytest.raises(TimeoutError):
                await store.append_durable(sample_event, timeout=0.08)
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()

        assert asyncio.get_running_loop().time() - started_at < 0.2
        await asyncio.sleep(0.05)
        assert await store.replay(sample_event.aggregate_type, sample_event.aggregate_id) == []
        await store.close()

    async def test_durable_append_refuses_unbounded_lazy_initialization(
        self,
        tmp_path: Path,
        sample_event: BaseEvent,
    ) -> None:
        """The hard append deadline never wraps cancellation-atomic schema setup."""
        store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'not-initialized.db'}")
        started_at = asyncio.get_running_loop().time()
        with pytest.raises(PersistenceError, match="requires an initialized EventStore"):
            await store.append_durable(sample_event, timeout=0.01)
        assert asyncio.get_running_loop().time() - started_at < 0.05

    async def test_durable_append_keeps_committed_authority_when_cleanup_fails(
        self,
        tmp_path: Path,
        sample_event: BaseEvent,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A post-commit driver cleanup error cannot report the append as absent."""
        from aiosqlite import Connection

        store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'post-commit-cleanup.db'}")
        await store.initialize()
        original_set_progress_handler = Connection.set_progress_handler

        async def fail_when_removing_progress_handler(
            connection: Connection,
            handler,
            steps: int,
        ) -> None:
            if handler is None:
                raise RuntimeError("simulated post-commit cleanup failure")
            await original_set_progress_handler(connection, handler, steps)

        monkeypatch.setattr(
            Connection,
            "set_progress_handler",
            fail_when_removing_progress_handler,
        )

        with caplog.at_level(logging.WARNING):
            await store.append_durable(sample_event, timeout=1.0)

        events = await store.replay(sample_event.aggregate_type, sample_event.aggregate_id)
        assert [event.id for event in events] == [sample_event.id]
        assert "event_store.append.post_commit_cleanup_failed" in caplog.text
        await store.close()

    async def test_durable_append_invalidates_connection_when_cleanup_is_cancelled(
        self,
        tmp_path: Path,
        sample_event: BaseEvent,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cleanup deadline after commit cannot return a poisoned driver to the pool."""
        from aiosqlite import Connection

        store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'cleanup-timeout.db'}")
        await store.initialize()
        original_set_progress_handler = Connection.set_progress_handler
        configured_drivers: list[Connection] = []
        cleanup_started = asyncio.Event()

        async def delay_progress_handler_removal(
            connection: Connection,
            handler,
            steps: int,
        ) -> None:
            if handler is None:
                cleanup_started.set()
                await asyncio.sleep(1.0)
            else:
                configured_drivers.append(connection)
            await original_set_progress_handler(connection, handler, steps)

        monkeypatch.setattr(
            Connection,
            "set_progress_handler",
            delay_progress_handler_removal,
        )

        await store.append_durable(sample_event, timeout=0.2)
        assert cleanup_started.is_set()
        assert len(configured_drivers) == 1

        monkeypatch.setattr(Connection, "set_progress_handler", original_set_progress_handler)
        events = await store.replay(sample_event.aggregate_type, sample_event.aggregate_id)
        assert [event.id for event in events] == [sample_event.id]

        engine = store._engine
        assert engine is not None
        async with engine.connect() as conn:
            raw = await conn.get_raw_connection()
            assert raw.driver_connection is not configured_drivers[0]
            result = await conn.exec_driver_sql(
                "WITH RECURSIVE count(x) AS ("
                "SELECT 1 UNION ALL SELECT x + 1 FROM count WHERE x < 2000"
                ") SELECT sum(x) FROM count"
            )
            assert result.scalar_one() == 2_001_000
        await store.close()

    async def test_repeated_initialize_settles_schema_transaction_before_cancellation(
        self,
        monkeypatch,
    ) -> None:
        """A bounded caller cannot abandon repeated create_all mid-transaction."""
        from sqlalchemy import event as sa_event
        from sqlalchemy.ext.asyncio import AsyncConnection

        from ouroboros.persistence import lineage_claims

        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()
        engine = store._engine
        assert engine is not None
        schema_started = asyncio.Event()
        release_schema = asyncio.Event()
        invalidations: list[BaseException | None] = []
        original_run_sync = AsyncConnection.run_sync

        async def gated_run_sync(connection, function, *args, **kwargs):  # type: ignore[no-untyped-def]
            schema_started.set()
            await release_schema.wait()
            return await original_run_sync(connection, function, *args, **kwargs)

        monkeypatch.setattr(AsyncConnection, "run_sync", gated_run_sync)
        sa_event.listen(
            engine.sync_engine,
            "invalidate",
            lambda _connection, _record, error: invalidations.append(error),
        )
        pending = asyncio.create_task(store.initialize())
        try:
            await asyncio.wait_for(schema_started.wait(), timeout=1.0)
            pending.cancel()
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()

            release_schema.set()
            with pytest.raises(asyncio.CancelledError):
                await pending

            assert engine.sync_engine.pool.checkedout() == 0
            assert invalidations == []
            claim = await lineage_claims.try_acquire(
                store,
                scope="initialize-cancel",
                lineage_id="post-initialize-cancel",
                generation_number=1,
                owner_id="owner",
                request_key="request",
            )
            assert claim is not None and claim.acquired
            await lineage_claims.release(
                store,
                scope="initialize-cancel",
                lineage_id="post-initialize-cancel",
                owner_id="owner",
            )
        finally:
            release_schema.set()
            if not pending.done():
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            await store.close()

    async def test_in_memory_store_shares_schema_across_concurrent_connections(self) -> None:
        """`:memory:` stores must not lose schema across async connection checkout."""
        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()

        async def append_one(index: int) -> None:
            await store.append(
                BaseEvent(
                    type="test.event.created",
                    aggregate_type="test",
                    aggregate_id="memory",
                    data={"index": index},
                )
            )

        await asyncio.gather(*(append_one(index) for index in range(5)))
        events = await store.replay("test", "memory")

        assert len(events) == 5
        await store.close()

    @pytest.mark.parametrize(
        "database_url",
        (
            "sqlite+aiosqlite://",
            "sqlite+aiosqlite:///",
            "sqlite+aiosqlite:///?cache=shared",
            "sqlite+aiosqlite:///:memory:",
            "sqlite+aiosqlite:///:memory:?cache=shared",
        ),
        ids=(
            "pathless-none",
            "pathless-empty",
            "pathless-query",
            "memory",
            "memory-query",
        ),
    )
    async def test_memory_store_connections_own_their_transactions(
        self,
        database_url: str,
    ) -> None:
        """Accepted in-memory URLs must not interleave connection transactions."""
        store = EventStore(database_url)
        await store.initialize()
        engine = store._engine
        assert engine is not None

        try:
            async with engine.connect() as reader:
                await reader.exec_driver_sql("BEGIN")
                await reader.exec_driver_sql("SELECT 1")

                async with engine.connect() as writer:
                    await writer.exec_driver_sql("BEGIN IMMEDIATE")
                    await writer.rollback()

                await reader.rollback()
        finally:
            await store.close()

    async def test_named_memory_store_preserves_identity_and_anchor_lifetime(self) -> None:
        """Named stores share identity until their final keepalive closes."""
        from ouroboros.evolution.generation_claims import step_claims_for

        database_url = "sqlite+aiosqlite:///file:named-anchor?mode=memory&cache=shared&uri=true"
        reordered_url = "sqlite+aiosqlite:///file:named-anchor?uri=true&cache=shared&mode=memory"
        percent_encoded_url = (
            "sqlite+aiosqlite:///file:named-%61nchor?mode=memory&cache=shared&uri=true"
        )
        first = EventStore(database_url)
        second = EventStore(reordered_url)
        encoded = EventStore(percent_encoded_url)
        different = EventStore(
            "sqlite+aiosqlite:///file:named-other?mode=memory&cache=shared&uri=true"
        )
        assert first.database_url == second.database_url
        assert first.database_url == encoded.database_url
        assert first.database_url != different.database_url
        assert "vfs=memdb" in first.database_url
        assert "mode=memory" not in first.database_url
        assert first.supports_cross_process_workers is False
        await first.initialize()
        await second.initialize()
        await encoded.initialize()
        await different.initialize()
        first_claims = step_claims_for(first)
        second_claims = step_claims_for(second)
        assert first_claims is second_claims
        assert first_claims is not step_claims_for(different)

        claim_outcomes = await asyncio.gather(
            first_claims.acquire("named-claim", "first"),
            second_claims.acquire("named-claim", "second"),
        )
        assert sorted(claim_outcomes) == [False, True]
        winner = "first" if claim_outcomes[0] else "second"
        await first_claims.release("named-claim", winner)

        event = BaseEvent(
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="named-anchor",
            data={},
        )
        try:
            await first.append(event)
            assert len(await second.replay("test", "named-anchor")) == 1
            assert len(await encoded.replay("test", "named-anchor")) == 1
            assert await different.replay("test", "named-anchor") == []

            import subprocess
            import sys

            from ouroboros.persistence.sqlite_memory import named_memory_keepalive_uri

            raw_uri = named_memory_keepalive_uri(first.database_url)
            assert raw_uri is not None
            child = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sqlite3,sys; c=sqlite3.connect(sys.argv[1], uri=True); "
                        'print(c.execute("SELECT count(*) FROM sqlite_master '
                        "WHERE type='table' AND name='events'\").fetchone()[0])"
                    ),
                    raw_uri,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            assert child.stdout.strip() == "0", "named memory must remain process-local"

            engine = first._engine
            assert engine is not None
            async with engine.connect() as connection:
                await connection.invalidate()
            assert len(await first.replay("test", "named-anchor")) == 1

            await first.close()
            assert len(await second.replay("test", "named-anchor")) == 1
        finally:
            await first.close()
            await second.close()
            await encoded.close()
            await different.close()

        reopened = EventStore(database_url)
        await reopened.initialize()
        try:
            assert await reopened.replay("test", "named-anchor") == []
        finally:
            await reopened.close()

    async def test_standard_shared_memory_uri_is_anchored_and_creates_no_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SQLite's standard shared-memory URI survives pooled invalidation."""
        monkeypatch.chdir(tmp_path)
        database_url = "sqlite+aiosqlite:///file::memory:?cache=shared&uri=true"
        reordered_url = "sqlite+aiosqlite:///file::memory:?uri=true&cache=shared"
        first = EventStore(database_url)
        second = EventStore(reordered_url)
        canonical_url = first.database_url
        canonical = EventStore(canonical_url)
        assert first.database_url == second.database_url
        assert canonical.database_url == canonical_url
        assert "vfs=memdb" in first.database_url
        assert first.sqlite_path() is None
        assert first.supports_cross_process_workers is False

        await first.initialize()
        try:
            await first.append(
                BaseEvent(
                    type="test.event.created",
                    aggregate_type="test",
                    aggregate_id="standard-shared-memory",
                    data={},
                )
            )

            engine = first._engine
            assert engine is not None
            async with engine.connect() as connection:
                await connection.invalidate()
            assert len(await first.replay("test", "standard-shared-memory")) == 1

            await second.initialize()
            assert len(await second.replay("test", "standard-shared-memory")) == 1
            await canonical.initialize()
            assert len(await canonical.replay("test", "standard-shared-memory")) == 1
            await first.close()
            assert len(await second.replay("test", "standard-shared-memory")) == 1
        finally:
            await first.close()
            await second.close()
            await canonical.close()

        reopened = EventStore(canonical_url)
        await reopened.initialize()
        try:
            assert await reopened.replay("test", "standard-shared-memory") == []
        finally:
            await reopened.close()
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize(
        "query",
        (
            "mode=ro&uri=true&vfs=memdb",
            "timeout=1&uri=true&vfs=memdb",
            "uri=true",
            "vfs=memdb",
            "uri=True&vfs=memdb",
            "uri=true&vfs=MEMDB",
            "uri=true&vfs=memdb&",
        ),
    )
    def test_canonical_named_memdb_variants_fail_closed_without_artifacts(
        self,
        query: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Canonical memdb URLs never discard caller query semantics."""
        monkeypatch.chdir(tmp_path)
        identity = "a" * 64
        database_url = f"sqlite+aiosqlite:///file:/ouroboros-named-{identity}?{query}"
        for read_only in (False, True):
            with pytest.raises(ValueError, match="Unsupported canonical named-memory"):
                EventStore(database_url, read_only=read_only)
        assert list(tmp_path.iterdir()) == []

    def test_standard_shared_memory_uri_rejects_read_only_target_swap(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Read-only mode must not reinterpret ``file::memory:`` as a disk target."""
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ValueError, match="Read-only named in-memory"):
            EventStore(
                "sqlite+aiosqlite:///file::memory:?cache=shared&uri=true",
                read_only=True,
            )
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize(
        "database_url",
        (
            "sqlite+aiosqlite:///file::MEMORY:?cache=shared&uri=true",
            "sqlite+aiosqlite:///file::memory:?cache=SHARED&uri=true",
            "sqlite+aiosqlite:///file::memory:?cache=shared&uri=True",
            "sqlite+aiosqlite:///file::memory:?cache=shared&URI=true",
            "sqlite+aiosqlite:///file::memory:?cache=shared",
            "sqlite+aiosqlite:///file::memory:?uri=true",
            "sqlite+aiosqlite:///file::memory:?cache=private&uri=true",
            "sqlite+aiosqlite:///file::memory:?cache=shared&uri=true&timeout=1",
            "sqlite+aiosqlite:///file::memory:?cache=shared&mode=ro&uri=true",
            "sqlite+aiosqlite:///file::memory:?cache=shared&uri=true&x=",
            "sqlite+aiosqlite:///file::memory:?cache=shared&uri=true&x",
            "sqlite+aiosqlite:///file::memory:?cache=shared&uri=true&",
            "sqlite+aiosqlite:///file::memory:?cache=shared&&uri=true",
            "sqlite+aiosqlite:///file::memory:?cache=%73hared&uri=true",
            "sqlite+aiosqlite:///file::%6demory:?cache=shared&uri=true",
            "sqlite+aiosqlite:///file::%256demory:?cache=shared&uri=true",
            "sqlite+aiosqlite:///file::%25256demory:?cache=shared&uri=true",
            "sqlite+aiosqlite:///%66ile::memory:?cache=shared&uri=true",
            "sqlite+aiosqlite:///file%3A%3Amemory%3A?cache=shared&uri=true",
            "sqlite+aiosqlite:///file::memory:%ZZ?cache=shared&uri=true",
            "sqlite+aiosqlite:///file::memory:suffix?cache=shared&uri=true",
            "sqlite+aiosqlite:///file::memory:%00?cache=shared&uri=true",
            "sqlite+aiosqlite:///file::%" + ("25" * 31) + "6demory:?cache=shared&uri=true",
        ),
    )
    def test_standard_shared_memory_uri_variants_fail_closed_without_artifacts(
        self,
        database_url: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Malformed and extended URI variants never fall through to disk SQLite."""
        monkeypatch.chdir(tmp_path)
        for read_only in (False, True):
            with pytest.raises(ValueError, match="Unsupported SQLite shared-memory URI"):
                EventStore(database_url, read_only=read_only)
        assert list(tmp_path.iterdir()) == []

    def test_ordinary_percent_literal_filename_is_not_reserved(self, tmp_path: Path) -> None:
        """Percent escapes outside the reserved target retain the disk URL contract."""
        database_url = f"sqlite+aiosqlite:///{tmp_path / 'report%25complete.db'}"
        store = EventStore(database_url)
        assert store.database_url == database_url

    async def test_named_memory_store_requires_memdb_vfs_support(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Ancient SQLite must fail explicitly instead of using shared-cache fallback."""
        monkeypatch.setattr("sqlite3.sqlite_version_info", (3, 35, 5))
        store = EventStore("sqlite+aiosqlite:///file:old?mode=memory&cache=shared&uri=true")
        with pytest.raises(PersistenceError, match="SQLite 3.36 or newer"):
            await store.initialize()

    async def test_external_memdb_uri_is_canonicalized_without_disk_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An external memdb URL is canonicalized before SQLite can create a file."""
        monkeypatch.chdir(tmp_path)
        store = EventStore("sqlite+aiosqlite:///file:external-memdb?vfs=memdb&uri=true")
        await store.initialize()
        await store.close()
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize(
        "database_url",
        (
            "sqlite+aiosqlite:///file:logical?mode=memory&cache=shared&uri=false",
            "sqlite+aiosqlite:///file:logical?mode=memory&cache=shared",
            "sqlite+aiosqlite:///file:logical?mode=memory&cache=shared&URI=true",
            "sqlite+aiosqlite:///file:logical?mode=memory&cache=shared&uri=True",
            "sqlite+aiosqlite:///file:logical?mode=memory&uri=true",
            "sqlite+aiosqlite:///file:logical?mode=memory&cache=private&uri=true",
            "sqlite+aiosqlite:///file:logical?mode=memory&cache=shared&uri=true&timeout=1",
            "sqlite+aiosqlite:///file:logical?mode=memory&cache=shared&uri=true&mode=ro",
            "sqlite+aiosqlite:///file:logical?mode=memory&cache=shared&uri=true&immutable=1",
            "sqlite+aiosqlite:///file:logical?mode=memory&cache=shared&uri=true&nolock=1",
            "sqlite+aiosqlite:///file:logical?mode=memory&mode=memory&cache=shared&uri=true",
            "sqlite+aiosqlite:///file:logical?%6dode=memory&cache=shared&uri=true",
            "sqlite+aiosqlite:///file:logical?mode=%6demory&cache=shared&uri=true",
            "sqlite+aiosqlite:///file:logical?%252525252525252525256dode=memory"
            "&cache=shared&uri=true",
            "sqlite+aiosqlite:///file:logical?mode=%252525252525252525256demory"
            "&cache=shared&uri=true",
            "sqlite+aiosqlite:///logical?mode=memory&cache=shared&uri=true",
            "sqlite+aiosqlite:///file:logical?mode=MEMORY&cache=shared&uri=true",
            "sqlite+aiosqlite:///file:logical?vfs=memdb&uri=false",
            "sqlite+aiosqlite:///file:logical?vfs=memdb",
            "sqlite+aiosqlite:///file:logical?vfs=memdb&URI=true",
            "sqlite+aiosqlite:///file:logical?vfs=memdb&uri=True",
            "sqlite+aiosqlite:///file:logical?vfs=memdb&uri=true&timeout=1",
            "sqlite+aiosqlite:///file:logical?vfs=memdb&vfs=memdb&uri=true",
            "sqlite+aiosqlite:///file:logical?%76fs=memdb&uri=true",
            "sqlite+aiosqlite:///file:logical?vfs=%6demdb&uri=true",
            "sqlite+aiosqlite:///logical?vfs=memdb&uri=true",
            "sqlite+aiosqlite:///file:logical?vfs=MEMDB&uri=true",
        ),
    )
    def test_named_memory_noncanonical_queries_fail_closed_without_artifacts(
        self,
        database_url: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Memory intent is never reclassified by enabling URI mode or dropping options."""
        monkeypatch.chdir(tmp_path)
        for read_only in (False, True):
            with pytest.raises(ValueError, match="Unsupported named-memory SQLite URI"):
                EventStore(database_url, read_only=read_only)
        assert list(tmp_path.iterdir()) == []

    async def test_non_memory_uri_false_remains_a_durable_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Ordinary URI text is not rejected or silently converted into volatile memory."""
        monkeypatch.chdir(tmp_path)
        database_url = "sqlite+aiosqlite:///file:ordinary?uri=false"
        first = EventStore(database_url)
        assert first.database_url == database_url
        assert first.sqlite_path() == "file:ordinary"
        assert first.supports_cross_process_workers is True
        await first.initialize()
        await first.append(
            BaseEvent(
                type="test.event.created",
                aggregate_type="test",
                aggregate_id="ordinary-uri-false",
                data={},
            )
        )
        await first.close()

        reopened = EventStore(database_url)
        await reopened.initialize()
        try:
            assert len(await reopened.replay("test", "ordinary-uri-false")) == 1
        finally:
            await reopened.close()

        read_only = EventStore(database_url, read_only=True)
        assert read_only.sqlite_path() == "file:ordinary"
        await read_only.initialize()
        try:
            assert len(await read_only.replay("test", "ordinary-uri-false")) == 1
        finally:
            await read_only.close()
        assert (tmp_path / "file:ordinary").is_file()

    def test_deep_encoded_ordinary_query_is_not_memory_intent(self, tmp_path: Path) -> None:
        database_url = f"sqlite+aiosqlite:///{tmp_path / 'ordinary.db'}?tag=%2525252525252525252561"
        store = EventStore(database_url)
        assert store.database_url == database_url
        assert store.sqlite_path() == str(tmp_path / "ordinary.db")

    @pytest.mark.parametrize("uri_value", ("1", "True", "yes", "on"))
    async def test_noncanonical_true_uri_values_preserve_read_only_target(
        self,
        uri_value: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        database_url = f"sqlite+aiosqlite:///file:ordinary-{uri_value}?uri={uri_value}"
        writer = EventStore(database_url)
        await writer.initialize()
        await writer.append(
            BaseEvent(
                type="test.event.created",
                aggregate_type="test",
                aggregate_id=uri_value,
                data={},
            )
        )
        await writer.close()

        reader = EventStore(database_url, read_only=True)
        await reader.initialize()
        try:
            assert len(await reader.replay("test", uri_value)) == 1
        finally:
            await reader.close()
        assert (tmp_path / f"ordinary-{uri_value}").is_file()

    def test_named_memory_rejects_read_only_target_swap(self, tmp_path: Path) -> None:
        """Read-only named memory must never become an unrelated disk database."""
        monkeypatch_target = tmp_path / "named-ro"
        with pytest.raises(ValueError, match="Read-only named in-memory"):
            EventStore(
                f"sqlite+aiosqlite:///file:{monkeypatch_target}?mode=memory&cache=shared&uri=true",
                read_only=True,
            )
        assert not monkeypatch_target.exists()

    @pytest.mark.parametrize(
        "database_url",
        (
            "sqlite+aiosqlite:///?cache=shared",
            "sqlite+aiosqlite:///:memory:?cache=shared",
        ),
    )
    async def test_read_only_anonymous_memory_creates_no_disk_target(
        self,
        database_url: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Anonymous query parameters must not masquerade as a file path."""
        monkeypatch.chdir(tmp_path)
        store = EventStore(database_url, read_only=True)
        assert store.database_url == database_url
        assert store.sqlite_path() is None
        await store.initialize()
        await store.close()
        assert list(tmp_path.iterdir()) == []

    async def test_read_only_explicit_file_uri_query_opens_the_real_database(
        self, tmp_path: Path
    ) -> None:
        """Explicit file URI parameters are not part of the SQLite file path."""
        database = tmp_path / "query-target.db"
        writer = EventStore(f"sqlite+aiosqlite:///{database}")
        await writer.initialize()
        await writer.append(
            BaseEvent(
                type="test.event.created",
                aggregate_type="test",
                aggregate_id="readonly-query",
                data={},
            )
        )
        await writer.close()

        reader = EventStore(
            f"sqlite+aiosqlite:///file:{database}?timeout=1&uri=true",
            read_only=True,
        )
        await reader.initialize()
        try:
            assert reader.sqlite_path() == str(database)
            assert len(await reader.replay("test", "readonly-query")) == 1
        finally:
            await reader.close()

    async def test_memory_store_concurrent_rollback_cannot_void_append(self, monkeypatch) -> None:
        """The #1566/#1576 CI append-void, reproduced deterministically.

        StaticPool handed the SAME aiosqlite connection to concurrent
        ``engine.begin()`` blocks with no exclusivity. SQLite transactions are
        connection-scoped, so a concurrent block exiting via exception (e.g. a
        cancelled monitor mid-SELECT) issued a ROLLBACK that discarded another
        task's uncommitted INSERT — whose own COMMIT then no-oped and append()
        reported success while the event vanished (no exception, no log).

        A slowed commit widens the INSERT→COMMIT window like a loaded CI
        runner. With the memdb-backed store each block runs on its OWN
        connection, so the failing block's rollback cannot touch the append's
        transaction.

        Fails on StaticPool (0 events persisted despite successful append);
        passes with the process-shared memdb-backed store.
        """
        import contextlib

        import aiosqlite

        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()

        commit_delay = 0.1
        orig_commit = aiosqlite.Connection.commit

        async def slow_commit(self):
            await asyncio.sleep(commit_delay)
            return await orig_commit(self)

        monkeypatch.setattr(aiosqlite.Connection, "commit", slow_commit)

        event = BaseEvent(
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="void",
            data={},
        )

        async def concurrent_failing_block() -> None:
            # Land between the append's INSERT and its COMMIT.
            await asyncio.sleep(commit_delay / 2)
            with contextlib.suppress(RuntimeError):
                async with store._engine.begin():
                    raise RuntimeError("simulated cancelled concurrent block")

        await asyncio.gather(store.append(event), concurrent_failing_block())
        monkeypatch.setattr(aiosqlite.Connection, "commit", orig_commit)

        events = await store.replay("test", "void")
        assert len(events) == 1, (
            "append() reported success but the event vanished (silent rollback "
            "by a concurrent transaction sharing the :memory: connection)"
        )
        await store.close()


class TestEventStoreAppend:
    """Test EventStore.append() method."""

    @staticmethod
    async def _seed_session_start(
        event_store: EventStore,
        *,
        session_id: str,
        execution_id: str,
        acceptance_root_indices: list[int] | None = None,
    ) -> None:
        data = {
            "execution_id": execution_id,
            "seed_id": "seed-acceptance",
            "start_time": datetime.now(UTC).isoformat(),
        }
        if acceptance_root_indices is not None:
            data["acceptance_root_indices"] = acceptance_root_indices
        await event_store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id=session_id,
                data=data,
            )
        )

    async def test_append_stores_event(
        self, event_store: EventStore, sample_event: BaseEvent
    ) -> None:
        """append() successfully stores an event."""
        await event_store.append(sample_event)
        # Verify by replaying
        events = await event_store.replay("ontology", "ont-123")
        assert len(events) == 1
        assert events[0].id == sample_event.id

    async def test_append_multiple_events(self, event_store: EventStore) -> None:
        """append() can store multiple events."""
        events_to_store = [
            BaseEvent(
                type="ontology.concept.added",
                aggregate_type="ontology",
                aggregate_id="ont-123",
                data={"concept_name": f"concept_{i}"},
            )
            for i in range(5)
        ]

        for event in events_to_store:
            await event_store.append(event)

        replayed = await event_store.replay("ontology", "ont-123")
        assert len(replayed) == 5

    async def test_append_preserves_the_first_terminal_session_event(
        self, event_store: EventStore
    ) -> None:
        """Generic append callers cannot overwrite an earlier terminal winner."""
        cancelled = BaseEvent(
            type="orchestrator.session.cancelled",
            aggregate_type="session",
            aggregate_id="sess-terminal-winner",
            data={"reason": "user cancelled"},
        )
        completed = BaseEvent(
            type="orchestrator.session.completed",
            aggregate_type="session",
            aggregate_id="sess-terminal-winner",
            data={"summary": {"would_have_overwritten": True}},
        )

        await event_store.append(cancelled)
        await event_store.append(completed)

        terminal_events = await event_store.replay("session", "sess-terminal-winner")
        assert [event.type for event in terminal_events] == ["orchestrator.session.cancelled"]

    async def test_session_terminal_marker_reports_when_another_transition_won(
        self, event_store: EventStore
    ) -> None:
        """Legacy markers must not report a losing terminal transition as success."""
        repository = SessionRepository(event_store)
        await repository.create_session(
            execution_id="exec-terminal-marker-race",
            seed_id="seed-terminal-marker-race",
            session_id="sess-terminal-marker-race",
        )

        completed = await repository.mark_completed("sess-terminal-marker-race")
        cancelled = await repository.mark_cancelled(
            "sess-terminal-marker-race",
            reason="late cancellation",
        )

        assert completed.is_ok and completed.value is True
        assert cancelled.is_ok and cancelled.value is False
        terminal_events = await event_store.replay("session", "sess-terminal-marker-race")
        assert [event.type for event in terminal_events] == [
            "orchestrator.session.started",
            "orchestrator.session.completed",
        ]

    async def test_raw_terminal_append_paths_are_rejected(self, event_store: EventStore) -> None:
        """Terminal sessions must use the one-winner public append boundary."""
        terminal = BaseEvent(
            type="orchestrator.session.failed",
            aggregate_type="session",
            aggregate_id="sess-raw-terminal-path",
            data={"error": "would bypass terminal CAS"},
        )
        benign = BaseEvent(
            type="orchestrator.progress.updated",
            aggregate_type="session",
            aggregate_id="sess-raw-terminal-path",
            data={"progress": {"messages_processed": 1}},
        )

        with pytest.raises(PersistenceError, match="one-winner guard"):
            await event_store.append_with_rowid(terminal)
        with pytest.raises(PersistenceError, match="cannot be appended in a batch"):
            await event_store.append_batch([benign, terminal])

        assert await event_store.replay("session", "sess-raw-terminal-path") == []

    @staticmethod
    def _acceptance_event(**overrides: object) -> BaseEvent:
        data: dict[str, object] = {
            "execution_id": "exec-acceptance-guard",
            "session_id": "sess-acceptance-guard",
            "acceptance_generation_id": acceptance_generation_id_for_session(
                "sess-acceptance-guard", "exec-acceptance-guard"
            ),
            "root_ac_index": 0,
            "final_retry_attempt": 1,
            "accepted": True,
            "disposition": "accepted",
            "outcome": "succeeded",
            "terminal_status": "completed",
        }
        data.update(overrides)
        return BaseEvent(
            type="execution.ac.acceptance_finalized",
            aggregate_type="execution",
            aggregate_id=str(data["execution_id"]),
            data=data,
        )

    async def test_final_acceptance_is_idempotent_and_guarded(
        self, event_store: EventStore
    ) -> None:
        payload = self._acceptance_event(
            execution_id="exec-acceptance-idempotent",
            session_id="sess-acceptance-idempotent",
            acceptance_generation_id=acceptance_generation_id_for_session(
                "sess-acceptance-idempotent", "exec-acceptance-idempotent"
            ),
        ).data
        terminal = BaseEvent(
            type="orchestrator.session.completed",
            aggregate_type="session",
            aggregate_id="sess-acceptance-idempotent",
            data={"acceptance_finalizations": [payload, dict(payload)]},
        )

        await self._seed_session_start(
            event_store,
            session_id="sess-acceptance-idempotent",
            execution_id="exec-acceptance-idempotent",
        )
        assert await event_store.append(terminal) is True

        replayed = await event_store.replay("execution", payload["execution_id"])
        assert [item.type for item in replayed] == ["execution.ac.acceptance_finalized"]

    async def test_conflicting_final_acceptance_fails_closed(self, event_store: EventStore) -> None:
        payload = self._acceptance_event(
            execution_id="exec-acceptance-conflict",
            session_id="sess-acceptance-conflict",
            acceptance_generation_id=acceptance_generation_id_for_session(
                "sess-acceptance-conflict", "exec-acceptance-conflict"
            ),
        )
        conflicting = {**payload.data, "marker": "changed"}
        terminal = BaseEvent(
            type="orchestrator.session.completed",
            aggregate_type="session",
            aggregate_id="sess-acceptance-conflict",
            data={"acceptance_finalizations": [payload.data, conflicting]},
        )

        await self._seed_session_start(
            event_store,
            session_id="sess-acceptance-conflict",
            execution_id="exec-acceptance-conflict",
        )
        with pytest.raises(PersistenceError, match="Conflicting final acceptance"):
            await event_store.append(terminal)

        assert [
            event.type for event in await event_store.replay("session", terminal.aggregate_id)
        ] == ["orchestrator.session.started"]
        assert await event_store.replay("execution", payload.aggregate_id) == []

    async def test_terminal_acceptance_plan_is_atomic(self, event_store: EventStore) -> None:
        """Terminal CAS and all planned root decisions commit together."""
        payload = self._acceptance_event(
            execution_id="exec-acceptance-plan",
            session_id="sess-acceptance-plan",
            acceptance_generation_id=acceptance_generation_id_for_session(
                "sess-acceptance-plan", "exec-acceptance-plan"
            ),
        ).data
        terminal = BaseEvent(
            type="orchestrator.session.completed",
            aggregate_type="session",
            aggregate_id="sess-acceptance-plan",
            data={"acceptance_finalizations": [payload]},
        )

        await self._seed_session_start(
            event_store,
            session_id="sess-acceptance-plan",
            execution_id="exec-acceptance-plan",
        )
        assert await event_store.append(terminal) is True
        assert sorted(
            event.type for event in await event_store.replay("session", terminal.aggregate_id)
        ) == sorted(["orchestrator.session.started", "orchestrator.session.completed"])
        assert [
            event.type for event in await event_store.replay("execution", payload["execution_id"])
        ] == ["execution.ac.acceptance_finalized"]

    @pytest.mark.parametrize(
        "roots",
        [[], [0], [0, 2], [0, 1, 2]],
    )
    async def test_current_terminal_plan_requires_exact_root_set(
        self,
        event_store: EventStore,
        roots: list[int],
    ) -> None:
        session_id = "sess-root-set"
        execution_id = "exec-root-set"

        def payload(root: int) -> dict[str, object]:
            return self._acceptance_event(
                execution_id=execution_id,
                session_id=session_id,
                acceptance_generation_id=acceptance_generation_id_for_session(
                    session_id, execution_id
                ),
                root_ac_index=root,
                accepted=False,
                disposition="failed",
                outcome="failed",
                terminal_status="failed",
            ).data

        await self._seed_session_start(
            event_store,
            session_id=session_id,
            execution_id=execution_id,
            acceptance_root_indices=[0, 1],
        )
        terminal = BaseEvent(
            type="orchestrator.session.failed",
            aggregate_type="session",
            aggregate_id=session_id,
            data={"acceptance_finalizations": [payload(root) for root in roots]},
        )
        with pytest.raises(PersistenceError, match="exactly match"):
            await event_store.append(terminal)

        assert [event.type for event in await event_store.replay("session", session_id)] == [
            "orchestrator.session.started"
        ]

    async def test_current_zero_root_session_requires_explicit_empty_plan(
        self,
        event_store: EventStore,
    ) -> None:
        session_id = "sess-zero-root-plan"
        execution_id = "exec-zero-root-plan"
        await self._seed_session_start(
            event_store,
            session_id=session_id,
            execution_id=execution_id,
            acceptance_root_indices=[],
        )

        with pytest.raises(PersistenceError, match="explicit acceptance plan"):
            await event_store.append(
                BaseEvent(
                    type="orchestrator.session.completed",
                    aggregate_type="session",
                    aggregate_id=session_id,
                    data={},
                )
            )

        assert (
            await event_store.append(
                BaseEvent(
                    type="orchestrator.session.completed",
                    aggregate_type="session",
                    aggregate_id=session_id,
                    data={"acceptance_finalizations": []},
                )
            )
            is True
        )

    async def test_current_terminal_plan_accepts_exact_durable_root_set(
        self,
        event_store: EventStore,
    ) -> None:
        session_id = "sess-exact-root-set"
        execution_id = "exec-exact-root-set"
        await self._seed_session_start(
            event_store,
            session_id=session_id,
            execution_id=execution_id,
            acceptance_root_indices=[0, 1],
        )
        plan = [
            self._acceptance_event(
                execution_id=execution_id,
                session_id=session_id,
                acceptance_generation_id=acceptance_generation_id_for_session(
                    session_id, execution_id
                ),
                root_ac_index=root,
                accepted=False,
                disposition="failed",
                outcome="failed",
                terminal_status="failed",
            ).data
            for root in (0, 1)
        ]

        assert (
            await event_store.append(
                BaseEvent(
                    type="orchestrator.session.failed",
                    aggregate_type="session",
                    aggregate_id=session_id,
                    data={"acceptance_finalizations": plan},
                )
            )
            is True
        )
        assert len(await event_store.replay("execution", execution_id)) == 2

    async def test_terminal_acceptance_binds_execution_to_session_start(
        self, event_store: EventStore
    ) -> None:
        await self._seed_session_start(
            event_store,
            session_id="sess-identity-bound",
            execution_id="exec-real",
        )
        payload = self._acceptance_event(
            execution_id="exec-other",
            session_id="sess-identity-bound",
            acceptance_generation_id=acceptance_generation_id_for_session(
                "sess-identity-bound", "exec-other"
            ),
        ).data
        terminal = BaseEvent(
            type="orchestrator.session.completed",
            aggregate_type="session",
            aggregate_id="sess-identity-bound",
            data={"acceptance_finalizations": [payload]},
        )

        with pytest.raises(PersistenceError, match="durable session start"):
            await event_store.append(terminal)

        assert await event_store.replay("execution", "exec-other") == []

    async def test_terminal_acceptance_plan_rolls_back_on_conflict(
        self, event_store: EventStore
    ) -> None:
        """A contradictory plan cannot leave a terminal session without its winner."""
        existing = self._acceptance_event(
            execution_id="exec-acceptance-plan-conflict",
            session_id="sess-acceptance-plan-conflict",
            acceptance_generation_id=acceptance_generation_id_for_session(
                "sess-acceptance-plan-conflict", "exec-acceptance-plan-conflict"
            ),
        )
        conflicting = dict(
            existing.data,
            marker="changed",
        )
        terminal = BaseEvent(
            type="orchestrator.session.completed",
            aggregate_type="session",
            aggregate_id="sess-acceptance-plan-conflict",
            data={"acceptance_finalizations": [existing.data, conflicting]},
        )

        await self._seed_session_start(
            event_store,
            session_id="sess-acceptance-plan-conflict",
            execution_id="exec-acceptance-plan-conflict",
        )
        with pytest.raises(PersistenceError, match="Conflicting final acceptance"):
            await event_store.append(terminal)
        assert [
            event.type for event in await event_store.replay("session", terminal.aggregate_id)
        ] == ["orchestrator.session.started"]

    async def test_acceptance_digest_includes_additional_payload_fields(
        self, event_store: EventStore
    ) -> None:
        """Extra final-event fields are part of fail-closed idempotence."""
        event = self._acceptance_event(
            execution_id="exec-acceptance-digest",
            session_id="sess-acceptance-digest",
            acceptance_generation_id=acceptance_generation_id_for_session(
                "sess-acceptance-digest", "exec-acceptance-digest"
            ),
        )
        conflicting = {**event.data, "marker": "changed"}
        terminal = BaseEvent(
            type="orchestrator.session.completed",
            aggregate_type="session",
            aggregate_id="sess-acceptance-digest",
            data={"acceptance_finalizations": [event.data, conflicting]},
        )

        await self._seed_session_start(
            event_store,
            session_id="sess-acceptance-digest",
            execution_id="exec-acceptance-digest",
        )
        with pytest.raises(PersistenceError, match="Conflicting final acceptance"):
            await event_store.append(terminal)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"acceptance_generation_id": ""},
            {"root_ac_index": -1},
            {"accepted": "true"},
            {"final_retry_attempt": -1},
            {"terminal_status": ""},
            {"terminal_status": "paused"},
            {"disposition": "rejected"},
            {"outcome": "failed"},
        ],
    )
    async def test_malformed_final_acceptance_is_rejected(
        self, event_store: EventStore, overrides: dict[str, object]
    ) -> None:
        with pytest.raises(
            PersistenceError,
            match="Final acceptance|Accepted final|Rejected final|Terminal acceptance plan",
        ):
            payload = self._acceptance_event(**overrides).data
            terminal = BaseEvent(
                type="orchestrator.session.completed",
                aggregate_type="session",
                aggregate_id="sess-acceptance-guard",
                data={"acceptance_finalizations": [payload]},
            )
            await self._seed_session_start(
                event_store,
                session_id="sess-acceptance-guard",
                execution_id="exec-acceptance-guard",
            )
            await event_store.append(terminal)

    async def test_final_acceptance_raw_append_paths_are_rejected(
        self, event_store: EventStore
    ) -> None:
        event = self._acceptance_event()
        benign = BaseEvent(
            type="execution.progress.updated",
            aggregate_type="execution",
            aggregate_id=event.aggregate_id,
            data={},
        )

        with pytest.raises(PersistenceError, match="one-winner guard"):
            await event_store.append_with_rowid(event)
        with pytest.raises(PersistenceError, match="cannot be appended in a batch"):
            await event_store.append_batch([benign, event])
        assert await event_store.replay("execution", event.aggregate_id) == []

    async def test_conditional_pause_preserves_existing_terminal_winner(
        self, event_store: EventStore
    ) -> None:
        """PAUSED cannot be appended behind an explicit terminal winner."""
        session_id = "sess-terminal-before-pause"
        cancelled = BaseEvent(
            type="orchestrator.session.cancelled",
            aggregate_type="session",
            aggregate_id=session_id,
            data={"reason": "terminal wins"},
        )
        paused = BaseEvent(
            type="orchestrator.session.paused",
            aggregate_type="session",
            aggregate_id=session_id,
            data={"reason": "too late"},
        )

        assert await event_store.append(cancelled) is True
        assert await event_store.append_session_pause_if_active(paused) is False

        events = await event_store.replay("session", session_id)
        assert [event.type for event in events] == ["orchestrator.session.cancelled"]

    async def test_append_preserves_event_data(
        self, event_store: EventStore, sample_event: BaseEvent
    ) -> None:
        """append() preserves all event fields."""
        await event_store.append(sample_event)
        events = await event_store.replay("ontology", "ont-123")

        stored = events[0]
        assert stored.id == sample_event.id
        assert stored.type == sample_event.type
        assert stored.aggregate_type == sample_event.aggregate_type
        assert stored.aggregate_id == sample_event.aggregate_id
        assert stored.data == sample_event.data

    async def test_append_excludes_raw_subscribed_payloads(self, event_store: EventStore) -> None:
        """append() stores normalized payloads without raw subscribed event data."""
        event = BaseEvent(
            type="orchestrator.progress.updated",
            aggregate_type="session",
            aggregate_id="sess-123",
            data={
                "progress": {
                    "messages_processed": 2,
                    "runtime": {
                        "backend": "opencode",
                        "native_session_id": "native-123",
                        "metadata": {
                            "resume_token": "resume-123",
                            "subscribed_events": [{"type": "item.completed"}],
                        },
                    },
                    "raw_event": {"type": "thread.delta"},
                }
            },
        )

        await event_store.append(event)

        replayed = await event_store.replay("session", "sess-123")
        assert replayed[0].data == {
            "progress": {
                "messages_processed": 2,
                "runtime": {
                    "backend": "opencode",
                    "native_session_id": "native-123",
                    "metadata": {
                        "resume_token": "resume-123",
                    },
                },
            }
        }

    async def test_append_excludes_raw_subscribed_payloads_nested_in_tuples(
        self, event_store: EventStore
    ) -> None:
        """append() should sanitize raw stream payloads even inside tuple-backed data."""
        event = BaseEvent(
            type="orchestrator.progress.updated",
            aggregate_type="session",
            aggregate_id="sess-123",
            data={
                "progress_batches": (
                    {
                        "messages_processed": 2,
                        "raw_event": {"type": "assistant.message.delta"},
                    },
                    {
                        "runtime": {
                            "backend": "opencode",
                            "native_session_id": "native-123",
                            "metadata": {
                                "resume_token": "resume-123",
                                "subscribed_events": [{"type": "item.completed"}],
                            },
                        },
                    },
                ),
            },
        )

        await event_store.append(event)

        replayed = await event_store.replay("session", "sess-123")
        assert replayed[0].data == {
            "progress_batches": [
                {
                    "messages_processed": 2,
                },
                {
                    "runtime": {
                        "backend": "opencode",
                        "native_session_id": "native-123",
                        "metadata": {
                            "resume_token": "resume-123",
                        },
                    },
                },
            ]
        }

    async def test_replay_history_contains_only_normalized_base_events(
        self, event_store: EventStore
    ) -> None:
        """Replayed history should contain only normalized BaseEvent records."""
        events = [
            BaseEvent(
                type="orchestrator.progress.updated",
                aggregate_type="session",
                aggregate_id="sess-history-123",
                data={
                    "progress": {
                        "step": "session.started",
                        "raw_event": {"type": "session.started"},
                        "runtime": {
                            "backend": "opencode",
                            "metadata": {
                                "resume_token": "resume-123",
                                "subscribed_events": [{"type": "session.started"}],
                            },
                        },
                    }
                },
            ),
            BaseEvent(
                type="orchestrator.tool.called",
                aggregate_type="session",
                aggregate_id="sess-history-123",
                data={
                    "tool_name": "Edit",
                    "tool_input": {"file_path": "src/ouroboros/orchestrator/runner.py"},
                    "raw_subscribed_event": {"type": "tool.started"},
                },
            ),
        ]

        await event_store.append_batch(events)

        replayed = await event_store.replay("session", "sess-history-123")

        assert len(replayed) == 2
        assert all(isinstance(event, BaseEvent) for event in replayed)
        assert replayed[0].data == {
            "progress": {
                "step": "session.started",
                "runtime": {
                    "backend": "opencode",
                    "metadata": {"resume_token": "resume-123"},
                },
            }
        }
        assert replayed[1].data == {
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/ouroboros/orchestrator/runner.py"},
        }

    async def test_append_rejects_non_base_event(self, event_store: EventStore) -> None:
        """append() rejects raw dict payloads in place of normalized BaseEvent records."""
        with pytest.raises(PersistenceError, match="BaseEvent"):
            await event_store.append({"type": "raw.event"})  # type: ignore[arg-type]

    async def test_append_rejects_raw_subscribed_stream_payload(
        self, event_store: EventStore
    ) -> None:
        """append() explicitly rejects raw subscribed runtime payloads."""
        with pytest.raises(PersistenceError, match="raw subscribed event stream payloads"):
            await event_store.append(  # type: ignore[arg-type]
                {
                    "type": "assistant.message.delta",
                    "session_id": "native-123",
                    "delta": {"text": "Applying patch"},
                    "payload": {"raw_chunk": "delta-1"},
                }
            )

    async def test_append_batch_rejects_non_base_event(self, event_store: EventStore) -> None:
        """append_batch() rejects raw dict payloads in place of normalized events."""
        with pytest.raises(PersistenceError, match="BaseEvent"):
            await event_store.append_batch(  # type: ignore[arg-type]
                [
                    BaseEvent(
                        type="test.event.created",
                        aggregate_type="test",
                        aggregate_id="test-123",
                    ),
                    {"type": "raw.event"},
                ]
            )

    async def test_append_batch_rejects_raw_subscribed_stream_payload(
        self, event_store: EventStore
    ) -> None:
        """append_batch() explicitly rejects raw subscribed runtime payloads."""
        with pytest.raises(PersistenceError, match="raw subscribed event stream payloads"):
            await event_store.append_batch(  # type: ignore[arg-type]
                [
                    BaseEvent(
                        type="test.event.created",
                        aggregate_type="test",
                        aggregate_id="test-123",
                    ),
                    {
                        "type": "tool.started",
                        "tool_name": "Edit",
                        "session_id": "native-123",
                        "input": {"file_path": "src/ouroboros/persistence/event_store.py"},
                    },
                ]
            )


class TestEventStoreReplay:
    """Test EventStore.replay() method."""

    async def test_replay_returns_empty_for_nonexistent_aggregate(
        self, event_store: EventStore
    ) -> None:
        """replay() returns empty list for nonexistent aggregate."""
        events = await event_store.replay("nonexistent", "id-999")
        assert events == []

    async def test_replay_returns_events_ordered_by_timestamp(
        self, event_store: EventStore
    ) -> None:
        """replay() returns events in timestamp order."""
        import asyncio

        events_to_store = []
        for i in range(3):
            event = BaseEvent(
                type=f"test.event.created_{i}",
                aggregate_type="test",
                aggregate_id="test-123",
                data={"order": i},
            )
            events_to_store.append(event)
            await event_store.append(event)
            await asyncio.sleep(0.01)  # Small delay for different timestamps

        replayed = await event_store.replay("test", "test-123")
        assert len(replayed) == 3
        # Verify order by checking data
        for i, event in enumerate(replayed):
            assert event.data["order"] == i

    async def test_replay_orders_by_timestamp_then_id_for_ties(
        self, event_store: EventStore
    ) -> None:
        """replay() is deterministic when multiple events share a timestamp."""
        from datetime import UTC, datetime

        shared_ts = datetime(2026, 2, 19, 0, 0, 0, tzinfo=UTC)
        later_id = BaseEvent(
            id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            timestamp=shared_ts,
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="test-order-tie",
            data={"order": "later-id"},
        )
        earlier_id = BaseEvent(
            id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            timestamp=shared_ts,
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="test-order-tie",
            data={"order": "earlier-id"},
        )

        # Insert in reverse lexical id order; replay should sort by (timestamp, id)
        await event_store.append(later_id)
        await event_store.append(earlier_id)

        replayed = await event_store.replay("test", "test-order-tie")
        assert [e.id for e in replayed] == [
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        ]

    async def test_replay_filters_by_aggregate_type(self, event_store: EventStore) -> None:
        """replay() only returns events for the specified aggregate type."""
        event1 = BaseEvent(
            type="ontology.concept.added",
            aggregate_type="ontology",
            aggregate_id="shared-id",
            data={"type": "ontology"},
        )
        event2 = BaseEvent(
            type="execution.ac.completed",
            aggregate_type="execution",
            aggregate_id="shared-id",
            data={"type": "execution"},
        )

        await event_store.append(event1)
        await event_store.append(event2)

        ontology_events = await event_store.replay("ontology", "shared-id")
        execution_events = await event_store.replay("execution", "shared-id")

        assert len(ontology_events) == 1
        assert ontology_events[0].data["type"] == "ontology"
        assert len(execution_events) == 1
        assert execution_events[0].data["type"] == "execution"

    async def test_replay_filters_by_aggregate_id(self, event_store: EventStore) -> None:
        """replay() only returns events for the specified aggregate ID."""
        event1 = BaseEvent(
            type="ontology.concept.added",
            aggregate_type="ontology",
            aggregate_id="ont-1",
            data={"id": "1"},
        )
        event2 = BaseEvent(
            type="ontology.concept.added",
            aggregate_type="ontology",
            aggregate_id="ont-2",
            data={"id": "2"},
        )

        await event_store.append(event1)
        await event_store.append(event2)

        events_1 = await event_store.replay("ontology", "ont-1")
        events_2 = await event_store.replay("ontology", "ont-2")

        assert len(events_1) == 1
        assert events_1[0].data["id"] == "1"
        assert len(events_2) == 1
        assert events_2[0].data["id"] == "2"


class TestEventStoreGetEventsAfter:
    """Test EventStore.get_events_after() incremental fetching."""

    async def test_get_events_after_returns_all_when_last_row_id_is_zero(
        self, event_store: EventStore
    ) -> None:
        """get_events_after() with last_row_id=0 returns all matching events."""
        for i in range(3):
            await event_store.append(
                BaseEvent(
                    type="test.event.created",
                    aggregate_type="execution",
                    aggregate_id="exec-1",
                    data={"order": i},
                )
            )

        events, last_row_id = await event_store.get_events_after("execution", "exec-1", 0)
        assert len(events) == 3
        assert last_row_id > 0

    async def test_get_events_after_returns_only_new_events(self, event_store: EventStore) -> None:
        """get_events_after() only returns events inserted after last_row_id."""
        # Insert first batch
        for i in range(3):
            await event_store.append(
                BaseEvent(
                    type="test.event.created",
                    aggregate_type="execution",
                    aggregate_id="exec-1",
                    data={"batch": 1, "order": i},
                )
            )

        # Get initial cursor
        _, last_row_id = await event_store.get_events_after("execution", "exec-1", 0)

        # Insert second batch
        for i in range(2):
            await event_store.append(
                BaseEvent(
                    type="test.event.created",
                    aggregate_type="execution",
                    aggregate_id="exec-1",
                    data={"batch": 2, "order": i},
                )
            )

        # Should only get the 2 new events
        new_events, new_row_id = await event_store.get_events_after(
            "execution", "exec-1", last_row_id
        )
        assert len(new_events) == 2
        assert all(e.data["batch"] == 2 for e in new_events)
        assert new_row_id > last_row_id

    async def test_get_events_after_limit_does_not_skip_out_of_order_timestamps(
        self, event_store: EventStore
    ) -> None:
        """A limited page must page by rowid, not timestamp, to stay skip-safe.

        Regression: if a row is appended with an earlier timestamp than an
        existing row, a limited page ordered by timestamp would return the
        higher rowid first and advance the cursor past the lower-rowid row,
        skipping it forever. Paging by rowid keeps every row reachable.
        """
        from datetime import UTC, datetime

        # rowid 1 carries the LATER timestamp; rowid 2 the earlier one.
        await event_store.append(
            BaseEvent(
                type="test.event.created",
                aggregate_type="execution",
                aggregate_id="exec-ooo",
                timestamp=datetime(2026, 4, 22, 0, 0, 10, tzinfo=UTC),
                data={"label": "rowid1-late-ts"},
            )
        )
        await event_store.append(
            BaseEvent(
                type="test.event.created",
                aggregate_type="execution",
                aggregate_id="exec-ooo",
                timestamp=datetime(2026, 4, 22, 0, 0, 0, tzinfo=UTC),
                data={"label": "rowid2-early-ts"},
            )
        )

        seen: list[str] = []
        cursor = 0
        for _ in range(4):  # bounded; must converge in 2 real pages + 1 empty
            page, next_cursor = await event_store.get_events_after(
                "execution", "exec-ooo", cursor, limit=1
            )
            if not page:
                break
            assert len(page) == 1
            assert next_cursor > cursor  # cursor strictly advances, no stall
            cursor = next_cursor
            seen.append(page[0].data["label"])

        # Both rows delivered exactly once, lowest rowid first, none skipped.
        assert seen == ["rowid1-late-ts", "rowid2-early-ts"]

    async def test_get_events_after_returns_empty_when_no_new_events(
        self, event_store: EventStore
    ) -> None:
        """get_events_after() returns empty list when no new events exist."""
        await event_store.append(
            BaseEvent(
                type="test.event.created",
                aggregate_type="execution",
                aggregate_id="exec-1",
                data={},
            )
        )

        _, last_row_id = await event_store.get_events_after("execution", "exec-1", 0)

        # No new events
        events, same_row_id = await event_store.get_events_after("execution", "exec-1", last_row_id)
        assert events == []
        assert same_row_id == last_row_id

    async def test_get_events_after_filters_by_aggregate(self, event_store: EventStore) -> None:
        """get_events_after() only returns events for the specified aggregate."""
        await event_store.append(
            BaseEvent(
                type="test.event.created",
                aggregate_type="execution",
                aggregate_id="exec-1",
                data={"target": True},
            )
        )
        await event_store.append(
            BaseEvent(
                type="test.event.created",
                aggregate_type="execution",
                aggregate_id="exec-2",
                data={"target": False},
            )
        )

        events, _ = await event_store.get_events_after("execution", "exec-1", 0)
        assert len(events) == 1
        assert events[0].data["target"] is True

    async def test_get_events_after_returns_empty_for_nonexistent_aggregate(
        self, event_store: EventStore
    ) -> None:
        """get_events_after() returns empty list for nonexistent aggregate."""
        events, last_row_id = await event_store.get_events_after("execution", "no-such-id", 0)
        assert events == []
        assert last_row_id == 0

    async def test_get_events_after_raises_when_not_initialized(self) -> None:
        """get_events_after() raises PersistenceError when store not initialized."""
        from ouroboros.core.errors import PersistenceError

        store = EventStore("sqlite+aiosqlite:///test.db")
        with pytest.raises(PersistenceError, match="not initialized"):
            await store.get_events_after("test", "test-123", 0)

    async def test_get_current_rowid_returns_global_tail_cursor(
        self,
        event_store: EventStore,
    ) -> None:
        """get_current_rowid() returns a cursor that excludes existing rows."""
        assert await event_store.get_current_rowid() == 0
        await event_store.append(
            BaseEvent(
                type="test.event.created",
                aggregate_type="execution",
                aggregate_id="exec-tail",
                data={},
            )
        )

        cursor = await event_store.get_current_rowid()
        assert cursor > 0
        events, same_cursor = await event_store.get_events_after(
            "execution",
            "exec-tail",
            cursor,
        )
        assert events == []
        assert same_cursor == cursor

    async def test_get_current_rowid_raises_when_not_initialized(self) -> None:
        """get_current_rowid() raises PersistenceError when store not initialized."""
        from ouroboros.core.errors import PersistenceError

        store = EventStore("sqlite+aiosqlite:///test.db")
        with pytest.raises(PersistenceError, match="not initialized"):
            await store.get_current_rowid()


class TestEventStoreErrorHandling:
    """Test error handling in EventStore."""

    async def test_append_raises_when_not_initialized(self) -> None:
        """append() raises PersistenceError when store not initialized."""
        from ouroboros.core.errors import PersistenceError

        store = EventStore("sqlite+aiosqlite:///test.db")
        event = BaseEvent(
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="test-123",
        )

        with pytest.raises(PersistenceError, match="not initialized"):
            await store.append(event)

    async def test_replay_raises_when_not_initialized(self) -> None:
        """replay() raises PersistenceError when store not initialized."""
        from ouroboros.core.errors import PersistenceError

        store = EventStore("sqlite+aiosqlite:///test.db")

        with pytest.raises(PersistenceError, match="not initialized"):
            await store.replay("test", "test-123")


class TestSessionActivitySnapshots:
    """Test aggregated session snapshot queries for orphan detection."""

    async def test_returns_latest_status_and_activity(self, event_store: EventStore) -> None:
        await event_store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="sess-running",
                data={
                    "execution_id": "exec-running",
                    "seed_id": "seed-running",
                    "start_time": "2026-04-01T00:00:00+00:00",
                },
            )
        )
        await event_store.append(
            BaseEvent(
                type="orchestrator.progress.updated",
                aggregate_type="session",
                aggregate_id="sess-running",
                data={"progress": {"step": 2}},
            )
        )
        await event_store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="sess-completed",
                data={
                    "execution_id": "exec-completed",
                    "seed_id": "seed-completed",
                    "start_time": "2026-04-01T01:00:00+00:00",
                },
            )
        )
        await event_store.append(
            BaseEvent(
                type="orchestrator.progress.updated",
                aggregate_type="session",
                aggregate_id="sess-completed",
                data={"progress": {"runtime_status": "completed"}},
            )
        )

        snapshots = await event_store.get_session_activity_snapshots()
        by_id = {snapshot.session_id: snapshot for snapshot in snapshots}

        assert set(by_id) == {"sess-running", "sess-completed"}
        assert by_id["sess-running"].execution_id == "exec-running"
        assert by_id["sess-running"].seed_id == "seed-running"
        assert by_id["sess-running"].status_event_type is None
        assert by_id["sess-running"].runtime_status is None
        assert by_id["sess-running"].last_activity is not None
        assert by_id["sess-completed"].status_event_type == "orchestrator.progress.updated"
        assert by_id["sess-completed"].runtime_status == "completed"

    async def test_includes_terminal_session_without_started_event(
        self, event_store: EventStore
    ) -> None:
        await event_store.append(
            BaseEvent(
                type="orchestrator.session.completed",
                aggregate_type="session",
                aggregate_id="sess-imported-complete",
                data={"summary": "imported terminal event"},
            )
        )

        snapshots = await event_store.get_session_activity_snapshots()
        by_id = {snapshot.session_id: snapshot for snapshot in snapshots}

        assert set(by_id) == {"sess-imported-complete"}
        snapshot = by_id["sess-imported-complete"]
        assert snapshot.execution_id is None
        assert snapshot.seed_id is None
        assert snapshot.start_time is not None
        assert snapshot.last_activity is not None
        assert snapshot.status_event_type == "orchestrator.session.completed"

    async def test_terminal_status_absorbs_later_running_progress(
        self, event_store: EventStore
    ) -> None:
        session_id = "sess-terminal-with-late-progress"
        await event_store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id=session_id,
                data={"execution_id": "exec-terminal-with-late-progress"},
            )
        )
        await event_store.append(
            BaseEvent(
                type="orchestrator.session.cancelled",
                aggregate_type="session",
                aggregate_id=session_id,
                data={"reason": "user requested cancellation"},
            )
        )
        await event_store.append(
            BaseEvent(
                type="orchestrator.progress.updated",
                aggregate_type="session",
                aggregate_id=session_id,
                data={"progress": {"runtime_status": "running", "phase": "late"}},
            )
        )

        snapshots = await event_store.get_session_activity_snapshots()
        snapshot = next(item for item in snapshots if item.session_id == session_id)

        assert snapshot.status_event_type == "orchestrator.session.cancelled"
        assert snapshot.runtime_status is None

    async def test_includes_progress_only_session_without_started_event(
        self, event_store: EventStore
    ) -> None:
        await event_store.append(
            BaseEvent(
                type="orchestrator.progress.updated",
                aggregate_type="session",
                aggregate_id="sess-imported-progress",
                data={"progress": {"step": 1, "runtime_status": "running"}},
            )
        )

        snapshots = await event_store.get_session_activity_snapshots()
        by_id = {snapshot.session_id: snapshot for snapshot in snapshots}

        assert set(by_id) == {"sess-imported-progress"}
        snapshot = by_id["sess-imported-progress"]
        assert snapshot.execution_id is None
        assert snapshot.seed_id is None
        assert snapshot.start_time is not None
        assert snapshot.last_activity is not None
        assert snapshot.status_event_type == "orchestrator.progress.updated"
        assert snapshot.runtime_status == "running"


class TestSessionRelatedEvents:
    """Test cross-aggregate session activity queries."""

    async def test_matches_normalized_evolve_execution_scope_ids(
        self,
        event_store: EventStore,
    ) -> None:
        session_id = "sess-evolve"
        execution_id = "evolve:lin-watch:generation:1"
        execution_scope = normalize_execution_scope_id(execution_id)
        events = [
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id=session_id,
                data={
                    "execution_id": execution_id,
                    "seed_id": "seed-evolve",
                    "start_time": "2026-04-01T00:00:00+00:00",
                },
            ),
            BaseEvent(
                type="workflow.progress.updated",
                aggregate_type="execution",
                aggregate_id=execution_id,
                data={"session_id": session_id, "completed_count": 0, "total_count": 1},
            ),
            BaseEvent(
                type="execution.ac.heartbeat",
                aggregate_type="execution",
                aggregate_id=f"{execution_scope}_ac_0",
                data={"session_id": session_id, "ac_index": 0, "message_count": 1},
            ),
            BaseEvent(
                type="execution.coordinator.completed",
                aggregate_type="execution",
                aggregate_id=f"{execution_scope}_level_1_coordinator_reconciliation",
                data={"session_id": session_id, "execution_id": execution_id},
            ),
            BaseEvent(
                type="execution.subagent.started",
                aggregate_type="execution",
                aggregate_id=f"{execution_scope}_child_0",
                data={"parent_execution_id": execution_id, "child_ac": "child task", "depth": 1},
            ),
            BaseEvent(
                type="execution.ac.heartbeat",
                aggregate_type="execution",
                aggregate_id="other_evolve_ac_0",
                data={"session_id": "other-session", "ac_index": 0, "message_count": 1},
            ),
        ]
        for event in events:
            await event_store.append(event)

        result = await event_store.query_session_related_events(
            session_id,
            execution_id=execution_id,
            limit=None,
        )
        aggregate_ids = {event.aggregate_id for event in result}

        assert session_id in aggregate_ids
        assert execution_id in aggregate_ids
        assert f"{execution_scope}_ac_0" in aggregate_ids
        assert f"{execution_scope}_level_1_coordinator_reconciliation" in aggregate_ids
        assert f"{execution_scope}_child_0" in aggregate_ids
        assert "other_evolve_ac_0" not in aggregate_ids

    async def test_session_related_events_are_bounded_by_session_start(
        self,
        event_store: EventStore,
    ) -> None:
        """Historical payload matches should not force full-history session scans."""
        session_id = "sess-bounded-related"
        execution_id = "exec-bounded-related"
        session_started_at = datetime.now(UTC)
        await event_store.append(
            BaseEvent(
                type="execution.ac.heartbeat",
                timestamp=session_started_at - timedelta(days=1),
                aggregate_type="execution",
                aggregate_id="old-runtime-scope",
                data={"session_id": session_id, "message_count": 1},
            )
        )
        await event_store.append(
            BaseEvent(
                type="orchestrator.session.started",
                timestamp=session_started_at,
                aggregate_type="session",
                aggregate_id=session_id,
                data={"execution_id": execution_id, "seed_id": "seed-bounded-related"},
            )
        )
        await event_store.append(
            BaseEvent(
                type="execution.ac.heartbeat",
                timestamp=session_started_at + timedelta(seconds=1),
                aggregate_type="execution",
                aggregate_id="new-runtime-scope",
                data={"session_id": session_id, "message_count": 1},
            )
        )

        result = await event_store.query_session_related_events(
            session_id,
            execution_id=execution_id,
            limit=None,
        )
        aggregate_ids = {event.aggregate_id for event in result}

        assert "new-runtime-scope" in aggregate_ids
        assert session_id in aggregate_ids
        assert "old-runtime-scope" not in aggregate_ids

    async def test_execution_related_events_include_child_scopes(
        self,
        event_store: EventStore,
    ) -> None:
        """Execution-only queries include child/runtime aggregates."""
        execution_id = "exec-related-only"
        events = [
            BaseEvent(
                type="workflow.progress.updated",
                aggregate_type="execution",
                aggregate_id=execution_id,
                data={"execution_id": execution_id},
            ),
            BaseEvent(
                type="execution.subagent.started",
                aggregate_type="execution",
                aggregate_id="exec-related-only-child",
                data={"parent_execution_id": execution_id},
            ),
            BaseEvent(
                type="execution.ac.heartbeat",
                aggregate_type="execution",
                aggregate_id="other-execution-child",
                data={"parent_execution_id": "other-execution"},
            ),
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="sess-related-only",
                data={"execution_id": execution_id, "seed_id": "seed-related-only"},
            ),
        ]
        for event in events:
            await event_store.append(event)

        result = await event_store.query_execution_related_events(
            execution_id,
            limit=None,
        )
        aggregate_ids = {event.aggregate_id for event in result}

        assert execution_id in aggregate_ids
        assert "exec-related-only-child" in aggregate_ids
        assert "other-execution-child" not in aggregate_ids
        assert "sess-related-only" not in aggregate_ids

    async def test_execution_payload_filter_is_applied_before_limit(
        self,
        event_store: EventStore,
    ) -> None:
        """Unrelated same-type rows cannot crowd a bounded contract stream."""

        execution_id = "exec-payload-filter"
        session_id = "sess-payload-filter"
        timestamp = datetime.now(UTC)
        route_aware = BaseEvent(
            type="execution.ac.attempt_judged",
            timestamp=timestamp,
            aggregate_type="execution",
            aggregate_id=execution_id,
            data={
                "execution_id": execution_id,
                "session_id": session_id,
                "route_contract_version": 1,
            },
        )
        legacy = BaseEvent(
            type="execution.ac.attempt_judged",
            timestamp=timestamp + timedelta(seconds=1),
            aggregate_type="execution",
            aggregate_id=execution_id,
            data={
                "execution_id": execution_id,
                "session_id": session_id,
            },
        )
        await event_store.append_batch([route_aware, legacy])

        unfiltered = await event_store.query_execution_related_events(
            execution_id,
            event_type="execution.ac.attempt_judged",
            limit=1,
        )
        filtered = await event_store.query_execution_related_events(
            execution_id,
            event_type="execution.ac.attempt_judged",
            limit=1,
            payload_equals={
                "route_contract_version": 1,
                "session_id": session_id,
            },
        )

        assert [event.id for event in unfiltered] == [legacy.id]
        assert [event.id for event in filtered] == [route_aware.id]

    async def test_latest_execution_job_status_follows_linked_job_stream(
        self,
        event_store: EventStore,
    ) -> None:
        execution_id = "exec-job-status"
        await event_store.append(
            BaseEvent(
                type="mcp.job.created",
                aggregate_type="job",
                aggregate_id="job-status-1",
                data={
                    "status": "queued",
                    "links": {"execution_id": execution_id},
                },
            )
        )
        await event_store.append(
            BaseEvent(
                type="mcp.job.interrupted",
                aggregate_type="job",
                aggregate_id="job-status-1",
                data={"status": "interrupted"},
            )
        )

        assert await event_store.get_latest_execution_job_status(execution_id) == ("interrupted")
        assert await event_store.get_latest_execution_job_status("exec-missing") is None

    async def test_session_signal_query_is_exact_and_replay_ordered(
        self,
        event_store: EventStore,
    ) -> None:
        base = datetime.now(UTC)
        matching = [
            BaseEvent(
                id=f"signal-event-{index}",
                type=f"control.session.signal.{state}",
                timestamp=base + timedelta(seconds=index),
                aggregate_type="session_signal",
                aggregate_id="sig_exact",
                data={
                    "expected_execution_id": "exec_exact",
                    "target_session_scope_id": "scope_exact",
                    "target_session_attempt_id": "attempt_exact",
                },
            )
            for index, state in enumerate(("requested", "accepted", "queued"))
        ]
        unrelated = BaseEvent(
            type="control.session.signal.queued",
            aggregate_type="session_signal",
            aggregate_id="sig_other",
            data={
                "expected_execution_id": "exec_exact",
                "target_session_scope_id": "scope_exact",
                "target_session_attempt_id": "attempt_other",
            },
        )
        for event in [*matching, unrelated]:
            await event_store.append(event)

        result = await event_store.query_session_signal_events(
            execution_id="exec_exact",
            session_scope_id="scope_exact",
            session_attempt_id="attempt_exact",
        )

        assert [event.id for event in result] == [event.id for event in matching]

    async def test_session_related_events_do_not_join_on_lossy_normalized_scope(
        self,
        event_store: EventStore,
    ) -> None:
        """Distinct execution IDs that normalize alike must stay isolated."""
        session_id = "sess-lossy-normalized"
        execution_id = "evolve:foo:bar:generation:1"
        colliding_execution_id = "evolve:foo_bar:generation:1"
        assert normalize_execution_scope_id(execution_id) == normalize_execution_scope_id(
            colliding_execution_id
        )
        colliding_scope = normalize_execution_scope_id(colliding_execution_id)

        events = [
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id=session_id,
                data={"execution_id": execution_id, "seed_id": "seed-lossy"},
            ),
            BaseEvent(
                type="execution.ac.heartbeat",
                aggregate_type="execution",
                aggregate_id=f"{colliding_scope}_ac_0",
                data={
                    "session_id": "other-session",
                    "execution_id": colliding_execution_id,
                    "ac_index": 0,
                    "message_count": 1,
                },
            ),
        ]
        for event in events:
            await event_store.append(event)

        result = await event_store.query_session_related_events(
            session_id,
            execution_id=execution_id,
            limit=None,
        )

        assert [event.aggregate_id for event in result] == [session_id]

    async def test_session_related_events_after_advances_one_cursor(
        self,
        event_store: EventStore,
    ) -> None:
        session_id = "sess-incremental"
        execution_id = "evolve:lin-incremental:generation:1"
        execution_scope = normalize_execution_scope_id(execution_id)
        await event_store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id=session_id,
                data={"execution_id": execution_id, "seed_id": "seed-incremental"},
            )
        )

        first_batch, cursor = await event_store.query_session_related_events_after(
            session_id,
            execution_id=execution_id,
        )
        assert [event.type for event in first_batch] == ["orchestrator.session.started"]

        await event_store.append(
            BaseEvent(
                type="execution.ac.heartbeat",
                aggregate_type="execution",
                aggregate_id=f"{execution_scope}_ac_0",
                data={"session_id": session_id, "ac_index": 0, "message_count": 2},
            )
        )

        second_batch, next_cursor = await event_store.query_session_related_events_after(
            session_id,
            execution_id=execution_id,
            last_row_id=cursor,
        )

        assert [event.type for event in second_batch] == ["execution.ac.heartbeat"]
        assert next_cursor > cursor

    async def test_session_related_events_after_are_bounded_by_session_start(
        self,
        event_store: EventStore,
    ) -> None:
        """Incremental session polling must ignore stale pre-start payload matches."""
        session_id = "sess-bounded-related-after"
        execution_id = "exec-bounded-related-after"
        session_started_at = datetime.now(UTC)
        await event_store.append(
            BaseEvent(
                type="execution.ac.heartbeat",
                timestamp=session_started_at - timedelta(days=1),
                aggregate_type="execution",
                aggregate_id="old-runtime-scope",
                data={"session_id": session_id, "message_count": 1},
            )
        )
        await event_store.append(
            BaseEvent(
                type="orchestrator.session.started",
                timestamp=session_started_at,
                aggregate_type="session",
                aggregate_id=session_id,
                data={"execution_id": execution_id, "seed_id": "seed-bounded-related-after"},
            )
        )
        await event_store.append(
            BaseEvent(
                type="execution.ac.heartbeat",
                timestamp=session_started_at + timedelta(seconds=1),
                aggregate_type="execution",
                aggregate_id="new-runtime-scope",
                data={"session_id": session_id, "message_count": 1},
            )
        )

        events, cursor = await event_store.query_session_related_events_after(
            session_id,
            execution_id=execution_id,
            last_row_id=0,
        )

        assert [event.aggregate_id for event in events] == [session_id, "new-runtime-scope"]
        assert cursor > 0

    async def test_session_related_events_after_includes_exact_parent_execution_children(
        self,
        event_store: EventStore,
    ) -> None:
        """Child execution scopes linked only by parent_execution_id remain visible."""
        session_id = "sess-parent-execution"
        execution_id = "evolve:lin-parent:generation:1"
        await event_store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id=session_id,
                data={"execution_id": execution_id, "seed_id": "seed-parent"},
            )
        )

        first_batch, cursor = await event_store.query_session_related_events_after(
            session_id,
            execution_id=execution_id,
        )
        assert [event.type for event in first_batch] == ["orchestrator.session.started"]

        await event_store.append(
            BaseEvent(
                type="execution.subagent.started",
                aggregate_type="execution",
                aggregate_id="evolve_lin_parent_generation_1_child_0",
                data={
                    "parent_execution_id": execution_id,
                    "child_ac": "child task",
                    "depth": 1,
                },
            )
        )
        await event_store.append(
            BaseEvent(
                type="execution.subagent.started",
                aggregate_type="execution",
                aggregate_id="evolve_lin_other_generation_1_child_0",
                data={
                    "parent_execution_id": "evolve:lin-other:generation:1",
                    "child_ac": "other child",
                    "depth": 1,
                },
            )
        )

        second_batch, next_cursor = await event_store.query_session_related_events_after(
            session_id,
            execution_id=execution_id,
            last_row_id=cursor,
        )

        assert [(event.type, event.aggregate_id) for event in second_batch] == [
            ("execution.subagent.started", "evolve_lin_parent_generation_1_child_0")
        ]
        assert next_cursor > cursor

    async def test_blank_scope_returns_no_events_not_whole_store(
        self,
        event_store: EventStore,
    ) -> None:
        """A blank session with no execution must fail closed, not scan the store.

        With an empty ``session_id`` and no ``execution_id`` there is no scope
        predicate, and ``or_()`` over zero clauses emits a WHERE-less query — this
        pins that the query returns nothing rather than the entire event store.
        """
        await event_store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="sess-unrelated",
                data={"execution_id": "exec-unrelated", "seed_id": "seed-unrelated"},
            )
        )
        await event_store.append(
            BaseEvent(
                type="execution.ac.heartbeat",
                aggregate_type="execution",
                aggregate_id="exec-unrelated",
                data={"session_id": "sess-unrelated", "message_count": 1},
            )
        )

        result = await event_store.query_session_related_events(
            "",
            execution_id=None,
            limit=None,
        )

        assert result == []

    async def test_blank_scope_after_returns_no_events_and_unchanged_cursor(
        self,
        event_store: EventStore,
    ) -> None:
        """The incremental variant must also fail closed and leave the cursor put."""
        await event_store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="sess-unrelated",
                data={"execution_id": "exec-unrelated", "seed_id": "seed-unrelated"},
            )
        )
        await event_store.append(
            BaseEvent(
                type="execution.ac.heartbeat",
                aggregate_type="execution",
                aggregate_id="exec-unrelated",
                data={"session_id": "sess-unrelated", "message_count": 1},
            )
        )

        events, cursor = await event_store.query_session_related_events_after(
            "",
            execution_id=None,
            last_row_id=0,
        )

        assert events == []
        assert cursor == 0

    async def test_blank_session_with_execution_still_returns_execution_rows(
        self,
        event_store: EventStore,
    ) -> None:
        """The TUI case that motivated the empty-session change must keep working.

        A blank session paired with a real ``execution_id`` still carries an
        execution scope, so the query returns that execution's related rows.
        """
        execution_id = "exec-tui-scope"
        await event_store.append(
            BaseEvent(
                type="execution.ac.heartbeat",
                aggregate_type="execution",
                aggregate_id=execution_id,
                data={"execution_id": execution_id, "message_count": 1},
            )
        )
        await event_store.append(
            BaseEvent(
                type="execution.ac.heartbeat",
                aggregate_type="execution",
                aggregate_id="exec-other-scope",
                data={"execution_id": "exec-other", "message_count": 1},
            )
        )

        result = await event_store.query_session_related_events(
            "",
            execution_id=execution_id,
            limit=None,
        )
        aggregate_ids = {event.aggregate_id for event in result}

        assert execution_id in aggregate_ids
        assert "exec-other-scope" not in aggregate_ids


class TestGetAllSessions:
    """Test get_all_sessions returns all session lifecycle events."""

    async def test_returns_all_session_event_types(self, event_store: EventStore) -> None:
        """get_all_sessions returns started, completed, cancelled, and failed events."""
        started = BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id="sess-1",
            data={"seed_goal": "Build API"},
        )
        completed = BaseEvent(
            type="orchestrator.session.completed",
            aggregate_type="session",
            aggregate_id="sess-1",
            data={"summary": "done"},
        )
        cancelled = BaseEvent(
            type="orchestrator.session.cancelled",
            aggregate_type="session",
            aggregate_id="sess-2",
            data={"reason": "orphaned"},
        )
        unrelated = BaseEvent(
            type="orchestrator.execution.started",
            aggregate_type="execution",
            aggregate_id="exec-1",
            data={},
        )
        for evt in [started, completed, cancelled, unrelated]:
            await event_store.append(evt)

        result = await event_store.get_all_sessions()
        types = [e.type for e in result]
        assert "orchestrator.session.started" in types
        assert "orchestrator.session.completed" in types
        assert "orchestrator.session.cancelled" in types
        assert "orchestrator.execution.started" not in types

    async def test_returns_events_in_ascending_order(self, event_store: EventStore) -> None:
        """Events are returned oldest-first so callers can replay status."""
        for suffix in ("started", "completed"):
            await event_store.append(
                BaseEvent(
                    type=f"orchestrator.session.{suffix}",
                    aggregate_type="session",
                    aggregate_id="sess-asc",
                    data={},
                )
            )

        result = await event_store.get_all_sessions()
        sess_events = [e for e in result if e.aggregate_id == "sess-asc"]
        assert len(sess_events) == 2
        assert sess_events[0].type == "orchestrator.session.started"
        assert sess_events[1].type == "orchestrator.session.completed"

    async def test_raises_when_not_initialized(self) -> None:
        """get_all_sessions raises PersistenceError when store not initialized."""
        store = EventStore("sqlite+aiosqlite:///dummy.db")
        with pytest.raises(PersistenceError):
            await store.get_all_sessions()

    async def test_falls_back_when_forced_index_missing(self, event_store: EventStore) -> None:
        """``INDEXED BY ix_events_event_type`` makes the index mandatory; if a
        store lacks it, get_all_sessions must fall back to the planner's choice
        and still return correct, ordered results instead of erroring."""
        for suffix in ("started", "completed"):
            await event_store.append(
                BaseEvent(
                    type=f"orchestrator.session.{suffix}",
                    aggregate_type="session",
                    aggregate_id="sess-fallback",
                    data={"seed_goal": "g"},
                )
            )

        # Drop the index the fast path pins so a raw INDEXED BY would error.
        async with event_store._engine.begin() as conn:  # type: ignore[union-attr]
            await conn.exec_driver_sql("DROP INDEX IF EXISTS ix_events_event_type")

        result = await event_store.get_all_sessions()
        sess = [e for e in result if e.aggregate_id == "sess-fallback"]
        assert len(sess) == 2
        assert sess[0].type == "orchestrator.session.started"
        assert sess[1].type == "orchestrator.session.completed"
        # Payload is still deserialized into a dict (column types preserved).
        assert sess[0].data.get("seed_goal") == "g"


class TestEventStoreClose:
    """Close-time behavior: WAL checkpoint, idempotency, read-only safety."""

    async def test_close_collapses_wal(self, tmp_path) -> None:
        """close() runs a TRUNCATE checkpoint so the ``-wal`` file is collapsed
        instead of growing unbounded across long-lived multi-connection use."""
        db_path = tmp_path / "wal_close.db"
        store = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await store.initialize()
        for i in range(200):
            await store.append(
                BaseEvent(
                    type="orchestrator.session.started",
                    aggregate_type="session",
                    aggregate_id=f"s{i}",
                    data={"blob": "x" * 1000},
                )
            )

        wal = db_path.with_name(db_path.name + "-wal")
        assert wal.exists() and wal.stat().st_size > 0

        await store.close()

        # TRUNCATE checkpoint shrinks the WAL to empty (or SQLite removes it on
        # the final connection close).
        assert (not wal.exists()) or wal.stat().st_size == 0

    async def test_close_is_idempotent(self, tmp_path) -> None:
        """Calling close() twice must be safe (engine already disposed)."""
        db_path = tmp_path / "wal_idem.db"
        store = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await store.initialize()
        await store.close()
        await store.close()  # must not raise

    async def test_close_read_only_skips_write_checkpoint(self, tmp_path) -> None:
        """A read-only store must not attempt the (writing) WAL checkpoint —
        doing so would raise 'attempt to write a readonly database'."""
        db_path = tmp_path / "ro.db"
        writer = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await writer.initialize()
        await writer.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="s1",
                data={},
            )
        )
        await writer.close()

        ro = EventStore(f"sqlite+aiosqlite:///{db_path}", read_only=True)
        await ro.initialize()
        await ro.close()  # must not raise

    async def test_read_only_store_preserves_literal_uri_characters_in_path(self, tmp_path) -> None:
        template_path = tmp_path / "template.db"
        writer = EventStore(f"sqlite+aiosqlite:///{template_path}")
        await writer.initialize()
        await writer.append(
            BaseEvent(
                type="execution.terminal",
                aggregate_type="execution",
                aggregate_id="exec_literal_path",
                data={"status": "complete"},
            )
        )
        await writer.close()

        literal_path = tmp_path / "literal?mode=rw&fragment#.db"
        shutil.copy2(template_path, literal_path)
        store = EventStore(f"sqlite+aiosqlite:///{literal_path}", read_only=True)
        await store.initialize(create_schema=False)
        try:
            events = await store.query_events(aggregate_id="exec_literal_path")
            assert len(events) == 1
            with pytest.raises(PersistenceError):
                await store.append(
                    BaseEvent(
                        type="execution.started",
                        aggregate_type="execution",
                        aggregate_id="exec_must_not_write",
                    )
                )
        finally:
            await store.close()

    async def test_read_only_store_overrides_writable_file_uri_mode(self, tmp_path) -> None:
        db_path = tmp_path / "explicit-uri.db"
        writer = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await writer.initialize()
        await writer.close()

        store = EventStore(
            f"sqlite+aiosqlite:///file:{db_path}?mode=rw&uri=true",
            read_only=True,
        )
        await store.initialize(create_schema=False)
        try:
            with pytest.raises(PersistenceError):
                await store.append(
                    BaseEvent(
                        type="execution.started",
                        aggregate_type="execution",
                        aggregate_id="exec_must_stay_read_only",
                    )
                )
        finally:
            await store.close()


class TestEventStoreTransactions:
    """Test transaction handling per AC7."""

    async def test_append_is_atomic(self, event_store: EventStore) -> None:
        """append() uses transactions for atomicity."""
        # This tests that a successful append is committed
        event = BaseEvent(
            type="test.transaction.committed",
            aggregate_type="test",
            aggregate_id="tx-test",
            data={"committed": True},
        )
        await event_store.append(event)

        # Close and reopen to ensure persistence
        await event_store.close()
        await event_store.initialize()

        events = await event_store.replay("test", "tx-test")
        assert len(events) == 1
        assert events[0].data["committed"] is True


class TestWorkflowIRAppendGuard:
    """Test the workflow_ir aggregate-type guard on EventStore.append()."""

    async def test_append_rejects_raw_workflow_ir_event(self, event_store: EventStore) -> None:
        """append() must refuse raw BaseEvents on the workflow_ir aggregate.

        ``WorkflowLifecycleEvent`` enforces a replay-unsafe key blocklist at
        its Pydantic boundary. A caller that constructs a raw ``BaseEvent``
        with ``aggregate_type="workflow_ir"`` must not be able to bypass
        that guard via :meth:`EventStore.append`.
        """
        bypass = BaseEvent(
            type="workflow.run.created",
            aggregate_type="workflow_ir",
            aggregate_id="wfspec_bypass",
            data={"workflow_id": "wfspec_bypass", "secret": "leak"},
        )
        with pytest.raises(PersistenceError) as exc_info:
            await event_store.append(bypass)
        assert "append_workflow_lifecycle_event" in str(exc_info.value)

        # The guarded row must not be persisted.
        from ouroboros.orchestrator.workflow_lifecycle import (
            WORKFLOW_LIFECYCLE_AGGREGATE_TYPE,
        )

        rows = await event_store.replay(WORKFLOW_LIFECYCLE_AGGREGATE_TYPE, "wfspec_bypass")
        assert rows == []

    async def test_append_workflow_lifecycle_event_still_persists(
        self, event_store: EventStore
    ) -> None:
        """The dedicated lifecycle helper must keep working end-to-end."""
        from ouroboros.orchestrator.workflow_lifecycle import (
            WorkflowLifecycleEvent,
            WorkflowLifecycleEventType,
        )

        event = WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id="wfspec_guarded",
        )
        await event_store.append_workflow_lifecycle_event(event)

        replayed = await event_store.replay_workflow_lifecycle("wfspec_guarded")
        assert replayed == [event]

    async def test_append_batch_rejects_workflow_ir_event(self, event_store: EventStore) -> None:
        """append_batch() must refuse raw workflow_ir BaseEvents.

        Without the batch-side guard, a caller could bypass the
        ``WorkflowLifecycleEvent`` redaction blocklist by wrapping a raw
        ``BaseEvent`` with ``aggregate_type="workflow_ir"`` inside a list
        and calling :meth:`EventStore.append_batch`. The batch guard must
        run BEFORE any DB insert so a single bad row refuses the whole
        transaction and the surrounding benign events are also dropped.
        """
        from ouroboros.orchestrator.workflow_lifecycle import (
            WORKFLOW_LIFECYCLE_AGGREGATE_TYPE,
        )

        benign = BaseEvent(
            type="test.event",
            aggregate_type="test",
            aggregate_id="batch-benign",
            data={"ok": True},
        )
        bypass = BaseEvent(
            type="workflow.run.created",
            aggregate_type=WORKFLOW_LIFECYCLE_AGGREGATE_TYPE,
            aggregate_id="wfspec_batch_bypass",
            data={"workflow_id": "wfspec_batch_bypass", "secret": "leak"},
        )

        with pytest.raises(PersistenceError) as exc_info:
            await event_store.append_batch([benign, bypass])
        message = str(exc_info.value)
        assert "append_workflow_lifecycle_event" in message
        assert "cannot be batched" in message

        # Neither the guarded workflow_ir row nor the surrounding benign
        # row may be persisted: the guard must run before the DB insert.
        guarded_rows = await event_store.replay(
            WORKFLOW_LIFECYCLE_AGGREGATE_TYPE, "wfspec_batch_bypass"
        )
        assert guarded_rows == []
        benign_rows = await event_store.replay("test", "batch-benign")
        assert benign_rows == []


class TestReplayWorkflowLifecycleResilience:
    """Test that replay_workflow_lifecycle() tolerates malformed rows."""

    async def test_replay_skips_malformed_rows_without_logging_payload_values(
        self, event_store: EventStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Malformed replay rows are skipped without logging poisoned payload values."""
        from ouroboros.orchestrator.workflow_lifecycle import (
            WORKFLOW_LIFECYCLE_AGGREGATE_TYPE,
            WorkflowLifecycleEvent,
            WorkflowLifecycleEventType,
        )
        from ouroboros.persistence.schema import events_table

        sentinel_secret = "sentinel-secret-replay-leak"
        workflow_id = "wfspec_resilient"
        # Insert one valid lifecycle event via the supported helper.
        valid_event = WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=workflow_id,
        )
        await event_store.append_workflow_lifecycle_event(valid_event)

        # Insert one malformed row directly, bypassing append()'s new guard.
        # This simulates a row written before the guard was in place or by an
        # out-of-band recovery tool. A non-string ``reason_code`` raises a
        # Pydantic ``ValidationError`` during rehydration; its string form can
        # include the raw input value, so the replay warning must not log it.
        malformed = BaseEvent(
            type="workflow.run.failed",
            aggregate_type=WORKFLOW_LIFECYCLE_AGGREGATE_TYPE,
            aggregate_id=workflow_id,
            data={"workflow_id": workflow_id, "reason_code": {"value": sentinel_secret}},
        )
        assert event_store._engine is not None  # type: ignore[attr-defined]
        async with event_store._engine.begin() as conn:  # type: ignore[attr-defined]
            await conn.execute(events_table.insert().values(**malformed.to_db_dict()))

        # Replay must return only the valid event without raising.
        caplog.set_level(logging.WARNING, logger="ouroboros.persistence.event_store")
        replayed = await event_store.replay_workflow_lifecycle(workflow_id)
        assert replayed == [valid_event]

        skip_records = [
            record
            for record in caplog.records
            if record.getMessage() == "event_store.replay_workflow_lifecycle.skip_malformed"
        ]
        assert len(skip_records) == 1
        record = skip_records[0]
        assert record.event_type == "workflow.run.failed"
        assert record.aggregate_id == workflow_id
        assert record.error == "ValidationError"
        assert not hasattr(record, "error_summary")

        captured_log_text = "\n".join(
            [record.getMessage(), *(str(value) for value in vars(record).values())]
        )
        assert sentinel_secret not in captured_log_text


class TestEventStoreCloseWalCheckpoint:
    """Test EventStore.close() WAL checkpoint behavior.

    A writable store must collapse the WAL with an explicit TRUNCATE checkpoint
    on close so the -wal file does not grow unbounded across many concurrent
    sessions; a read-only store must skip that write entirely (it would trip
    SQLite's read-only guard).
    """

    @staticmethod
    def _attach_sql_capture(store: EventStore) -> list[str]:
        """Record raw SQL executed by ``store`` until it is closed."""
        from sqlalchemy import event as sa_event

        assert store._engine is not None  # type: ignore[attr-defined]
        executed: list[str] = []

        def _capture(_conn, _cursor, statement, _params, _context, _many) -> None:
            executed.append(statement)

        sa_event.listen(
            store._engine.sync_engine,  # type: ignore[attr-defined]
            "before_cursor_execute",
            _capture,
        )
        return executed

    async def test_close_truncates_wal_for_writable_store(self, tmp_path) -> None:
        """Writable close() issues PRAGMA wal_checkpoint(TRUNCATE)."""
        db_path = tmp_path / "writable.db"
        store = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await store.initialize()

        executed = self._attach_sql_capture(store)
        await store.close()

        assert any("wal_checkpoint(truncate)" in sql.lower() for sql in executed)
        assert store._engine is None  # type: ignore[attr-defined]

    async def test_close_skips_wal_checkpoint_for_readonly_store(self, tmp_path) -> None:
        """Read-only close() must not attempt the WAL checkpoint write."""
        db_path = tmp_path / "readonly.db"
        # Create the on-disk schema with a writable store first so the read-only
        # store has an existing database file to open.
        writable = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await writable.initialize()
        await writable.close()

        store = EventStore(f"sqlite+aiosqlite:///{db_path}", read_only=True)
        await store.initialize()

        executed = self._attach_sql_capture(store)
        await store.close()

        assert not any("wal_checkpoint" in sql.lower() for sql in executed)
        assert store._engine is None  # type: ignore[attr-defined]


def _make_event(*, aggregate_id: str) -> BaseEvent:
    return BaseEvent(
        type="test.event.created",
        aggregate_type="test",
        aggregate_id=aggregate_id,
        data={},
    )


class TestCancellationSettlement:
    """#1794: a cancelled append must settle before cancellation surfaces.

    Shield-only semantics let the write outlive the caller — a probe that
    blocked ``aiosqlite.Connection.commit`` showed ``close()`` returning and
    the events committing afterward, mutating durable history after shutdown.
    The contract pinned here: cancellation is re-raised only once the
    transaction task has completed, so no write can escape the store
    lifecycle.
    """

    @pytest.mark.asyncio
    async def test_cancelled_append_settles_before_cancellation_surfaces(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import aiosqlite

        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()

        gate = asyncio.Event()
        entered = asyncio.Event()
        orig_commit = aiosqlite.Connection.commit

        async def gated_commit(self):  # noqa: ANN001, ANN202
            entered.set()
            await gate.wait()
            return await orig_commit(self)

        monkeypatch.setattr(aiosqlite.Connection, "commit", gated_commit)

        event = _make_event(aggregate_id="settle-append")
        task = asyncio.create_task(store.append(event))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done(), (
            "cancellation surfaced while the commit was still in flight — "
            "the write escaped the caller's lifetime"
        )

        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

        events = await store.replay("test", "settle-append")
        assert len(events) == 1
        await store.close()

    @pytest.mark.asyncio
    async def test_close_waits_for_settling_append(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """close() must drain in-flight writes so history is durable at close.

        A gated-commit probe showed close() returning while a cancelled
        append's transaction was still settling — the event then committed
        after close and appeared on reopen, mutating durable history after
        shutdown.
        """
        import aiosqlite

        db_path = tmp_path / "settle-close.db"
        store = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await store.initialize()

        gate = asyncio.Event()
        entered = asyncio.Event()
        orig_commit = aiosqlite.Connection.commit

        async def gated_commit(self):  # noqa: ANN001, ANN202
            entered.set()
            await gate.wait()
            return await orig_commit(self)

        monkeypatch.setattr(aiosqlite.Connection, "commit", gated_commit)

        append_task = asyncio.create_task(store.append(_make_event(aggregate_id="race-close")))
        await asyncio.wait_for(entered.wait(), timeout=5)
        append_task.cancel()

        close_task = asyncio.create_task(store.close())
        # Outlast checkpoint_wal's 2s busy timeout: without an explicit drain,
        # close() gives up on the checkpoint and disposes while the write is
        # still settling — the drain contract must hold beyond that window.
        await asyncio.sleep(2.5)
        assert not close_task.done(), "close() returned while a settling write was still in flight"

        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(append_task, timeout=5)
        await asyncio.wait_for(close_task, timeout=5)
        monkeypatch.setattr(aiosqlite.Connection, "commit", orig_commit)

        reopened = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await reopened.initialize()
        events = await reopened.replay("test", "race-close")
        assert len(events) == 1
        await reopened.close()

    @pytest.mark.asyncio
    async def test_close_drains_write_without_waiting_for_owner_task_lifetime(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A writer may await close after its write-specific work has settled."""
        from ouroboros.persistence import event_store as event_store_module

        db_path = tmp_path / "settle-close-owner-cycle.db"
        store = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await store.initialize()

        entered = asyncio.Event()
        release = asyncio.Event()
        write_settled = asyncio.Event()
        original_insert = event_store_module._insert_event
        close_task: asyncio.Task[None] | None = None

        async def gated_insert(connection, event, projection_ready):  # noqa: ANN001, ANN202
            entered.set()
            await release.wait()
            return await original_insert(connection, event, projection_ready)

        async def write_then_join_close() -> None:
            await store.append(_make_event(aggregate_id="close-owner-cycle"))
            write_settled.set()
            assert close_task is not None
            await close_task

        monkeypatch.setattr(event_store_module, "_insert_event", gated_insert)
        writer_task = asyncio.create_task(write_then_join_close())
        await asyncio.wait_for(entered.wait(), timeout=5)
        close_task = asyncio.create_task(store.close())
        await asyncio.sleep(0)

        release.set()
        await asyncio.wait_for(write_settled.wait(), timeout=5)
        await asyncio.wait_for(close_task, timeout=5)
        await asyncio.wait_for(writer_task, timeout=5)

        monkeypatch.setattr(event_store_module, "_insert_event", original_insert)
        reopened = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await reopened.initialize()
        assert len(await reopened.replay("test", "close-owner-cycle")) == 1
        await reopened.close()

    @pytest.mark.asyncio
    async def test_close_waits_for_settling_batch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import aiosqlite

        db_path = tmp_path / "settle-close-batch.db"
        store = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await store.initialize()

        gate = asyncio.Event()
        entered = asyncio.Event()
        orig_commit = aiosqlite.Connection.commit

        async def gated_commit(self):  # noqa: ANN001, ANN202
            entered.set()
            await gate.wait()
            return await orig_commit(self)

        monkeypatch.setattr(aiosqlite.Connection, "commit", gated_commit)

        batch_task = asyncio.create_task(
            store.append_batch([_make_event(aggregate_id="race-close-batch")])
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        batch_task.cancel()

        close_task = asyncio.create_task(store.close())
        # Outlast checkpoint_wal's 2s busy timeout: without an explicit drain,
        # close() gives up on the checkpoint and disposes while the write is
        # still settling — the drain contract must hold beyond that window.
        await asyncio.sleep(2.5)
        assert not close_task.done(), "close() returned while a settling batch was still in flight"

        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(batch_task, timeout=5)
        await asyncio.wait_for(close_task, timeout=5)
        monkeypatch.setattr(aiosqlite.Connection, "commit", orig_commit)

        reopened = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await reopened.initialize()
        events = await reopened.replay("test", "race-close-batch")
        assert len(events) == 1
        await reopened.close()

    @pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
    @pytest.mark.asyncio
    async def test_close_fences_complete_retry_lifecycle(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        batch: bool,
    ) -> None:
        """A retrying write cannot escape close through its backoff gap."""
        from sqlalchemy.exc import OperationalError

        db_path = tmp_path / f"retry-close-{batch}.db"
        store = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await store.initialize()
        assert store._engine is not None

        engine_type = type(store._engine)
        original_begin = engine_type.begin
        attempts = 0

        class _LockedTransaction:
            async def __aenter__(self):  # noqa: ANN204
                raise OperationalError("INSERT", {}, Exception("database is locked"))

            async def __aexit__(self, *_args: object) -> None:
                return None

        def flaky_begin(engine):  # noqa: ANN001, ANN202
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return _LockedTransaction()
            return original_begin(engine)

        monkeypatch.setattr(engine_type, "begin", flaky_begin)
        backoff_entered = asyncio.Event()
        release_backoff = asyncio.Event()
        original_sleep = asyncio.sleep

        async def gated_sleep(delay: float) -> None:
            if delay == 0.1 and not backoff_entered.is_set():
                backoff_entered.set()
                await release_backoff.wait()
                return
            await original_sleep(delay)

        monkeypatch.setattr("ouroboros.persistence.event_store.asyncio.sleep", gated_sleep)
        event = _make_event(aggregate_id=f"retry-close-{batch}")
        write_task = asyncio.create_task(
            store.append_batch([event]) if batch else store.append(event)
        )
        await asyncio.wait_for(backoff_entered.wait(), timeout=5)

        close_task = asyncio.create_task(store.close())
        await original_sleep(0)
        assert not close_task.done(), "close escaped while the logical write was backing off"

        release_backoff.set()
        with pytest.raises(PersistenceError) as exc_info:
            await asyncio.wait_for(write_task, timeout=5)
        assert exc_info.value.operation == ("append_batch" if batch else "append_with_rowid")
        await asyncio.wait_for(close_task, timeout=10)

        await store.initialize()
        assert await store.replay("test", f"retry-close-{batch}") == []
        await store.close()

    @pytest.mark.asyncio
    async def test_append_started_during_close_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Write admission must be synchronized with closing.

        Draining alone is not enough: a write that starts after the drain but
        before disposal would still commit post-shutdown. A write arriving
        while close() is in flight must be refused outright.
        """
        db_path = tmp_path / "admission.db"
        store = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await store.initialize()

        gate = asyncio.Event()
        entered = asyncio.Event()
        orig_checkpoint = store.checkpoint_wal

        async def gated_checkpoint() -> bool:
            entered.set()
            await gate.wait()
            return await orig_checkpoint()

        monkeypatch.setattr(store, "checkpoint_wal", gated_checkpoint)

        close_task = asyncio.create_task(store.close())
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            with pytest.raises(PersistenceError):
                await store.append(_make_event(aggregate_id="during-close"))
        finally:
            gate.set()
            await asyncio.wait_for(close_task, timeout=10)

        reopened = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await reopened.initialize()
        assert await reopened.replay("test", "during-close") == []
        await reopened.close()

    @pytest.mark.asyncio
    async def test_batch_started_during_close_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "admission-batch.db"
        store = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await store.initialize()

        gate = asyncio.Event()
        entered = asyncio.Event()
        orig_checkpoint = store.checkpoint_wal

        async def gated_checkpoint() -> bool:
            entered.set()
            await gate.wait()
            return await orig_checkpoint()

        monkeypatch.setattr(store, "checkpoint_wal", gated_checkpoint)

        close_task = asyncio.create_task(store.close())
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            with pytest.raises(PersistenceError):
                await store.append_batch([_make_event(aggregate_id="during-close-batch")])
        finally:
            gate.set()
            await asyncio.wait_for(close_task, timeout=10)

        reopened = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await reopened.initialize()
        assert await reopened.replay("test", "during-close-batch") == []
        await reopened.close()

    @pytest.mark.asyncio
    async def test_session_lifecycle_appends_during_close_are_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Session start/terminal CAS paths must honor the closing fence too.

        They dispatch above append()'s settlement wrapper, so without their
        own admission a session-start racing close() committed after
        disposal.
        """
        db_path = tmp_path / "lifecycle-close.db"
        store = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await store.initialize()

        started = BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id="sess-live",
            data={"execution_id": "exec-live"},
        )
        await store.append(started)

        gate = asyncio.Event()
        entered = asyncio.Event()
        orig_checkpoint = store.checkpoint_wal

        async def gated_checkpoint() -> bool:
            entered.set()
            await gate.wait()
            return await orig_checkpoint()

        monkeypatch.setattr(store, "checkpoint_wal", gated_checkpoint)

        close_task = asyncio.create_task(store.close())
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)

            with pytest.raises(PersistenceError):
                await store.append(
                    BaseEvent(
                        type="orchestrator.session.started",
                        aggregate_type="session",
                        aggregate_id="sess-during-close",
                        data={"execution_id": "exec-during-close"},
                    )
                )
            with pytest.raises(PersistenceError):
                await store.append(
                    BaseEvent(
                        type="orchestrator.session.completed",
                        aggregate_type="session",
                        aggregate_id="sess-live",
                        data={},
                    )
                )
        finally:
            gate.set()
            await asyncio.wait_for(close_task, timeout=10)

        reopened = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await reopened.initialize()
        assert await reopened.replay("session", "sess-during-close") == []
        terminal = [
            e
            for e in await reopened.replay("session", "sess-live")
            if e.type == "orchestrator.session.completed"
        ]
        assert terminal == []
        await reopened.close()

    @pytest.mark.asyncio
    async def test_initialize_cannot_reopen_admission_during_close(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """initialize() must serialize with an in-flight close().

        Clearing the closing flag mid-close reopened write admission: a probe
        paused close in the checkpoint, called initialize(), appended, and
        found the event durable after reopen.
        """
        db_path = tmp_path / "init-during-close.db"
        store = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await store.initialize()

        gate = asyncio.Event()
        entered = asyncio.Event()
        orig_checkpoint = store.checkpoint_wal

        async def gated_checkpoint() -> bool:
            entered.set()
            await gate.wait()
            return await orig_checkpoint()

        monkeypatch.setattr(store, "checkpoint_wal", gated_checkpoint)

        close_task = asyncio.create_task(store.close())
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)

            init_task = asyncio.create_task(store.initialize())
            await asyncio.sleep(0.05)
            with pytest.raises(PersistenceError):
                await store.append(_make_event(aggregate_id="init-during-close"))
        finally:
            gate.set()
            await asyncio.wait_for(close_task, timeout=10)
        await asyncio.wait_for(init_task, timeout=10)

        assert await store.replay("test", "init-during-close") == []
        await store.close()

    @pytest.mark.asyncio
    async def test_session_pause_append_during_close_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "pause-close.db"
        store = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await store.initialize()
        await store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="sess-pause",
                data={"execution_id": "exec-pause"},
            )
        )

        gate = asyncio.Event()
        entered = asyncio.Event()
        orig_checkpoint = store.checkpoint_wal

        async def gated_checkpoint() -> bool:
            entered.set()
            await gate.wait()
            return await orig_checkpoint()

        monkeypatch.setattr(store, "checkpoint_wal", gated_checkpoint)

        close_task = asyncio.create_task(store.close())
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            with pytest.raises(PersistenceError):
                await store.append_session_pause_if_active(
                    BaseEvent(
                        type="orchestrator.session.paused",
                        aggregate_type="session",
                        aggregate_id="sess-pause",
                        data={},
                    )
                )
        finally:
            gate.set()
            await asyncio.wait_for(close_task, timeout=10)

        reopened = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await reopened.initialize()
        paused = [
            e
            for e in await reopened.replay("session", "sess-pause")
            if e.type == "orchestrator.session.paused"
        ]
        assert paused == []
        await reopened.close()

    @pytest.mark.asyncio
    async def test_close_started_during_initialize_waits_for_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """initialize/close must be mutually exclusive in both orders.

        The close gate only guarded an already-running close; a close started
        DURING initialization returned first, leaving initialize reporting
        success on a disposed store.
        """
        import aiosqlite

        db_path = tmp_path / "init-order.db"
        store = EventStore(f"sqlite+aiosqlite:///{db_path}")

        gate = asyncio.Event()
        entered = asyncio.Event()
        orig_commit = aiosqlite.Connection.commit

        async def gated_commit(self):  # noqa: ANN001, ANN202
            entered.set()
            await gate.wait()
            return await orig_commit(self)

        monkeypatch.setattr(aiosqlite.Connection, "commit", gated_commit)

        init_task = asyncio.create_task(store.initialize())
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            close_task = asyncio.create_task(store.close())
            await asyncio.sleep(0.05)
            assert not close_task.done(), "close() finished while initialize() was still in flight"
        finally:
            gate.set()
        await asyncio.wait_for(init_task, timeout=10)
        await asyncio.wait_for(close_task, timeout=10)

    @pytest.mark.asyncio
    async def test_cancelled_close_leaves_recoverable_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Cancelling close() mid-drain must not wedge the lifecycle.

        Before the fix, cancellation during the drain left the closing flag
        set and the gate cleared forever; the next initialize() hung.
        """
        import aiosqlite

        db_path = tmp_path / "close-cancel.db"
        store = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await store.initialize()

        gate = asyncio.Event()
        entered = asyncio.Event()
        orig_commit = aiosqlite.Connection.commit

        async def gated_commit(self):  # noqa: ANN001, ANN202
            entered.set()
            await gate.wait()
            return await orig_commit(self)

        monkeypatch.setattr(aiosqlite.Connection, "commit", gated_commit)

        append_task = asyncio.create_task(store.append(_make_event(aggregate_id="wedge")))
        await asyncio.wait_for(entered.wait(), timeout=5)
        append_task.cancel()

        close_task = asyncio.create_task(store.close())
        await asyncio.sleep(0.05)
        close_task.cancel()
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(close_task, timeout=10)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(append_task, timeout=10)

        await asyncio.wait_for(store.initialize(), timeout=5)
        await store.append(_make_event(aggregate_id="recovered"))
        events = await store.replay("test", "recovered")
        assert len(events) == 1
        await store.close()

    @pytest.mark.asyncio
    async def test_cancelled_append_with_failing_commit_still_raises_cancellation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A settling transaction that fails must not replace the cancellation.

        The caller was cancelled; the write's own failure is secondary and is
        chained as the cause, so lifecycle code awaiting a cancelled task
        (e.g. the watchdog) still sees CancelledError and proceeds to its
        durable decision batch.
        """
        import aiosqlite

        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()

        gate = asyncio.Event()
        entered = asyncio.Event()

        async def failing_commit(self):  # noqa: ANN001, ANN202
            entered.set()
            await gate.wait()
            raise RuntimeError("commit blew up mid-settlement")

        monkeypatch.setattr(aiosqlite.Connection, "commit", failing_commit)

        task = asyncio.create_task(store.append(_make_event(aggregate_id="fail-append")))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        gate.set()
        with pytest.raises(asyncio.CancelledError) as exc_info:
            await asyncio.wait_for(task, timeout=5)
        assert exc_info.value.__cause__ is not None

    @pytest.mark.asyncio
    async def test_cancelled_batch_with_failing_commit_still_raises_cancellation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import aiosqlite

        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()

        gate = asyncio.Event()
        entered = asyncio.Event()

        async def failing_commit(self):  # noqa: ANN001, ANN202
            entered.set()
            await gate.wait()
            raise RuntimeError("batch commit blew up mid-settlement")

        monkeypatch.setattr(aiosqlite.Connection, "commit", failing_commit)

        task = asyncio.create_task(store.append_batch([_make_event(aggregate_id="fail-batch")]))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

    @pytest.mark.asyncio
    async def test_cancelled_append_batch_settles_before_cancellation_surfaces(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import aiosqlite

        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()

        gate = asyncio.Event()
        entered = asyncio.Event()
        orig_commit = aiosqlite.Connection.commit

        async def gated_commit(self):  # noqa: ANN001, ANN202
            entered.set()
            await gate.wait()
            return await orig_commit(self)

        monkeypatch.setattr(aiosqlite.Connection, "commit", gated_commit)

        events = [_make_event(aggregate_id="settle-batch") for _ in range(2)]
        task = asyncio.create_task(store.append_batch(events))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done(), "batch cancellation surfaced while the commit was still in flight"

        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

        stored = await store.replay("test", "settle-batch")
        assert len(stored) == 2
        await store.close()


class TestCountEvents:
    """Direct contract for EventStore.count_events (#1813)."""

    @pytest.mark.asyncio
    async def test_counts_zero_then_appended_events(self, tmp_path: Path) -> None:
        store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'count.db'}")
        await store.initialize()
        try:
            assert await store.count_events() == 0
            for index in range(3):
                await store.append(
                    BaseEvent(
                        type="execution.started",
                        timestamp=datetime.now(UTC),
                        aggregate_type="execution",
                        aggregate_id=f"exec_count_{index}",
                        data={},
                    )
                )
            assert await store.count_events() == 3
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_uninitialized_store_raises(self) -> None:
        store = EventStore("sqlite+aiosqlite:///:memory:")
        with pytest.raises(PersistenceError, match="not initialized"):
            await store.count_events()


class TestSqliteOnlyBackendContract:
    """#1832: the cursor APIs are rowid-built; the backend boundary says so."""

    def test_non_sqlite_url_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="SQLite-only"):
            EventStore("postgresql+asyncpg://user:pass@localhost/ouroboros")

    def test_every_sqlite_form_still_constructs(self, tmp_path) -> None:
        EventStore("sqlite+aiosqlite://")
        EventStore("sqlite+aiosqlite:///:memory:")
        EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
        EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}", read_only=True)

    def test_capability_report_matches_the_contract(self, tmp_path) -> None:
        """Cross-process capability requires a real file another process can open."""
        file_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
        assert file_store.supports_cross_process_workers is True

        assert EventStore("sqlite+aiosqlite://").supports_cross_process_workers is False, (
            "a pathless in-memory store is private to this process"
        )
        assert EventStore("sqlite+aiosqlite:///:memory:").supports_cross_process_workers is False
        named_memory = EventStore(
            "sqlite+aiosqlite:///file:namedmem?mode=memory&cache=shared&uri=true"
        )
        assert named_memory.supports_cross_process_workers is False, (
            "a named in-memory database is process-local regardless of its name"
        )
        assert named_memory.sqlite_path() is None

        for query_bearing in (
            "sqlite+aiosqlite:///?cache=shared",
            "sqlite+aiosqlite:///:memory:?cache=shared",
        ):
            store = EventStore(query_bearing)
            assert store.sqlite_path() is None, (
                f"a query string masqueraded as a path: {query_bearing}"
            )
            assert store.supports_cross_process_workers is False
