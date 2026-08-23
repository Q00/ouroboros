"""Tests for StartEvaluateHandler — async/fire-and-forget evaluate wrapper.

Background: the synchronous ``ouroboros_evaluate`` tool routinely exceeds an
MCP client's 120s tool-call timeout when the three-stage pipeline (mechanical
+ semantic + optional consensus) runs end-to-end. ``StartEvaluateHandler``
mirrors :class:`StartExecuteSeedHandler` and :class:`StartEvolveStepHandler`:
return a ``job_id`` immediately, run the evaluation in a JobManager-backed
background task, let callers poll ``job_status`` / ``job_wait`` / ``job_result``.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPToolError
from ouroboros.mcp.job_manager import JobManager
from ouroboros.mcp.tools.evaluation_handlers import (
    EvaluateHandler,
    StartEvaluateHandler,
)
from ouroboros.mcp.tools.job_observer import extract_job_observer_inline_handoff
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.persistence.event_store import EventStore


@pytest.fixture
async def event_store():
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture
def fake_inner_handler():
    """An EvaluateHandler stub whose ``handle`` returns a canned ok result."""
    inner = MagicMock(spec=EvaluateHandler)
    inner.handle = AsyncMock(
        return_value=Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="evaluated"),),
                is_error=False,
                meta={"final_approved": True},
            )
        )
    )
    return inner


class TestDefinition:
    def test_tool_name(self) -> None:
        h = StartEvaluateHandler()
        assert h.definition.name == "ouroboros_start_evaluate"

    def test_description_mentions_background(self) -> None:
        h = StartEvaluateHandler()
        assert "background" in h.definition.description.lower()

    def test_parameters_mirror_evaluate(self) -> None:
        h = StartEvaluateHandler()
        inner = EvaluateHandler()
        assert {p.name for p in h.definition.parameters} == {
            *(p.name for p in inner.definition.parameters),
            "auto_evolve",
            "seed_handoff_id",
        }
        assert "auto_evolve" not in {p.name for p in inner.definition.parameters}

    @pytest.mark.asyncio
    async def test_consumed_handoff_is_safe_for_detached_worker_reentry(
        self, tmp_path: Path, fake_inner_handler
    ) -> None:
        """The durable request carries the restored Seed, never a process-local handle."""
        from ouroboros.mcp.tools.seed_handoff import SeedHandoffRegistry

        store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'handoff.db'}")
        await store.initialize()
        manager = JobManager(store, durable_jobs=True)
        registry = SeedHandoffRegistry()
        handoff_id = registry.register(
            session_id="orch_detached_handoff",
            seed_content="goal: parent only\nprivate_marker: HIDDEN_PARENT_SEED\n",
        )
        snapshot = MagicMock(job_id="job_detached", cursor=0)
        snapshot.status.value = "queued"

        with patch(
            "ouroboros.mcp.tools.background.launch_detached_job",
            new=AsyncMock(return_value=snapshot),
        ) as launch:
            handler = StartEvaluateHandler(
                evaluate_handler=fake_inner_handler,
                event_store=store,
                job_manager=manager,
                seed_handoff_registry=registry,
            )
            result = await handler.handle(
                {
                    "session_id": "orch_detached_handoff",
                    "artifact": "partial artifact",
                    "seed_handoff_id": handoff_id,
                    "auto_evolve": True,
                }
            )

        try:
            assert result.is_ok
            request = launch.await_args.kwargs["request"]
            assert "seed_handoff_id" not in request.arguments
            assert request.arguments["seed_content"].startswith("goal: parent only")
            assert request.arguments["auto_evolve"] is True
            assert request.arguments["_auto_evolve_max_generations"] >= 1
            assert manager._reserved_job_ids == set()
            assert manager._forced_inline_allocations == set()
            assert manager._known_job_ids == {request.job_id}
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_failed_detached_acceptance_restores_handoff_for_retry(
        self, tmp_path: Path, fake_inner_handler
    ) -> None:
        from ouroboros.mcp.tools.seed_handoff import SeedHandoffRegistry

        store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'handoff-retry.db'}")
        await store.initialize()
        registry = SeedHandoffRegistry()
        handoff_id = registry.register(
            session_id="orch_handoff_retry",
            seed_content="goal: retry safely\n",
        )
        snapshot = MagicMock(job_id="job_handoff_retry", cursor=0)
        snapshot.status.value = "queued"
        manager = JobManager(store, durable_jobs=True)

        async def launch_with_definitive_rejection(*, request, **_kwargs):
            if not launch_with_definitive_rejection.rejected:
                launch_with_definitive_rejection.rejected = True
                manager.abandon_reserved_job_id(request.job_id)
                raise RuntimeError("not accepted")
            return snapshot

        launch_with_definitive_rejection.rejected = False
        launch = AsyncMock(side_effect=launch_with_definitive_rejection)
        handler = StartEvaluateHandler(
            evaluate_handler=fake_inner_handler,
            event_store=store,
            job_manager=manager,
            seed_handoff_registry=registry,
        )
        arguments = {
            "session_id": "orch_handoff_retry",
            "artifact": "partial artifact",
            "seed_handoff_id": handoff_id,
            "auto_evolve": True,
        }

        try:
            with patch("ouroboros.mcp.tools.background.launch_detached_job", new=launch):
                with pytest.raises(RuntimeError, match="not accepted"):
                    await handler.handle(arguments)
                assert registry.resolve(handoff_id, session_id="orch_handoff_retry") is not None
                result = await handler.handle(arguments)

            assert result.is_ok
            assert registry.resolve(handoff_id, session_id="orch_handoff_retry") is None
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_detached_request_construction_failure_restores_without_reservation_leak(
        self, tmp_path: Path, fake_inner_handler
    ) -> None:
        """Failure before worker launch is definitive non-acceptance."""
        from ouroboros.mcp.tools.seed_handoff import SeedHandoffRegistry

        store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'handoff-prespawn.db'}")
        await store.initialize()
        manager = JobManager(store, durable_jobs=True)
        registry = SeedHandoffRegistry()
        session_id = "orch_handoff_prespawn"
        handoff_id = registry.register(
            session_id=session_id,
            seed_content="goal: retry after pre-spawn failure\n",
        )
        handler = StartEvaluateHandler(
            evaluate_handler=fake_inner_handler,
            event_store=store,
            job_manager=manager,
            seed_handoff_registry=registry,
        )
        arguments = {
            "session_id": session_id,
            "artifact": "partial artifact",
            "seed_handoff_id": handoff_id,
            "auto_evolve": True,
        }

        try:
            with patch(
                "ouroboros.mcp.tools.background.Path.cwd",
                side_effect=FileNotFoundError("cwd vanished"),
            ):
                with pytest.raises(FileNotFoundError, match="cwd vanished"):
                    await handler.handle(arguments)

            assert registry.resolve(handoff_id, session_id=session_id) is not None
            assert manager._known_job_ids == set()
            assert manager._reserved_job_ids == set()
            assert manager._started_job_ids == set()
            assert manager._tasks == {}
            assert manager._runner_tasks == {}
            assert manager._monitors == {}
            assert await store.query_events(aggregate_type="job") == []

            snapshot = MagicMock(job_id="job_retry_after_prespawn", cursor=0)
            snapshot.status.value = "queued"
            with patch(
                "ouroboros.mcp.tools.background.launch_detached_job",
                new=AsyncMock(return_value=snapshot),
            ):
                retry = await handler.handle(arguments)

            assert retry.is_ok
            assert registry.resolve(handoff_id, session_id=session_id) is None
        finally:
            await store.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("path_method", "failure"),
        (("mkdir", "state directory denied"), ("unlink", "stale cleanup denied")),
    )
    async def test_detached_prespawn_filesystem_failure_restores_without_reservation_leak(
        self,
        tmp_path: Path,
        fake_inner_handler,
        monkeypatch: pytest.MonkeyPatch,
        path_method: str,
        failure: str,
    ) -> None:
        """A failure before detached spawn abandons the reserved job id."""
        from ouroboros.mcp.tools.seed_handoff import SeedHandoffRegistry

        monkeypatch.setenv("HOME", str(tmp_path))
        store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'handoff-stale-cleanup.db'}")
        await store.initialize()
        manager = JobManager(store, durable_jobs=True)
        registry = SeedHandoffRegistry()
        session_id = "orch_handoff_stale_cleanup"
        handoff_id = registry.register(
            session_id=session_id,
            seed_content="goal: retry after detached cleanup failure\n",
        )
        handler = StartEvaluateHandler(
            evaluate_handler=fake_inner_handler,
            event_store=store,
            job_manager=manager,
            seed_handoff_registry=registry,
        )
        arguments = {
            "session_id": session_id,
            "artifact": "partial artifact",
            "seed_handoff_id": handoff_id,
            "auto_evolve": True,
        }

        try:
            with patch(
                f"ouroboros.mcp.detached_jobs.Path.{path_method}",
                side_effect=PermissionError(failure),
            ):
                with pytest.raises(PermissionError, match=failure):
                    await handler.handle(arguments)

            assert registry.resolve(handoff_id, session_id=session_id) is not None
            assert manager._known_job_ids == set()
            assert manager._reserved_job_ids == set()
            assert manager._started_job_ids == set()
            assert manager._tasks == {}
            assert await store.query_events(aggregate_type="job") == []

            snapshot = MagicMock(job_id="job_retry_after_cleanup", cursor=0)
            snapshot.status.value = "queued"
            with patch(
                "ouroboros.mcp.tools.background.launch_detached_job",
                new=AsyncMock(return_value=snapshot),
            ):
                retry = await handler.handle(arguments)

            assert retry.is_ok
            assert registry.resolve(handoff_id, session_id=session_id) is None
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_ambiguous_detached_receipt_failure_keeps_handoff_consumed(
        self, tmp_path: Path, fake_inner_handler
    ) -> None:
        """A non-abandoned detached reservation is fail-closed on receipt error."""
        from ouroboros.mcp.tools.seed_handoff import SeedHandoffRegistry

        store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'handoff-ambiguous.db'}")
        await store.initialize()
        registry = SeedHandoffRegistry()
        handoff_id = registry.register(
            session_id="orch_handoff_ambiguous",
            seed_content="goal: do not duplicate an unresolved detached owner\n",
        )
        manager = JobManager(store, durable_jobs=True)
        handler = StartEvaluateHandler(
            evaluate_handler=fake_inner_handler,
            event_store=store,
            job_manager=manager,
            seed_handoff_registry=registry,
        )
        arguments = {
            "session_id": "orch_handoff_ambiguous",
            "artifact": "partial artifact",
            "seed_handoff_id": handoff_id,
            "auto_evolve": True,
        }

        try:
            with patch(
                "ouroboros.mcp.tools.background.launch_detached_job",
                new=AsyncMock(side_effect=RuntimeError("accepted receipt unreadable")),
            ):
                with pytest.raises(RuntimeError, match="accepted receipt unreadable"):
                    await handler.handle(arguments)

            assert registry.resolve(handoff_id, session_id="orch_handoff_ambiguous") is None
            retry = await handler.handle(arguments)
            assert retry.is_err
            assert "unknown or does not belong" in retry.error.message
            assert manager._reserved_job_ids == set()
            assert len(manager._known_job_ids) == 1
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_post_created_detached_snapshot_failure_keeps_handoff_consumed(
        self, tmp_path: Path, fake_inner_handler
    ) -> None:
        """Snapshot reconstruction failure cannot erase durable acceptance."""
        from ouroboros.mcp.tools.seed_handoff import SeedHandoffRegistry

        class PostCreatedSnapshotFailureManager(JobManager):
            async def get_snapshot(self, job_id: str):
                await super().get_snapshot(job_id)
                raise RuntimeError("post-created snapshot reconstruction failed")

        store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'post-created-failure.db'}")
        await store.initialize()
        registry = SeedHandoffRegistry()
        handoff_id = registry.register(
            session_id="orch_post_created_failure",
            seed_content="goal: preserve durable detached acceptance\n",
        )
        manager = PostCreatedSnapshotFailureManager(store, durable_jobs=True)
        handler = StartEvaluateHandler(
            evaluate_handler=fake_inner_handler,
            event_store=store,
            job_manager=manager,
            seed_handoff_registry=registry,
        )
        arguments = {
            "session_id": "orch_post_created_failure",
            "artifact": "partial artifact",
            "seed_handoff_id": handoff_id,
            "auto_evolve": True,
        }

        async def persist_then_fail_snapshot(**kwargs):
            job_manager = kwargs["job_manager"]
            request = kwargs["request"]
            await job_manager._append_event(
                "mcp.job.created",
                request.job_id,
                {
                    "job_type": "evaluate",
                    "status": "queued",
                    "message": "Queued evaluation",
                    "links": {},
                },
            )
            return await job_manager.get_snapshot(request.job_id)

        try:
            with patch(
                "ouroboros.mcp.tools.background.launch_detached_job",
                new=persist_then_fail_snapshot,
            ):
                with pytest.raises(
                    RuntimeError, match="post-created snapshot reconstruction failed"
                ):
                    await handler.handle(arguments)

            assert registry.resolve(handoff_id, session_id="orch_post_created_failure") is None
            assert manager._reserved_job_ids == set()
            assert len(manager._known_job_ids) == 1
            assert len(await store.query_events(aggregate_type="job")) == 1
            retry = await handler.handle(arguments)
            assert retry.is_err
            assert "unknown or does not belong" in retry.error.message
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_pending_detached_acceptance_keeps_handoff_consumed(
        self, tmp_path: Path, fake_inner_handler
    ) -> None:
        from ouroboros.mcp.detached_jobs import DetachedJobAcceptanceTimeout
        from ouroboros.mcp.tools.seed_handoff import SeedHandoffRegistry

        store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'handoff-pending.db'}")
        await store.initialize()
        registry = SeedHandoffRegistry()
        handoff_id = registry.register(
            session_id="orch_handoff_pending",
            seed_content="goal: do not duplicate\n",
        )
        manager = JobManager(store, durable_jobs=True)
        handler = StartEvaluateHandler(
            evaluate_handler=fake_inner_handler,
            event_store=store,
            job_manager=manager,
            seed_handoff_registry=registry,
        )

        try:
            with patch(
                "ouroboros.mcp.tools.background.launch_detached_job",
                new=AsyncMock(
                    side_effect=DetachedJobAcceptanceTimeout(
                        job_id="job_handoff_pending",
                        worker_pid=4242,
                        timeout_seconds=0.1,
                    )
                ),
            ):
                with pytest.raises(MCPToolError, match="acceptance is still pending") as raised:
                    await handler.handle(
                        {
                            "session_id": "orch_handoff_pending",
                            "artifact": "partial artifact",
                            "seed_handoff_id": handoff_id,
                            "auto_evolve": True,
                        }
                    )

            assert raised.value.error_code == "detached_job_acceptance_pending"
            assert registry.resolve(handoff_id, session_id="orch_handoff_pending") is None
            assert manager._reserved_job_ids == set()
            assert len(manager._known_job_ids) == 1
        finally:
            await store.close()


class TestRequiredArguments:
    @pytest.mark.asyncio
    async def test_missing_session_id_errors(self, event_store, fake_inner_handler) -> None:
        h = StartEvaluateHandler(evaluate_handler=fake_inner_handler, event_store=event_store)
        result = await h.handle({"artifact": "x"})
        assert result.is_err
        assert "session_id" in result.error.message

    @pytest.mark.asyncio
    async def test_missing_artifact_errors(self, event_store, fake_inner_handler) -> None:
        h = StartEvaluateHandler(evaluate_handler=fake_inner_handler, event_store=event_store)
        result = await h.handle({"session_id": "orch_abc"})
        assert result.is_err
        assert "artifact" in result.error.message


class TestBackgroundJobPath:
    """Non-plugin runtime: a JobManager-backed job is enqueued."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("failure_target", "failure"),
        (
            (
                "ouroboros.mcp.tools.evaluation_handlers.get_auto_evolve_enabled",
                "config unavailable",
            ),
            (
                "ouroboros.mcp.tools.evaluation_handlers.should_dispatch_via_plugin",
                "routing unavailable",
            ),
        ),
    )
    async def test_preallocation_runtime_error_restores_handoff(
        self,
        event_store: EventStore,
        fake_inner_handler,
        failure_target: str,
        failure: str,
    ) -> None:
        """Synchronous routing setup cannot consume an unaccepted handoff."""
        from ouroboros.mcp.tools.seed_handoff import SeedHandoffRegistry

        manager = JobManager(event_store, durable_jobs=False)
        registry = SeedHandoffRegistry()
        session_id = "orch_policy_failure"
        handoff_id = registry.register(
            session_id=session_id,
            seed_content="goal: retry after policy failure\n",
        )
        handler = StartEvaluateHandler(
            evaluate_handler=fake_inner_handler,
            event_store=event_store,
            job_manager=manager,
            seed_handoff_registry=registry,
        )
        arguments = {
            "session_id": session_id,
            "artifact": "partial artifact",
            "seed_handoff_id": handoff_id,
            "auto_evolve": True,
        }

        with patch(
            failure_target,
            side_effect=RuntimeError(failure),
        ):
            with pytest.raises(RuntimeError, match=failure):
                await handler.handle(arguments)

        assert registry.resolve(handoff_id, session_id=session_id) is not None
        assert manager._known_job_ids == set()
        assert manager._reserved_job_ids == set()
        assert manager._started_job_ids == set()
        assert manager._tasks == {}
        assert await event_store.query_events(aggregate_type="job") == []

        retry = await handler.handle(arguments)

        assert retry.is_ok
        assert registry.resolve(handoff_id, session_id=session_id) is None
        await manager.drain()

    @pytest.mark.asyncio
    async def test_preallocation_cancellation_restores_handoff_for_retry(
        self,
        event_store: EventStore,
        fake_inner_handler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cancellation before job allocation is definitive non-acceptance."""
        from ouroboros.mcp.tools.seed_handoff import SeedHandoffRegistry

        manager = JobManager(event_store, durable_jobs=False)
        registry = SeedHandoffRegistry()
        handoff_id = registry.register(
            session_id="orch_preallocation_cancel",
            seed_content="goal: retry after definitive cancellation\n",
        )
        handler = StartEvaluateHandler(
            evaluate_handler=fake_inner_handler,
            event_store=event_store,
            job_manager=manager,
            seed_handoff_registry=registry,
        )
        arguments = {
            "session_id": "orch_preallocation_cancel",
            "artifact": "partial artifact",
            "seed_handoff_id": handoff_id,
            "auto_evolve": True,
        }
        allocation_started = asyncio.Event()
        original_allocate = manager.allocate_job_id

        async def block_allocation() -> str:
            allocation_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        monkeypatch.setattr(manager, "allocate_job_id", block_allocation)
        pending = asyncio.create_task(handler.handle(arguments))
        await allocation_started.wait()
        pending.cancel()

        with pytest.raises(asyncio.CancelledError):
            await pending

        assert registry.resolve(handoff_id, session_id="orch_preallocation_cancel") is not None
        assert manager._known_job_ids == set()
        assert manager._reserved_job_ids == set()
        assert manager._tasks == {}
        assert manager._runner_tasks == {}
        assert manager._monitors == {}
        assert await event_store.query_events(aggregate_type="job") == []

        monkeypatch.setattr(manager, "allocate_job_id", original_allocate)
        retry = await handler.handle(arguments)

        assert retry.is_ok
        assert retry.value.meta["job_id"]
        assert registry.resolve(handoff_id, session_id="orch_preallocation_cancel") is None
        await manager.drain()

    @pytest.mark.asyncio
    async def test_local_preenqueue_cancellation_restores_handoff_for_retry(
        self,
        event_store: EventStore,
        fake_inner_handler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cancellation before local ownership is definitive non-acceptance."""
        from ouroboros.mcp.tools.seed_handoff import SeedHandoffRegistry

        manager = JobManager(event_store, durable_jobs=False)
        registry = SeedHandoffRegistry()
        handoff_id = registry.register(
            session_id="orch_local_preenqueue_cancel",
            seed_content="goal: retry before local ownership\n",
        )
        handler = StartEvaluateHandler(
            evaluate_handler=fake_inner_handler,
            event_store=event_store,
            job_manager=manager,
            seed_handoff_registry=registry,
        )
        arguments = {
            "session_id": "orch_local_preenqueue_cancel",
            "artifact": "partial artifact",
            "seed_handoff_id": handoff_id,
            "auto_evolve": True,
        }
        enqueue_started = asyncio.Event()
        original_start_job = manager.start_job

        async def block_enqueue(**_kwargs):
            enqueue_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        monkeypatch.setattr(manager, "start_job", block_enqueue)
        pending = asyncio.create_task(handler.handle(arguments))
        await enqueue_started.wait()
        pending.cancel()

        with pytest.raises(asyncio.CancelledError):
            await pending

        assert registry.resolve(handoff_id, session_id="orch_local_preenqueue_cancel") is not None
        assert manager._known_job_ids == set()
        assert manager._reserved_job_ids == set()
        assert manager._forced_inline_allocations == set()
        assert manager._started_job_ids == set()
        assert manager._tasks == {}
        assert await event_store.query_events(aggregate_type="job") == []

        monkeypatch.setattr(manager, "start_job", original_start_job)
        retry = await handler.handle(arguments)

        assert retry.is_ok
        assert registry.resolve(handoff_id, session_id="orch_local_preenqueue_cancel") is None
        await manager.drain()

    @pytest.mark.asyncio
    async def test_local_created_commit_cancellation_keeps_runner_and_handoff_consumed(
        self,
        event_store: EventStore,
        fake_inner_handler,
    ) -> None:
        """A settled created receipt crosses local acceptance atomically."""
        from ouroboros.mcp.tools.seed_handoff import SeedHandoffRegistry

        class CreatedCommitBarrierJobManager(JobManager):
            def __init__(self, store: EventStore) -> None:
                super().__init__(store, durable_jobs=False)
                self.created_committed = asyncio.Event()
                self._blocked_created = False

            async def _append_event(self, event_type, job_id, data, *, event_id=None):
                await super()._append_event(
                    event_type,
                    job_id,
                    data,
                    event_id=event_id,
                )
                if event_type == "mcp.job.created" and not self._blocked_created:
                    self._blocked_created = True
                    self.created_committed.set()
                    await asyncio.Event().wait()

        work_started = asyncio.Event()
        release_work = asyncio.Event()

        async def finish_after_release(_arguments, **_kwargs):
            work_started.set()
            await release_work.wait()
            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="evaluated"),),
                    is_error=False,
                    meta={"final_approved": True},
                )
            )

        fake_inner_handler.handle = AsyncMock(side_effect=finish_after_release)
        manager = CreatedCommitBarrierJobManager(event_store)
        registry = SeedHandoffRegistry()
        session_id = "orch_created_commit_cancel"
        handoff_id = registry.register(
            session_id=session_id,
            seed_content="goal: preserve accepted created receipt\n",
        )
        handler = StartEvaluateHandler(
            evaluate_handler=fake_inner_handler,
            event_store=event_store,
            job_manager=manager,
            seed_handoff_registry=registry,
        )
        arguments = {
            "session_id": session_id,
            "artifact": "partial artifact",
            "seed_handoff_id": handoff_id,
            "auto_evolve": True,
        }

        pending = asyncio.create_task(handler.handle(arguments))
        await manager.created_committed.wait()
        pending.cancel()

        with pytest.raises(asyncio.CancelledError):
            await pending

        await work_started.wait()
        assert len(manager._started_job_ids) == 1
        job_id = next(iter(manager._started_job_ids))
        assert registry.resolve(handoff_id, session_id=session_id) is None
        assert manager.has_live_job_task(job_id)

        retry = await handler.handle(arguments)
        assert retry.is_err
        assert "unknown or does not belong" in retry.error.message
        created_events = await event_store.query_events(
            aggregate_type="job", event_type="mcp.job.created"
        )
        assert [event.aggregate_id for event in created_events] == [job_id]

        release_work.set()
        for _ in range(100):
            snapshot = await manager.get_snapshot(job_id)
            if snapshot.is_terminal:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("accepted created-receipt job did not terminalize")

        await manager.drain()
        await asyncio.sleep(0)
        assert not manager.has_live_job_task(job_id)
        assert manager._tasks == {}
        assert manager._runner_tasks == {}
        assert manager._monitors == {}

    @pytest.mark.asyncio
    async def test_local_created_commit_confirmation_error_fails_closed(
        self,
        event_store: EventStore,
        fake_inner_handler,
    ) -> None:
        """An unreadable committed receipt cannot reopen the Seed handoff."""
        from ouroboros.mcp.tools.seed_handoff import SeedHandoffRegistry

        class UnreadableCreatedReceiptJobManager(JobManager):
            def __init__(self, store: EventStore) -> None:
                super().__init__(store, durable_jobs=False)
                self.fail_confirmation = False

            async def _append_event(self, event_type, job_id, data, *, event_id=None):
                await super()._append_event(
                    event_type,
                    job_id,
                    data,
                    event_id=event_id,
                )
                if event_type == "mcp.job.created":
                    self.fail_confirmation = True
                    raise RuntimeError("created receipt response lost")

            async def _job_exists(self, job_id: str) -> bool:
                if self.fail_confirmation:
                    self.fail_confirmation = False
                    raise RuntimeError("created receipt confirmation unavailable")
                return await super()._job_exists(job_id)

        manager = UnreadableCreatedReceiptJobManager(event_store)
        registry = SeedHandoffRegistry()
        session_id = "orch_created_confirmation_error"
        handoff_id = registry.register(
            session_id=session_id,
            seed_content="goal: fail closed on unreadable created receipt\n",
        )
        handler = StartEvaluateHandler(
            evaluate_handler=fake_inner_handler,
            event_store=event_store,
            job_manager=manager,
            seed_handoff_registry=registry,
        )
        arguments = {
            "session_id": session_id,
            "artifact": "partial artifact",
            "seed_handoff_id": handoff_id,
            "auto_evolve": True,
        }

        with pytest.raises(RuntimeError, match="created receipt response lost"):
            await handler.handle(arguments)

        assert len(manager._started_job_ids) == 1
        job_id = next(iter(manager._started_job_ids))
        fake_inner_handler.handle.assert_not_called()
        assert registry.resolve(handoff_id, session_id=session_id) is None
        retry = await handler.handle(arguments)
        assert retry.is_err
        assert "unknown or does not belong" in retry.error.message

        for _ in range(100):
            snapshot = await manager.get_snapshot(job_id)
            if snapshot.is_terminal:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("fail-closed accepted job did not terminalize")

        await manager.drain()
        await asyncio.sleep(0)
        assert not manager.has_live_job_task(job_id)
        assert manager._tasks == {}
        assert manager._runner_tasks == {}
        assert manager._monitors == {}

    @pytest.mark.asyncio
    async def test_local_transient_confirmation_failure_restores_handoff(
        self,
        event_store: EventStore,
        fake_inner_handler,
    ) -> None:
        """A retry that proves no receipt exists makes rejection definitive."""
        from ouroboros.mcp.tools.seed_handoff import SeedHandoffRegistry

        class UnconfirmedCreatedReceiptJobManager(JobManager):
            def __init__(self, store: EventStore) -> None:
                super().__init__(store, durable_jobs=False)
                self.fail_confirmation = False

            async def _append_event(self, event_type, job_id, data, *, event_id=None):
                if event_type == "mcp.job.created":
                    self.fail_confirmation = True
                    raise RuntimeError("created append rejected")
                await super()._append_event(
                    event_type,
                    job_id,
                    data,
                    event_id=event_id,
                )

            async def _job_exists(self, job_id: str) -> bool:
                if self.fail_confirmation:
                    self.fail_confirmation = False
                    raise RuntimeError("created confirmation unavailable")
                return await super()._job_exists(job_id)

        manager = UnconfirmedCreatedReceiptJobManager(event_store)
        registry = SeedHandoffRegistry()
        session_id = "orch_unconfirmed_created"
        handoff_id = registry.register(
            session_id=session_id,
            seed_content="goal: never run without a durable receipt\n",
        )
        handler = StartEvaluateHandler(
            evaluate_handler=fake_inner_handler,
            event_store=event_store,
            job_manager=manager,
            seed_handoff_registry=registry,
        )
        arguments = {
            "session_id": session_id,
            "artifact": "partial artifact",
            "seed_handoff_id": handoff_id,
            "auto_evolve": True,
        }

        with pytest.raises(RuntimeError, match="created append rejected"):
            await handler.handle(arguments)

        fake_inner_handler.handle.assert_not_called()
        assert manager._started_job_ids == set()
        assert manager._known_job_ids == set()
        assert manager._reserved_job_ids == set()
        assert registry.resolve(handoff_id, session_id=session_id) is not None
        assert manager._tasks == {}
        assert manager._runner_tasks == {}
        assert manager._monitors == {}
        assert await event_store.query_events(aggregate_type="job") == []

    @pytest.mark.asyncio
    async def test_local_persistently_unreadable_confirmation_stays_fail_closed(
        self,
        event_store: EventStore,
        fake_inner_handler,
    ) -> None:
        """Persistent receipt ambiguity consumes the handoff without starting work."""
        from ouroboros.mcp.tools.seed_handoff import SeedHandoffRegistry

        class PersistentlyUnreadableReceiptJobManager(JobManager):
            def __init__(self, store: EventStore) -> None:
                super().__init__(store, durable_jobs=False)
                self.fail_confirmation = False

            async def _append_event(self, event_type, job_id, data, *, event_id=None):
                if event_type == "mcp.job.created":
                    self.fail_confirmation = True
                    raise RuntimeError("created append outcome unknown")
                await super()._append_event(event_type, job_id, data, event_id=event_id)

            async def _job_exists(self, job_id: str) -> bool:
                if self.fail_confirmation:
                    raise RuntimeError("created confirmation unavailable")
                return await super()._job_exists(job_id)

        manager = PersistentlyUnreadableReceiptJobManager(event_store)
        registry = SeedHandoffRegistry()
        session_id = "orch_persistently_unreadable_created"
        handoff_id = registry.register(
            session_id=session_id,
            seed_content="goal: fail closed while receipt state is unknowable\n",
        )
        handler = StartEvaluateHandler(
            evaluate_handler=fake_inner_handler,
            event_store=event_store,
            job_manager=manager,
            seed_handoff_registry=registry,
        )
        arguments = {
            "session_id": session_id,
            "artifact": "partial artifact",
            "seed_handoff_id": handoff_id,
            "auto_evolve": True,
        }

        with pytest.raises(RuntimeError, match="created append outcome unknown"):
            await handler.handle(arguments)

        fake_inner_handler.handle.assert_not_called()
        assert len(manager._started_job_ids) == 1
        assert registry.resolve(handoff_id, session_id=session_id) is None
        assert manager._tasks == {}
        assert manager._runner_tasks == {}
        assert manager._monitors == {}
        assert await event_store.query_events(aggregate_type="job") == []
        retry = await handler.handle(arguments)
        assert retry.is_err
        assert "unknown or does not belong" in retry.error.message

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failed_create_task_call", (1, 2, 3))
    async def test_local_created_receipt_task_registration_failures_stay_owned(
        self,
        event_store: EventStore,
        fake_inner_handler,
        monkeypatch: pytest.MonkeyPatch,
        failed_create_task_call: int,
    ) -> None:
        """Partial task setup cannot reopen a durable created receipt."""
        from ouroboros.mcp import job_start
        from ouroboros.mcp.tools.seed_handoff import SeedHandoffRegistry

        manager = JobManager(event_store, durable_jobs=False)
        registry = SeedHandoffRegistry()
        session_id = f"orch_registration_failure_{failed_create_task_call}"
        handoff_id = registry.register(
            session_id=session_id,
            seed_content="goal: keep durable receipt single-owner\n",
        )
        handler = StartEvaluateHandler(
            evaluate_handler=fake_inner_handler,
            event_store=event_store,
            job_manager=manager,
            seed_handoff_registry=registry,
        )
        arguments = {
            "session_id": session_id,
            "artifact": "partial artifact",
            "seed_handoff_id": handoff_id,
            "auto_evolve": True,
        }
        original_create_task = job_start._create_task
        create_task_calls = 0

        def fail_selected_create_task(coro):
            nonlocal create_task_calls
            create_task_calls += 1
            if create_task_calls == failed_create_task_call:
                raise RuntimeError(f"create_task {failed_create_task_call} failed")
            return original_create_task(coro)

        monkeypatch.setattr(job_start, "_create_task", fail_selected_create_task)
        if failed_create_task_call < 3:
            with pytest.raises(
                RuntimeError,
                match=rf"create_task {failed_create_task_call} failed",
            ):
                await handler.handle(arguments)
        else:
            started = await handler.handle(arguments)
            assert started.is_ok
        monkeypatch.setattr(job_start, "_create_task", original_create_task)

        created_events = await event_store.query_events(
            aggregate_type="job", event_type="mcp.job.created"
        )
        assert len(created_events) == 1
        job_id = created_events[0].aggregate_id

        if failed_create_task_call < 3:
            assert registry.resolve(handoff_id, session_id=session_id) is not None
            assert job_id not in manager._started_job_ids
            assert not manager.has_live_job_task(job_id)
            failed_snapshot = await manager.get_snapshot(job_id)
            assert failed_snapshot.status.value == "interrupted"
            retry = await handler.handle(arguments)
            assert retry.is_ok
            assert retry.value.meta["job_id"] != job_id
            assert registry.resolve(handoff_id, session_id=session_id) is None
            job_id = retry.value.meta["job_id"]
        else:
            assert manager._started_job_ids == {job_id}
            assert registry.resolve(handoff_id, session_id=session_id) is None
            retry = await handler.handle(arguments)
            assert retry.is_err
            assert "unknown or does not belong" in retry.error.message

        for _ in range(100):
            snapshot = await manager.get_snapshot(job_id)
            if snapshot.is_terminal:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("partially registered job did not terminalize")

        await manager.drain()
        await asyncio.sleep(0)
        assert not manager.has_live_job_task(job_id)
        assert manager._tasks == {}
        assert manager._runner_tasks == {}
        assert manager._monitors == {}

    @pytest.mark.asyncio
    async def test_local_postacceptance_cancellation_keeps_handoff_consumed(
        self,
        event_store: EventStore,
        fake_inner_handler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cancellation after local ownership must not permit a duplicate retry."""
        from ouroboros.mcp.tools.seed_handoff import SeedHandoffRegistry

        manager = JobManager(event_store, durable_jobs=False)
        registry = SeedHandoffRegistry()
        handoff_id = registry.register(
            session_id="orch_local_accepted_cancel",
            seed_content="goal: do not retry after local ownership\n",
        )
        handler = StartEvaluateHandler(
            evaluate_handler=fake_inner_handler,
            event_store=event_store,
            job_manager=manager,
            seed_handoff_registry=registry,
        )
        arguments = {
            "session_id": "orch_local_accepted_cancel",
            "artifact": "partial artifact",
            "seed_handoff_id": handoff_id,
            "auto_evolve": True,
        }
        accepted = asyncio.Event()
        work_started = asyncio.Event()
        release_work = asyncio.Event()
        original_start_job = manager.start_job

        async def finish_after_release(_arguments, **_kwargs):
            work_started.set()
            await release_work.wait()
            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="evaluated"),),
                    is_error=False,
                    meta={"final_approved": True},
                )
            )

        fake_inner_handler.handle = AsyncMock(side_effect=finish_after_release)

        async def block_after_acceptance(**kwargs):
            await original_start_job(**kwargs)
            accepted.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        monkeypatch.setattr(manager, "start_job", block_after_acceptance)
        pending = asyncio.create_task(handler.handle(arguments))
        await accepted.wait()
        await work_started.wait()
        job_id = next(iter(manager._started_job_ids))
        job_task = manager._tasks[job_id]
        pending.cancel()

        with pytest.raises(asyncio.CancelledError):
            await pending

        assert registry.resolve(handoff_id, session_id="orch_local_accepted_cancel") is None
        release_work.set()
        await asyncio.wait_for(asyncio.shield(job_task), timeout=1.0)
        assert (await manager.get_snapshot(job_id)).is_terminal
        await manager.drain()

    @pytest.mark.asyncio
    async def test_local_postacceptance_receipt_failure_keeps_single_handoff_chain(
        self,
        event_store: EventStore,
    ) -> None:
        """An accepted runner survives an ordinary receipt-read failure (#1938)."""
        from ouroboros.mcp.tools.seed_handoff import SeedHandoffRegistry

        class ReceiptFailingJobManager(JobManager):
            def __init__(self, store: EventStore) -> None:
                super().__init__(store, durable_jobs=False)
                self.receipt_owner: asyncio.Task[object] | None = None
                self.accepted_receipt_read = asyncio.Event()
                self.release_receipt_read = asyncio.Event()
                self.receipt_failed = False

            async def get_snapshot(self, job_id: str):
                if (
                    not self.receipt_failed
                    and asyncio.current_task() is self.receipt_owner
                    and self.has_accepted_job(job_id)
                ):
                    self.accepted_receipt_read.set()
                    await self.release_receipt_read.wait()
                    self.receipt_failed = True
                    raise RuntimeError("accepted receipt snapshot failed")
                return await super().get_snapshot(job_id)

        evaluation_started = asyncio.Event()
        release_evaluation = asyncio.Event()
        evaluation_calls = 0

        async def reject_after_release(_arguments, **_kwargs):
            nonlocal evaluation_calls
            evaluation_calls += 1
            evaluation_started.set()
            await release_evaluation.wait()
            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="REJECTED"),),
                    meta={"final_approved": False, "highest_stage": 2},
                )
            )

        ralph_calls: list[dict[str, object]] = []

        class FakeStartRalphHandler:
            async def handle(self, arguments):
                ralph_calls.append(arguments)
                return Result.ok(MCPToolResult(meta={"job_id": "job_ralph_once"}))

        manager = ReceiptFailingJobManager(event_store)
        registry = SeedHandoffRegistry()
        session_id = "orch_accepted_receipt_failure"
        handoff_id = registry.register(
            session_id=session_id,
            seed_content=(
                "goal: converge once\n"
                "acceptance_criteria: [receipt ownership is safe]\n"
                "ontology_schema:\n"
                "  name: ReceiptOwnership\n"
                "  description: Accepted background evaluation ownership\n"
                "metadata:\n"
                "  seed_id: seed-receipt-once\n"
                "  ambiguity_score: 0.1\n"
            ),
        )
        inner = MagicMock(spec=EvaluateHandler)
        inner.handle = AsyncMock(side_effect=reject_after_release)
        handler = StartEvaluateHandler(
            evaluate_handler=inner,
            event_store=event_store,
            job_manager=manager,
            start_ralph_handler=FakeStartRalphHandler(),  # type: ignore[arg-type]
            seed_handoff_registry=registry,
        )
        arguments = {
            "session_id": session_id,
            "artifact": "partial artifact",
            "seed_handoff_id": handoff_id,
            "auto_evolve": True,
        }

        first = asyncio.create_task(handler.handle(arguments))
        manager.receipt_owner = first
        await manager.accepted_receipt_read.wait()
        await evaluation_started.wait()

        assert len(manager._started_job_ids) == 1
        job_id = next(iter(manager._started_job_ids))
        assert manager.has_live_job_task(job_id)

        manager.release_receipt_read.set()
        with pytest.raises(RuntimeError, match="accepted receipt snapshot failed"):
            await first

        assert registry.resolve(handoff_id, session_id=session_id) is None
        retry = await handler.handle(arguments)
        assert retry.is_err
        assert "unknown or does not belong" in retry.error.message
        assert manager._started_job_ids == {job_id}
        assert evaluation_calls == 1

        release_evaluation.set()
        for _ in range(100):
            snapshot = await manager.get_snapshot(job_id)
            if snapshot.is_terminal:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("accepted evaluation did not terminalize")

        assert snapshot.result_meta["chained_ralph_job_id"] == "job_ralph_once"
        assert len(ralph_calls) == 1
        job_events = await event_store.query_events(aggregate_type="job")
        assert {event.aggregate_id for event in job_events} == {job_id}

        await manager.drain()
        await asyncio.sleep(0)
        assert not manager.has_live_job_task(job_id)
        assert manager._tasks == {}
        assert manager._runner_tasks == {}
        assert manager._monitors == {}

    @pytest.mark.asyncio
    async def test_returns_job_id_immediately(self, event_store, fake_inner_handler) -> None:
        job_manager = MagicMock()
        job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
        snapshot = MagicMock()
        snapshot.job_id = "job_abc123"
        snapshot.status.value = "queued"
        snapshot.cursor = 0

        async def _start_job(*, runner, **_):
            # Close the runner coroutine so the test does not leak it.
            if inspect.iscoroutine(runner):
                runner.close()
            return snapshot

        job_manager.start_job = AsyncMock(side_effect=_start_job)

        h = StartEvaluateHandler(
            evaluate_handler=fake_inner_handler,
            event_store=event_store,
            job_manager=job_manager,
        )
        result = await h.handle({"session_id": "orch_xyz", "artifact": "code"})

        assert result.is_ok
        assert result.value.meta["job_id"] == "job_abc123"
        assert result.value.meta["session_id"] == "orch_xyz"
        assert result.value.meta["status"] == "queued"
        observer = result.value.meta["job_observer"]
        assert observer["job_id"] == "job_abc123"
        assert observer["session_id"] == "orch_xyz"
        assert observer["recommended_host_action"] == "spawn_observer_session"
        assert (
            extract_job_observer_inline_handoff(
                result.value.text_content,
                expected_job_id="job_abc123",
                expected_session_id="orch_xyz",
            )
            == observer
        )
        # Inner evaluate must NOT have been called synchronously — fire-and-forget.
        fake_inner_handler.handle.assert_not_called()
        job_manager.start_job.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_job_type_and_links(self, event_store, fake_inner_handler) -> None:
        job_manager = MagicMock()
        job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
        snapshot = MagicMock(job_id="job_x", cursor=0)
        snapshot.status.value = "queued"

        async def _start_job(*, runner, **_):
            if inspect.iscoroutine(runner):
                runner.close()
            return snapshot

        job_manager.start_job = AsyncMock(side_effect=_start_job)

        h = StartEvaluateHandler(
            evaluate_handler=fake_inner_handler,
            event_store=event_store,
            job_manager=job_manager,
        )
        await h.handle({"session_id": "orch_xyz", "artifact": "code"})

        call_kwargs = job_manager.start_job.await_args.kwargs
        assert call_kwargs["job_type"] == "evaluate"
        assert call_kwargs["links"].session_id == "orch_xyz"


