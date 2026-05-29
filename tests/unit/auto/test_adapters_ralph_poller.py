"""Focused tests for AutoPipeline Ralph handler adapters."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from ouroboros.auto import adapters
from ouroboros.auto.adapters import HandlerRalphPoller, HandlerRalphStarter
from ouroboros.core.types import Result
from ouroboros.mcp.job_manager import JobManager
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.persistence.event_store import EventStore


class _FakeJobManager:
    def __init__(self) -> None:
        self._event_store = object()


class _FakeRalphHandler:
    def __init__(self) -> None:
        self._job_manager = _FakeJobManager()


@pytest.mark.asyncio
async def test_handler_ralph_poller_propagates_terminal_generation_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal job metadata must restore auto state's Ralph generation on resume."""
    handler = _FakeRalphHandler()
    poller = HandlerRalphPoller(handler)  # type: ignore[arg-type]

    async def wait_for_terminal(_job_manager: Any, job_id: str, **_kwargs: Any) -> dict[str, Any]:
        assert job_id == "job_ralph_existing"
        return {
            "status": "completed",
            "stop_reason": "qa passed",
            "lineage_id": "lineage-1",
            "iterations": 7,
        }

    monkeypatch.setattr(adapters, "_wait_for_job_terminal", wait_for_terminal)

    result = await poller(job_id="job_ralph_existing")

    assert poller.job_event_store is handler._job_manager._event_store
    assert result == {
        "job_id": "job_ralph_existing",
        "lineage_id": "lineage-1",
        "dispatch_mode": "job",
        "terminal_status": "completed",
        "stop_reason": "qa passed",
        "current_generation": 7,
    }


@pytest.mark.asyncio
async def test_handler_ralph_poller_prefers_generations_over_iterations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume metadata must preserve lineage generation, not loop iteration count."""
    handler = _FakeRalphHandler()
    poller = HandlerRalphPoller(handler)  # type: ignore[arg-type]

    async def wait_for_terminal(_job_manager: Any, job_id: str, **_kwargs: Any) -> dict[str, Any]:
        assert job_id == "job_ralph_existing"
        return {
            "status": "completed",
            "stop_reason": "qa passed",
            "lineage_id": "lineage-1",
            "iterations": 2,
            "generations": [9, 10],
        }

    monkeypatch.setattr(adapters, "_wait_for_job_terminal", wait_for_terminal)

    result = await poller(job_id="job_ralph_existing")

    assert result["current_generation"] == 10


# ---------------------------------------------------------------------------
# Full Ralph job-manager adapter path: checkpoint metadata propagation
# (Q00/ouroboros#1281 review req_1780029496_276 BLOCKING).
#
# These exercise the *real* JobManager terminal-snapshot boundary — not a
# monkeypatched ``_wait_for_job_terminal`` and not ``RalphLoopRunner`` directly
# — so a regression that drops ``checkpoint_commits`` /
# ``checkpoint_attempted_ac_ids`` when the job-mode adapters rebuild their
# structured terminal dict is caught. Without forwarding, an in-process
# complete-product coding session can create AC git commits inside
# ``evolve_step`` while the auto state/result never persists or surfaces them,
# so a later resume re-attempts already-committed ACs.
# ---------------------------------------------------------------------------

_CHECKPOINT_COMMITS = [
    {"ac_id": "AC-1", "sha": "abc1230", "subject": "feat: satisfy AC-1"},
    {"ac_id": "AC-2", "sha": "def4560", "subject": "feat: satisfy AC-2"},
]
_CHECKPOINT_ATTEMPTS = ["AC-1", "AC-2", "AC-3"]


async def _cancel_manager_tasks(manager: JobManager) -> None:
    tasks = [
        *manager._tasks.values(),  # noqa: SLF001
        *manager._runner_tasks.values(),  # noqa: SLF001
        *manager._monitors.values(),  # noqa: SLF001
    ]
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _ralph_terminal_result(lineage_id: str) -> MCPToolResult:
    """Mirror the meta shape ``RalphLoopResult.to_tool_result`` surfaces."""
    return MCPToolResult(
        content=(MCPContentItem(type=ContentType.TEXT, text="ralph done"),),
        is_error=False,
        meta={
            "status": "completed",
            "stop_reason": "qa passed",
            "lineage_id": lineage_id,
            "iterations": 3,
            "checkpoint_commits": _CHECKPOINT_COMMITS,
            "checkpoint_attempted_ac_ids": _CHECKPOINT_ATTEMPTS,
        },
    )


@pytest.mark.asyncio
async def test_handler_ralph_poller_forwards_checkpoint_meta_via_real_job_manager(
    tmp_path,
) -> None:
    """Resume path must carry the durable commit list back into auto state."""
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
    manager = JobManager(store)
    try:

        async def _runner() -> MCPToolResult:
            return _ralph_terminal_result("lineage-ckpt")

        started = await manager.start_job(
            job_type="ralph", initial_message="queued", runner=_runner()
        )
        handler = SimpleNamespace(_job_manager=manager)
        poller = HandlerRalphPoller(handler)  # type: ignore[arg-type]

        result = await poller(job_id=started.job_id, max_total_seconds=5.0)

        assert result["terminal_status"] == "completed"
        assert result["checkpoint_commits"] == _CHECKPOINT_COMMITS
        assert result["checkpoint_attempted_ac_ids"] == _CHECKPOINT_ATTEMPTS
    finally:
        await _cancel_manager_tasks(manager)


@pytest.mark.asyncio
async def test_handler_ralph_starter_forwards_checkpoint_meta_via_real_job_manager(
    tmp_path,
) -> None:
    """Job-mode starter must carry checkpoint metadata back into auto state."""
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
    manager = JobManager(store)

    class _StarterHandler:
        # Absent runtime-backend attrs => job mode (not plugin dispatch).
        agent_runtime_backend = None
        opencode_mode = None

        def __init__(self, job_manager: JobManager) -> None:
            self._job_manager = job_manager

        async def handle(self, arguments: dict[str, Any]) -> Result[MCPToolResult, Any]:
            lineage_id = arguments.get("lineage_id", "lineage-ckpt")

            async def _runner() -> MCPToolResult:
                return _ralph_terminal_result(lineage_id)

            started = await self._job_manager.start_job(
                job_type="ralph", initial_message="queued", runner=_runner()
            )
            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="dispatched"),),
                    is_error=False,
                    meta={"dispatch_mode": "job", "job_id": started.job_id},
                )
            )

    try:
        handler = _StarterHandler(manager)
        starter = HandlerRalphStarter(handler)  # type: ignore[arg-type]
        # ``to_dict`` is the only Seed surface the job-mode starter touches.
        seed = SimpleNamespace(to_dict=lambda: {"goal": "x", "acceptance_criteria": ["a"]})

        result = await starter(
            seed,  # type: ignore[arg-type]
            lineage_id="lineage-ckpt",
            reuse_existing=False,
            max_total_seconds=5.0,
        )

        assert result["terminal_status"] == "completed"
        assert result["checkpoint_commits"] == _CHECKPOINT_COMMITS
        assert result["checkpoint_attempted_ac_ids"] == _CHECKPOINT_ATTEMPTS
    finally:
        await _cancel_manager_tasks(manager)