class TestPluginModeDispatch:
    """OpenCode plugin mode: terminal subagent delegation, no job enqueue."""

    @pytest.fixture
    def handler(self, event_store, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            "ouroboros.mcp.tools.evaluation_handlers.get_auto_evolve_enabled",
            lambda: False,
        )
        return StartEvaluateHandler(
            evaluate_handler=MagicMock(),
            event_store=event_store,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )

    @pytest.mark.asyncio
    async def test_returns_none_job_id(self, handler) -> None:
        result = await handler.handle({"session_id": "orch_xyz", "artifact": "code"})
        assert result.is_ok
        assert result.value.meta["job_id"] is None

    @pytest.mark.asyncio
    async def test_pre_envelope_cancellation_restores_handoff_for_retry(
        self,
        event_store: EventStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ouroboros.mcp.tools.seed_handoff import SeedHandoffRegistry

        registry = SeedHandoffRegistry()
        handoff_id = registry.register(
            session_id="orch_plugin_cancel",
            seed_content="goal: retry plugin dispatch safely\n",
        )
        handler = StartEvaluateHandler(
            event_store=event_store,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
            seed_handoff_registry=registry,
        )
        arguments = {
            "session_id": "orch_plugin_cancel",
            "artifact": "partial artifact",
            "seed_handoff_id": handoff_id,
            "auto_evolve": False,
        }
        initialize_started = asyncio.Event()
        original_initialize = event_store.initialize

        async def blocking_initialize() -> None:
            initialize_started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(event_store, "initialize", blocking_initialize)
        dispatch = asyncio.create_task(handler.handle(arguments))
        await initialize_started.wait()
        dispatch.cancel()

        with pytest.raises(asyncio.CancelledError):
            await dispatch

        assert registry.resolve(handoff_id, session_id="orch_plugin_cancel") is not None

        monkeypatch.setattr(event_store, "initialize", original_initialize)
        retry = await handler.handle(arguments)

        assert retry.is_ok
        assert retry.value.meta["status"] == "delegated_to_plugin"
        assert registry.resolve(handoff_id, session_id="orch_plugin_cancel") is None

    @pytest.mark.asyncio
    async def test_status_delegated_to_plugin(self, handler) -> None:
        result = await handler.handle({"session_id": "orch_xyz", "artifact": "code"})
        assert result.value.meta["status"] == "delegated_to_plugin"
        assert result.value.meta["dispatch_mode"] == "plugin"

    @pytest.mark.asyncio
    async def test_subagent_payload_present(self, handler) -> None:
        result = await handler.handle({"session_id": "orch_xyz", "artifact": "code"})
        assert "_subagent" in result.value.meta
        assert result.value.meta["_subagent"]["tool_name"] == "ouroboros_evaluate"

    @pytest.mark.asyncio
    async def test_multi_acceptance_criteria_preserved_in_plugin_payload(self, handler) -> None:
        """Plugin dispatch must not silently drop the multi-AC checklist input.

        Regression guard for PR #882 review feedback: ``acceptance_criteria``
        (plural list) was being dropped on the plugin path because only the
        singular ``acceptance_criterion`` field was forwarded. The non-plugin
        path normalises both inputs inside ``EvaluateHandler.handle``, so the
        plugin path must do the equivalent before building the subagent
        payload.
        """
        result = await handler.handle(
            {
                "session_id": "orch_xyz",
                "artifact": "code",
                "acceptance_criteria": ["First AC", "Second AC", "Third AC"],
            }
        )
        assert result.is_ok
        payload_context = result.value.meta["_subagent"]["context"]
        forwarded = payload_context["acceptance_criterion"] or ""
        assert "First AC" in forwarded
        assert "Second AC" in forwarded
        assert "Third AC" in forwarded

    @pytest.mark.asyncio
    async def test_seed_acceptance_criteria_preserved_in_plugin_payload(self, handler) -> None:
        seed_content = (
            "goal: Build a CLI task manager\n"
            "acceptance_criteria:\n"
            "  - Tasks can be created\n"
            "  - Tasks can be listed\n"
            "ontology_schema:\n"
            "  name: TaskManager\n"
            "  description: Task management domain\n"
            "metadata:\n"
            "  ambiguity_score: 0.15\n"
        )

        result = await handler.handle(
            {
                "session_id": "orch_xyz",
                "artifact": "code",
                "seed_content": seed_content,
            }
        )

        assert result.is_ok
        payload_context = result.value.meta["_subagent"]["context"]
        forwarded = payload_context["acceptance_criterion"] or ""
        assert "Tasks can be created" in forwarded
        assert "Tasks can be listed" in forwarded

    @pytest.mark.asyncio
    async def test_restored_handoff_stays_hidden_in_delegated_evaluation(self, event_store) -> None:
        from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler
        from ouroboros.mcp.tools.seed_handoff import SeedHandoffRegistry

        registry = SeedHandoffRegistry()
        seed_content = (
            "goal: Judge the artifact\n"
            "constraints:\n"
            "  - Never print python secret_check.py --token TOP_SECRET\n"
            "  - Never print HIDDEN_SENTINEL\n"
            "acceptance_criteria:\n"
            "  - description: Produce output.json without HIDDEN_SENTINEL\n"
            "    expected_artifacts: [output.json]\n"
            "    verify_command: python secret_check.py --token TOP_SECRET\n"
            "    output_assertion: HIDDEN_SENTINEL\n"
            "ontology_schema:\n"
            "  name: HiddenContractArtifact\n"
            "  description: Artifact with parent-owned verification\n"
            "metadata:\n"
            "  ambiguity_score: 0.0\n"
        )
        execute_handler = ExecuteSeedHandler(
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
            seed_handoff_registry=registry,
        )
        execution = await execute_handler.handle(
            {
                "seed_content": seed_content,
                "auto_evaluate": True,
                "auto_evolve": False,
            }
        )
        assert execution.is_ok
        execution_payload = execution.value.meta["_subagent"]
        session_id = execution.value.meta["session_id"]
        handoff_id = execution_payload["context"]["seed_handoff_id"]
        assert "status `delegated_to_plugin` with no job_id" in execution_payload["prompt"]
        assert "do not poll job tools" in execution_payload["prompt"]
        assert "poll the returned job" not in execution_payload["prompt"]

        handler = StartEvaluateHandler(
            evaluate_handler=MagicMock(),
            event_store=event_store,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
            seed_handoff_registry=registry,
        )

        result = await handler.handle(
            {
                "session_id": session_id,
                "artifact": "partial artifact HIDDEN_SENTINEL",
                "seed_handoff_id": handoff_id,
                "auto_evolve": False,
            }
        )

        assert result.is_ok
        assert result.value.meta["status"] == "delegated_to_plugin"
        assert result.value.meta["job_id"] is None
        payload = result.value.meta["_subagent"]
        visible = payload["prompt"] + str(payload["context"])
        assert "Produce output.json" in visible
        assert "output.json" in visible
        assert "TOP_SECRET" not in visible
        assert "HIDDEN_SENTINEL" not in visible
        assert "verify_command" not in visible
        assert "output_assertion" not in visible

    @pytest.mark.asyncio
    async def test_evaluate_handler_seed_acceptance_criteria_preserved_in_plugin_payload(
        self, event_store
    ) -> None:
        seed_content = (
            "goal: Build a CLI task manager\n"
            "acceptance_criteria:\n"
            "  - Tasks can be created\n"
            "  - Tasks can be listed\n"
            "ontology_schema:\n"
            "  name: TaskManager\n"
            "  description: Task management domain\n"
            "metadata:\n"
            "  ambiguity_score: 0.15\n"
        )
        handler = EvaluateHandler(
            event_store=event_store,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )

        result = await handler.handle(
            {
                "session_id": "orch_xyz",
                "artifact": "code",
                "seed_content": seed_content,
            }
        )

        assert result.is_ok
        payload_context = result.value.meta["_subagent"]["context"]
        forwarded = payload_context["acceptance_criterion"] or ""
        assert "Tasks can be created" in forwarded
        assert "Tasks can be listed" in forwarded

    @pytest.mark.asyncio
    async def test_singular_acceptance_criterion_still_forwarded(self, handler) -> None:
        result = await handler.handle(
            {
                "session_id": "orch_xyz",
                "artifact": "code",
                "acceptance_criterion": "Only one",
            }
        )
        payload_context = result.value.meta["_subagent"]["context"]
        assert payload_context["acceptance_criterion"] == "Only one"

    @pytest.mark.asyncio
    async def test_plural_takes_precedence_over_singular(self, handler) -> None:
        result = await handler.handle(
            {
                "session_id": "orch_xyz",
                "artifact": "code",
                "acceptance_criterion": "Should be ignored",
                "acceptance_criteria": ["Wins"],
            }
        )
        payload_context = result.value.meta["_subagent"]["context"]
        assert payload_context["acceptance_criterion"] == "Wins"

    @pytest.mark.asyncio
    async def test_plugin_payload_uses_brownfield_default_when_working_dir_omitted(
        self, handler, tmp_path: Path
    ) -> None:
        default_project = tmp_path / "project"
        default_project.mkdir()

        with patch(
            "ouroboros.mcp.tools.evaluation_handlers._default_brownfield_project_dir",
            new=AsyncMock(return_value=default_project),
        ):
            result = await handler.handle({"session_id": "orch_xyz", "artifact": "code"})

        assert result.is_ok
        payload_context = result.value.meta["_subagent"]["context"]
        assert payload_context["working_dir"] == str(default_project.resolve())

    @pytest.mark.asyncio
    async def test_auto_evolve_keeps_verdict_parent_owned_and_pollable(
        self,
        event_store,
        fake_inner_handler,
    ) -> None:
        manager = JobManager(event_store)
        handler = StartEvaluateHandler(
            evaluate_handler=fake_inner_handler,
            event_store=event_store,
            job_manager=manager,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )

        result = await handler.handle(
            {
                "session_id": "orch_converge",
                "artifact": "code",
                "auto_evolve": True,
            }
        )

        assert result.is_ok
        assert isinstance(result.value.meta["job_id"], str)
        assert "_subagent" not in result.value.meta
        for _ in range(200):
            snapshot = await manager.get_snapshot(result.value.meta["job_id"])
            if snapshot.is_terminal:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("parent-owned evaluation did not finish")
        forwarded = fake_inner_handler.handle.await_args.args[0]
        assert forwarded["_force_in_process"] is True
        assert snapshot.result_meta["final_approved"] is True


class TestFactoryWiring:
    def test_get_ouroboros_tools_includes_start_evaluate(self) -> None:
        from ouroboros.mcp.tools.definitions import get_ouroboros_tools

        tools = get_ouroboros_tools()
        names = {t.definition.name for t in tools}
        assert "ouroboros_start_evaluate" in names

    def test_start_evaluate_handler_factory_threads_kwargs(self) -> None:
        from ouroboros.mcp.tools.definitions import start_evaluate_handler

        h = start_evaluate_handler(
            runtime_backend="opencode",
            opencode_mode="plugin",
        )
        assert h.agent_runtime_backend == "opencode"
        assert h.opencode_mode == "plugin"
        # Inner EvaluateHandler also receives the wiring so plugin-gate stays
        # consistent if the start-handler ever delegates to it.
        assert h._evaluate_handler.agent_runtime_backend == "opencode"
        assert h._evaluate_handler.opencode_mode == "plugin"
