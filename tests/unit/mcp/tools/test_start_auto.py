"""Tests for StartAutoHandler — fire-and-forget ``ooo auto`` wrapper.

Mirrors :mod:`test_start_evaluate`. The synchronous ``ouroboros_auto`` tool
routinely exceeds an MCP client's tool-call timeout because the Socratic
interview + repair loops + (optional) Ralph chain run end-to-end. The fire-
and-forget handler must return a ``job_id`` immediately and run the pipeline
under a :class:`JobManager`-backed background task.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import inspect
import json
import os
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.auto.pipeline import AutoPipelineResult
from ouroboros.auto.runtime_routing import AutoStageRuntimePlan, StageRuntime
from ouroboros.auto.state import (
    AutoCommitPolicy,
    AutoPipelineState,
    AutoStore,
    AutoWorktreePolicy,
)
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPToolError
from ouroboros.mcp.job_manager import JobManager, JobStatus
from ouroboros.mcp.tools.auto_handler import (
    _START_AUTO_PENDING_LEASE_SECONDS,
    AutoHandler,
    StartAutoHandler,
    _reconcile_execution_job_snapshot,
)
from ouroboros.mcp.tools.job_handlers import JobResultHandler, JobStatusHandler, JobWaitHandler
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.persistence.event_store import EventStore

_STRUCTURED_OBSERVATION_GOAL = """
Goal:
Verify current ooo auto can create hello_auto.py and tests/test_hello_auto.py.

Implementation:
- Create `hello_auto.py` at the repository root.
- Add a minimal pytest test at `tests/test_hello_auto.py`.

Outputs:
- `hello_auto.py` exists.
- `tests/test_hello_auto.py` exists.

Runtime context:
- This is a local development repository.
- Local file edits are allowed.
- Running targeted tests is allowed.
- Network access is not required.
- No credentials are required.

Actors:
- A single local developer/operator using Codex and Ouroboros in the local repository.

Inputs:
- The local repository state, the requested implementation contract, and the verification commands described in this goal prompt.

Non-goals:
- Do not refactor existing code.
- Do not add dependencies.
- Do not edit unrelated files.

Success criteria:
- `ooo auto` is handled by Ouroboros auto/MCP, not plain text.
- `hello_auto.py` exists.
- `tests/test_hello_auto.py` exists.
- The targeted test command `uv run pytest tests/test_hello_auto.py` passes.
- Final report includes auto session id, seed id, files changed, exact test command, and test result.

Important dispatch rule:
If `ouroboros_auto` is unavailable or interpreted as normal text, stop and report failure.
"""


_AUTO_ID_RE = re.compile(r"auto_[0-9a-f]+")
_UUID_HEX_RE = re.compile(r"\b[0-9a-f]{32}\b")


def _normalize_detached_auto_response(value):
    """Scrub generated handles while preserving rendered response structure."""
    if isinstance(value, dict):
        return {key: _normalize_detached_auto_response(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_normalize_detached_auto_response(item) for item in value]
    if isinstance(value, str):
        value = _AUTO_ID_RE.sub("auto_<id>", value)
        value = _UUID_HEX_RE.sub("<uuid>", value)
        return value
    return value


def _serializable_tool_result(result: MCPToolResult) -> dict[str, object]:
    return {
        "content": [
            {
                "type": item.type.value,
                "text": item.text,
                "data": item.data,
                "mime_type": item.mime_type,
                "uri": item.uri,
            }
            for item in result.content
        ],
        "is_error": result.is_error,
        "meta": result.meta,
    }


def _assert_detached_start_text_has_guidance_with_handles(
    result: MCPToolResult,
    *,
    job_id: str,
    auto_session_id: str,
) -> None:
    text = result.text_content
    assert text == (
        "Started background auto session.\n\n"
        "Status: queued\n"
        f"job_id: {job_id}\n"
        f"auto_session_id: {auto_session_id}\n"
        f"Runtime Backend: {result.meta['runtime_backend']}\n"
        f"LLM Backend: {result.meta['llm_backend']}\n"
        f"Efficiency Mode: {result.meta['efficiency_mode']}\n"
        f"Frugality Assurance: {result.meta['frugality_assurance']}\n\n"
        "The exact active model and first parallel level will be reported "
        "from run configuration and execution plan events.\n"
        "Track with ouroboros_job_wait / ouroboros_job_status until terminal, "
        "then fetch ouroboros_job_result."
    )
    assert "ouroboros_job_wait" in text
    assert "ouroboros_job_status" in text
    assert "ouroboros_job_result" in text
    # Handles must be visible in the text body so MCP clients that surface
    # only ``content`` (and not structured ``meta``) can still track the job.
    assert job_id in text
    assert auto_session_id in text


@pytest.mark.asyncio
async def test_reconcile_execution_job_does_not_complete_detached_ralph_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execution completion must not override active Ralph detached evidence."""

    class FakeJobManager:
        async def get_snapshot(self, job_id: str):  # pragma: no cover
            raise AssertionError(f"should not inspect execution job for {job_id}")

    monkeypatch.setattr(
        "ouroboros.mcp.tools.auto_handler.JobManager",
        lambda: FakeJobManager(),
    )
    result = AutoPipelineResult(
        status="detached",
        auto_session_id="auto_detached",
        phase="ralph_handoff",
        job_id="job_execution_done",
        execution_id="exec_done",
        run_session_id="orch_done",
        ralph_job_id="job_ralph_running",
        ralph_lineage_id="lin_ralph_running",
        ralph_dispatch_mode="job",
        execution_job_status=JobStatus.COMPLETED.value,
    )

    reconciled = await _reconcile_execution_job_snapshot(result)

    assert reconciled is result
    assert reconciled.status == "detached"
    assert reconciled.phase == "ralph_handoff"
    assert reconciled.ralph_job_id == "job_ralph_running"


@pytest.fixture
async def event_store():
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture
def fake_inner_auto():
    """An AutoHandler stub whose ``handle`` returns a canned ok result."""
    inner = MagicMock(spec=AutoHandler)
    inner.handle = AsyncMock(
        return_value=Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="ran"),),
                is_error=False,
                meta={"auto_session_id": "auto_xyz"},
            )
        )
    )
    return inner


class TestDefinition:
    def test_tool_name(self) -> None:
        assert StartAutoHandler().definition.name == "ouroboros_start_auto"

    def test_description_mentions_background(self) -> None:
        description = StartAutoHandler().definition.description.lower()
        assert "background" in description
        assert "auto_session_id + job_id immediately" in description

    def test_parameters_mirror_auto(self) -> None:
        h = StartAutoHandler()
        inner = AutoHandler()
        assert {p.name for p in h.definition.parameters} == {
            p.name for p in inner.definition.parameters
        }

    def test_user_preferences_schema_mentions_list_values(self) -> None:
        param = next(
            p for p in StartAutoHandler().definition.parameters if p.name == "user_preferences"
        )
        assert "non-empty lists of strings/numbers" in param.description


class TestRequiredArguments:
    @pytest.mark.asyncio
    async def test_missing_goal_and_resume_errors(self, event_store) -> None:
        h = StartAutoHandler(event_store=event_store)
        result = await h.handle({})
        assert result.is_err
        assert "goal" in result.error.message

    @pytest.mark.asyncio
    async def test_blank_goal_and_blank_resume_errors(self, event_store) -> None:
        h = StartAutoHandler(event_store=event_store)
        result = await h.handle({"goal": "   ", "resume": "   "})
        assert result.is_err

    @pytest.mark.asyncio
    async def test_missing_resume_session_errors_before_enqueue(
        self, event_store, tmp_path
    ) -> None:
        job_manager = MagicMock()
        job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
        job_manager.start_job = AsyncMock()
        h = StartAutoHandler(
            event_store=event_store,
            job_manager=job_manager,
            store=AutoStore(tmp_path),
        )

        result = await h.handle({"resume": "auto_missing123"})

        assert result.is_err
        assert "Auto session not found" in result.error.message
        job_manager.start_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_resume_argument_is_trimmed_for_enqueued_runner(
        self, event_store, tmp_path
    ) -> None:
        store = AutoStore(tmp_path)
        state = AutoPipelineState(goal="build a CLI", cwd=str(tmp_path))
        store.save(state)
        job_manager = MagicMock()
        job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
        snapshot = MagicMock()
        snapshot.job_id = "job_auto_resume"
        captured: dict[str, object] = {}

        async def _start_job(*, runner, **_):
            captured["runner"] = runner
            return snapshot

        job_manager.start_job = AsyncMock(side_effect=_start_job)
        h = StartAutoHandler(event_store=event_store, job_manager=job_manager, store=store)
        inner = MagicMock(spec=AutoHandler)
        inner.handle = AsyncMock(
            return_value=Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="ran"),),
                    is_error=False,
                    meta={"auto_session_id": state.auto_session_id},
                )
            )
        )
        h._inner_auto = inner

        result = await h.handle({"resume": f" {state.auto_session_id} "})

        assert result.is_ok
        await captured["runner"]
        inner.handle.assert_awaited_once()
        assert inner.handle.await_args.args[0]["resume"] == state.auto_session_id

    @pytest.mark.asyncio
    async def test_resume_ignores_blank_frontmatter_preference_placeholders(
        self, event_store, tmp_path
    ) -> None:
        store = AutoStore(tmp_path)
        state = AutoPipelineState(goal="build a CLI", cwd=str(tmp_path))
        state.efficiency_mode = "quality_first"
        state.frugality_assurance = "off"
        store.save(state)
        snapshot = MagicMock(job_id="job_auto_resume_blank")
        captured: dict[str, object] = {}

        async def _start_job(*, runner, **_):
            captured["runner"] = runner
            return snapshot

        job_manager = MagicMock()
        job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
        job_manager.start_job = AsyncMock(side_effect=_start_job)
        handler = StartAutoHandler(
            event_store=event_store,
            job_manager=job_manager,
            store=store,
        )
        inner = MagicMock(spec=AutoHandler)
        inner.handle = AsyncMock(
            return_value=Result.ok(MCPToolResult(meta={"auto_session_id": state.auto_session_id}))
        )
        handler._inner_auto = inner

        result = await handler.handle(
            {
                "resume": state.auto_session_id,
                "efficiency_mode": "",
                "frugality_assurance": "",
            }
        )

        assert result.is_ok
        assert result.value.meta["efficiency_mode"] == "quality_first"
        assert result.value.meta["frugality_assurance"] == "off"
        await captured["runner"]
        forwarded = inner.handle.await_args.args[0]
        assert "efficiency_mode" not in forwarded
        assert "frugality_assurance" not in forwarded


class TestBackgroundJobPath:
    @pytest.mark.asyncio
    async def test_returns_job_and_auto_session_id_immediately(
        self, event_store, fake_inner_auto, tmp_path
    ) -> None:
        job_manager = MagicMock()
        job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
        snapshot = MagicMock()
        snapshot.job_id = "job_auto_001"
        captured: dict[str, object] = {}

        async def _start_job(*, runner, **_):
            captured.update(_)
            if inspect.iscoroutine(runner):
                runner.close()
            return snapshot

        job_manager.start_job = AsyncMock(side_effect=_start_job)

        store = AutoStore(tmp_path)
        h = StartAutoHandler(event_store=event_store, job_manager=job_manager, store=store)
        # Inject the fake inner so we don't accidentally fire a real pipeline.
        h._inner_auto = fake_inner_auto

        result = await h.handle({"goal": "build a CLI"})
        assert result.is_ok
        auto_session_id = result.value.meta["auto_session_id"]
        assert isinstance(auto_session_id, str)
        assert auto_session_id.startswith("auto_")
        _assert_detached_start_text_has_guidance_with_handles(
            result.value,
            job_id="job_auto_001",
            auto_session_id=auto_session_id,
        )
        assert result.value.meta["job_id"] == "job_auto_001"
        assert result.value.meta["session_id"] == auto_session_id
        assert result.value.meta["dispatch_mode"] == "job"
        assert result.value.meta["runtime_backend"]
        assert result.value.meta["llm_backend"]
        assert result.value.meta["efficiency_mode"] == "adaptive"
        assert result.value.meta["frugality_assurance"] == "observe"
        assert result.value.meta["status_tool"] == "ouroboros_job_status"
        assert result.value.meta["wait_tool"] == "ouroboros_job_wait"
        assert result.value.meta["result_tool"] == "ouroboros_job_result"
        observer = result.value.meta["job_observer"]
        assert observer["recommended_host_action"] == "spawn_observer_session"
        assert observer["ownership"] == "exclusive"
        assert observer["job_id"] == "job_auto_001"
        assert observer["session_id"] == auto_session_id
        assert observer["main_session_policy"] == "start_and_on_demand_only"
        assert observer["follow_result_job_keys"] == [
            "job_id",
            "ralph_job_id",
            "chained_evaluate_job_id",
        ]
        assert captured["links"].session_id == auto_session_id
        assert store.path_for(auto_session_id).exists()
        # The inner AutoHandler must NOT have run synchronously — the runner is
        # enqueued on the JobManager only.
        fake_inner_auto.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_now_binds_durable_job_scoped_cancel_key(
        self, event_store, fake_inner_auto, tmp_path, monkeypatch
    ) -> None:
        """Behaviour fix: StartAuto must bind the durable ``mcp_job:{job_id}`` cancel key.

        Before the background-pipeline consolidation StartAuto wrapped its
        runner with ``lambda _handle: _runner()`` and passed neither
        ``process_id`` nor ``cancel_key``.  ``run_with_agent_process`` only
        builds the :class:`CheckpointStore` that loads a persisted cancel
        marker when ``cancel_key or process_id`` is set, so the durable
        ``mcp_job:{job_id}`` marker written by ``JobManager.cancel_job`` was
        never observable for auto jobs — a restart-visible cancel was silently
        dropped.  Routing through ``start_background_tool_job`` now binds it.
        """
        from ouroboros.mcp.tools import background

        captured_run: list[dict] = []

        def _fake_run_with_agent_process(**kwargs):
            captured_run.append(kwargs)

            async def _runner():
                return MCPToolResult()

            return _runner()

        monkeypatch.setattr(background, "run_with_agent_process", _fake_run_with_agent_process)

        job_manager = MagicMock()
        job_manager.allocate_job_id = AsyncMock(return_value="job_auto_cancel")
        snapshot = MagicMock()
        snapshot.job_id = "job_auto_cancel"

        async def _start_job(*, runner, **_):
            if inspect.iscoroutine(runner):
                runner.close()
            return snapshot

        job_manager.start_job = AsyncMock(side_effect=_start_job)

        store = AutoStore(tmp_path)
        h = StartAutoHandler(event_store=event_store, job_manager=job_manager, store=store)
        h._inner_auto = fake_inner_auto

        result = await h.handle({"goal": "build a CLI"})
        assert result.is_ok
        auto_session_id = result.value.meta["auto_session_id"]

        assert len(captured_run) == 1
        # The durable cancel key is now bound (previously absent), so the auto
        # agent process loads the persisted mcp_job:{job_id} marker on spawn.
        assert captured_run[0]["cancel_key"] == "mcp_job:job_auto_cancel"
        assert captured_run[0]["process_id"] == f"auto:{auto_session_id}:job_auto_cancel"
        assert captured_run[0]["cancel_key"] != captured_run[0]["process_id"]

    @pytest.mark.asyncio
    async def test_mcp_start_auto_detached_response_text_is_deterministic_for_identical_input(
        self, event_store, fake_inner_auto, tmp_path
    ) -> None:
        async def _invoke(store_path, job_id: str) -> MCPToolResult:
            job_manager = MagicMock()
            job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
            snapshot = MagicMock()
            snapshot.job_id = job_id

            async def _start_job(*, runner, **_):
                if inspect.iscoroutine(runner):
                    runner.close()
                return snapshot

            job_manager.start_job = AsyncMock(side_effect=_start_job)
            handler = StartAutoHandler(
                event_store=event_store,
                job_manager=job_manager,
                store=AutoStore(store_path),
            )
            handler._inner_auto = fake_inner_auto

            result = await handler.handle({"goal": "document detached auto polling"})
            assert result.is_ok
            return result.value

        first = await _invoke(tmp_path / "first", "job_auto_001")
        second = await _invoke(tmp_path / "second", "job_auto_002")

        # The text now embeds the (volatile) job_id/auto_session_id handles, so
        # determinism holds only after scrubbing them — identical inputs must
        # still render a byte-identical response skeleton.
        assert _normalize_detached_auto_response(
            first.text_content
        ) == _normalize_detached_auto_response(second.text_content)
        assert first.meta["job_id"] != second.meta["job_id"]
        assert first.meta["auto_session_id"] != second.meta["auto_session_id"]
        _assert_detached_start_text_has_guidance_with_handles(
            first,
            job_id=first.meta["job_id"],
            auto_session_id=first.meta["auto_session_id"],
        )
        _assert_detached_start_text_has_guidance_with_handles(
            second,
            job_id=second.meta["job_id"],
            auto_session_id=second.meta["auto_session_id"],
        )
        assert first.meta["status"] == second.meta["status"] == "queued"
        assert first.meta["dispatch_mode"] == second.meta["dispatch_mode"] == "job"
        assert first.meta["wait_tool"] == second.meta["wait_tool"] == "ouroboros_job_wait"
        fake_inner_auto.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_mcp_start_auto_response_job_handle_can_poll_and_fetch_result(
        self, event_store, tmp_path
    ) -> None:
        """Runnable API check for the detached start handle contract."""
        job_manager = JobManager(event_store)
        store = AutoStore(tmp_path)
        inner_started = asyncio.Event()
        release_inner = asyncio.Event()

        async def _pollable_auto(arguments):
            inner_started.set()
            await release_inner.wait()
            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="detached result"),),
                    is_error=False,
                    meta={
                        "auto_session_id": arguments["resume"],
                        "status": "completed",
                        "artifact": "detached-auto-result",
                    },
                )
            )

        inner = MagicMock(spec=AutoHandler)
        inner.handle = AsyncMock(side_effect=_pollable_auto)
        handler = StartAutoHandler(
            event_store=event_store,
            job_manager=job_manager,
            store=store,
        )
        handler._inner_auto = inner

        started = await handler.handle({"goal": "return a stable detached auto handle"})

        try:
            assert started.is_ok
            job_id = started.value.meta["job_id"]
            auto_session_id = started.value.meta["auto_session_id"]
            assert re.fullmatch(r"job_[0-9a-f]{12}", job_id)
            assert isinstance(auto_session_id, str)
            assert auto_session_id.startswith("auto_")
            meta = dict(started.value.meta)
            observer = meta.pop("job_observer")
            assert meta == {
                "job_id": job_id,
                "auto_session_id": auto_session_id,
                "session_id": auto_session_id,
                "status": "queued",
                "dispatch_mode": "job",
                "runtime_backend": "claude",
                "llm_backend": "claude_code",
                "efficiency_mode": "adaptive",
                "frugality_assurance": "observe",
                "status_tool": "ouroboros_job_status",
                "wait_tool": "ouroboros_job_wait",
                "result_tool": "ouroboros_job_result",
            }
            assert observer["job_id"] == job_id
            assert observer["session_id"] == auto_session_id
            assert observer["recommended_host_action"] == "spawn_observer_session"
            assert observer["ownership"] == "exclusive"

            await asyncio.wait_for(inner_started.wait(), timeout=1.0)

            status_handler = JobStatusHandler(event_store=event_store, job_manager=job_manager)
            status = await status_handler.handle({"job_id": job_id, "view": "summary"})

            assert status.is_ok
            assert status.value.meta["job_id"] == job_id
            assert status.value.meta["session_id"] == auto_session_id
            assert status.value.meta["status"] in {"queued", "running"}
            assert status.value.meta["is_terminal"] is False

            release_inner.set()
            wait_handler = JobWaitHandler(event_store=event_store, job_manager=job_manager)
            terminal = None
            for _ in range(50):
                waited = await wait_handler.handle(
                    {"job_id": job_id, "cursor": 0, "timeout_seconds": 0, "view": "summary"}
                )
                assert waited.is_ok
                if waited.value.meta["is_terminal"]:
                    terminal = waited.value
                    break
                await asyncio.sleep(0.01)

            assert terminal is not None
            assert terminal.meta["job_id"] == job_id
            assert terminal.meta["session_id"] == auto_session_id
            assert terminal.meta["status"] == "completed"

            result_handler = JobResultHandler(event_store=event_store, job_manager=job_manager)
            fetched = await result_handler.handle({"job_id": job_id})

            assert fetched.is_ok
            assert fetched.value.text_content == "detached result"
            assert fetched.value.meta["job_id"] == job_id
            assert fetched.value.meta["session_id"] == auto_session_id
            assert fetched.value.meta["auto_session_id"] == auto_session_id
            assert fetched.value.meta["status"] == "completed"
            assert fetched.value.meta["is_terminal"] is True
            assert fetched.value.meta["artifact"] == "detached-auto-result"
        finally:
            release_inner.set()

    @pytest.mark.asyncio
    async def test_mcp_start_auto_status_reports_running_detached_background_work(
        self, event_store, tmp_path
    ) -> None:
        job_manager = JobManager(event_store)
        store = AutoStore(tmp_path)
        inner_started = asyncio.Event()
        release_inner = asyncio.Event()

        async def _blocking_auto(_arguments):
            inner_started.set()
            await release_inner.wait()
            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="detached auto done"),),
                    is_error=False,
                    meta={"auto_session_id": "auto_running_status"},
                )
            )

        inner = MagicMock(spec=AutoHandler)
        inner.handle = AsyncMock(side_effect=_blocking_auto)
        handler = StartAutoHandler(
            event_store=event_store,
            job_manager=job_manager,
            store=store,
        )
        handler._inner_auto = inner

        started = await handler.handle({"goal": "keep detached auto observable"})

        try:
            assert started.is_ok
            job_id = started.value.meta["job_id"]
            auto_session_id = started.value.meta["auto_session_id"]
            await asyncio.wait_for(inner_started.wait(), timeout=1.0)

            deadline = asyncio.get_running_loop().time() + 1.0
            snapshot = await job_manager.get_snapshot(job_id)
            while snapshot.status is not JobStatus.RUNNING:
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError(
                        f"job {job_id} did not become running; last={snapshot.status}"
                    )
                await asyncio.sleep(0.01)
                snapshot = await job_manager.get_snapshot(job_id)

            status_handler = JobStatusHandler(event_store=event_store, job_manager=job_manager)
            status = await status_handler.handle({"job_id": job_id, "view": "full"})

            assert status.is_ok
            assert status.value.is_error is False
            assert status.value.meta["job_id"] == job_id
            assert status.value.meta["status"] == "running"
            assert status.value.meta["status"] not in {"completed", "failed", "cancelled"}
            assert status.value.meta["is_terminal"] is False
            assert status.value.meta["result_available"] is False
            assert status.value.meta["session_id"] == auto_session_id
            assert status.value.meta["view"] == "full"
            assert f"## Job: {job_id}" in status.value.text_content
            assert "**Type**: auto" in status.value.text_content
            assert "**Status**: running" in status.value.text_content
            assert "**Terminal**: false" in status.value.text_content
            assert "**Status**: completed" not in status.value.text_content
            assert "**Status**: failed" not in status.value.text_content
            assert "**Status**: cancelled" not in status.value.text_content
            assert (
                "**Tracking**: detached auto tracked background work" in status.value.text_content
            )
            assert f"**Session ID**: {auto_session_id}" in status.value.text_content
            assert "Use `ouroboros_job_result` to fetch the full terminal output." not in (
                status.value.text_content
            )
        finally:
            release_inner.set()
            if started.is_ok:
                job_id = started.value.meta["job_id"]
                deadline = asyncio.get_running_loop().time() + 1.0
                snapshot = await job_manager.get_snapshot(job_id)
                while snapshot.status is not JobStatus.COMPLETED:
                    if asyncio.get_running_loop().time() >= deadline:
                        break
                    await asyncio.sleep(0.01)
                    snapshot = await job_manager.get_snapshot(job_id)

    @pytest.mark.asyncio
    async def test_mcp_start_auto_wait_reports_stable_running_detached_output(
        self, event_store, tmp_path
    ) -> None:
        """Runnable API check for observable wait output while auto is still running."""
        job_manager = JobManager(event_store)
        store = AutoStore(tmp_path)
        inner_started = asyncio.Event()
        release_inner = asyncio.Event()

        async def _blocking_auto(_arguments):
            inner_started.set()
            await release_inner.wait()
            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="detached auto done"),),
                    is_error=False,
                    meta={"auto_session_id": "auto_running_wait"},
                )
            )

        inner = MagicMock(spec=AutoHandler)
        inner.handle = AsyncMock(side_effect=_blocking_auto)
        handler = StartAutoHandler(
            event_store=event_store,
            job_manager=job_manager,
            store=store,
        )
        handler._inner_auto = inner

        started = await handler.handle({"goal": "keep detached auto wait observable"})

        try:
            assert started.is_ok
            job_id = started.value.meta["job_id"]
            auto_session_id = started.value.meta["auto_session_id"]
            await asyncio.wait_for(inner_started.wait(), timeout=1.0)

            deadline = asyncio.get_running_loop().time() + 1.0
            snapshot = await job_manager.get_snapshot(job_id)
            while snapshot.status is not JobStatus.RUNNING:
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError(
                        f"job {job_id} did not become running; last={snapshot.status}"
                    )
                await asyncio.sleep(0.01)
                snapshot = await job_manager.get_snapshot(job_id)

            wait_handler = JobWaitHandler(event_store=event_store, job_manager=job_manager)
            arguments = {
                "job_id": job_id,
                "cursor": snapshot.cursor,
                "timeout_seconds": 0,
                "view": "full",
            }

            first = await wait_handler.handle(arguments)
            second = await wait_handler.handle(arguments)

            assert first.is_ok
            assert second.is_ok
            assert first.value.text_content == second.value.text_content
            assert first.value.meta == second.value.meta
            assert first.value.meta["job_id"] == job_id
            assert first.value.meta["status"] == "running"
            assert first.value.meta["is_terminal"] is False
            assert first.value.meta["changed"] is False
            assert first.value.meta["session_id"] == auto_session_id
            assert f"## Job: {job_id}" in first.value.text_content
            assert "**Type**: auto" in first.value.text_content
            assert "**Status**: running" in first.value.text_content
            assert "**Terminal**: false" in first.value.text_content
            assert "**Tracking**: detached auto tracked background work" in first.value.text_content
            assert f"**Session ID**: {auto_session_id}" in first.value.text_content
            assert "No new job-level events during this wait window." in first.value.text_content
            assert "Use `ouroboros_job_result` to fetch the full terminal output." not in (
                first.value.text_content
            )
        finally:
            release_inner.set()
            if started.is_ok:
                deadline = asyncio.get_running_loop().time() + 1.0
                snapshot = await job_manager.get_snapshot(started.value.meta["job_id"])
                while snapshot.status is not JobStatus.COMPLETED:
                    if asyncio.get_running_loop().time() >= deadline:
                        break
                    await asyncio.sleep(0.01)
                    snapshot = await job_manager.get_snapshot(started.value.meta["job_id"])

    @pytest.mark.asyncio
    async def test_mcp_start_auto_wait_then_result_returns_completed_artifact(
        self, event_store, tmp_path
    ) -> None:
        job_manager = JobManager(event_store)
        store = AutoStore(tmp_path)
        inner = MagicMock(spec=AutoHandler)
        inner.handle = AsyncMock(
            return_value=Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(type=ContentType.TEXT, text="detached auto finished"),
                        MCPContentItem(
                            type=ContentType.RESOURCE,
                            uri="file:///tmp/detached-auto-result.json",
                        ),
                    ),
                    is_error=False,
                    meta={
                        "auto_session_id": "auto_completed_result",
                        "status": "completed",
                        "result": {
                            "artifact": "detached-auto-result.json",
                            "ok": True,
                        },
                    },
                )
            )
        )
        handler = StartAutoHandler(
            event_store=event_store,
            job_manager=job_manager,
            store=store,
        )
        handler._inner_auto = inner

        started = await handler.handle({"goal": "produce a detached auto artifact"})

        assert started.is_ok
        job_id = started.value.meta["job_id"]
        auto_session_id = started.value.meta["auto_session_id"]
        wait_handler = JobWaitHandler(event_store=event_store, job_manager=job_manager)
        result_handler = JobResultHandler(event_store=event_store, job_manager=job_manager)

        terminal_wait = None
        cursor = 0
        # Advance the cursor and use a blocking per-wait timeout under a generous
        # wall-clock deadline so the background runner reaches terminal even on a
        # slow/loaded CI runner. A fixed tiny busy-poll budget races the dispatch.
        deadline = asyncio.get_running_loop().time() + 10.0
        while asyncio.get_running_loop().time() < deadline:
            wait_result = await wait_handler.handle(
                {"job_id": job_id, "cursor": cursor, "timeout_seconds": 2, "view": "summary"}
            )
            assert wait_result.is_ok
            cursor = wait_result.value.meta["cursor"]
            if wait_result.value.meta["is_terminal"]:
                terminal_wait = wait_result.value
                break

        assert terminal_wait is not None
        assert terminal_wait.meta["job_id"] == job_id
        assert terminal_wait.meta["status"] == "completed"
        assert terminal_wait.meta["session_id"] == auto_session_id

        completed = await result_handler.handle({"job_id": job_id})

        assert completed.is_ok
        assert completed.value.is_error is False
        assert completed.value.content == (
            MCPContentItem(type=ContentType.TEXT, text="detached auto finished"),
            MCPContentItem(
                type=ContentType.RESOURCE,
                uri="file:///tmp/detached-auto-result.json",
            ),
        )
        assert completed.value.meta["job_id"] == job_id
        assert completed.value.meta["status"] == "completed"
        assert completed.value.meta["is_terminal"] is True
        assert completed.value.meta["session_id"] == auto_session_id
        assert completed.value.meta["auto_session_id"] == "auto_completed_result"
        assert completed.value.meta["result"] == {
            "artifact": "detached-auto-result.json",
            "ok": True,
        }
        assert completed.value.meta["result_payload"] == {
            "content": [
                {
                    "type": "text",
                    "text": "detached auto finished",
                    "data": None,
                    "mime_type": None,
                    "uri": None,
                },
                {
                    "type": "resource",
                    "text": None,
                    "data": None,
                    "mime_type": None,
                    "uri": "file:///tmp/detached-auto-result.json",
                },
            ],
            "is_error": False,
            "meta": {
                "auto_session_id": "auto_completed_result",
                "status": "completed",
                "result": {
                    "artifact": "detached-auto-result.json",
                    "ok": True,
                },
            },
            "text_content": "detached auto finished",
        }
        assert completed.value.text_content == "detached auto finished"

        repeated = await result_handler.handle({"job_id": job_id})

        assert repeated.is_ok
        assert repeated.value.content == completed.value.content
        assert repeated.value.meta == completed.value.meta
        assert repeated.value.text_content == completed.value.text_content
        inner.handle.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mcp_detached_auto_failed_result_returns_stable_error_output(
        self, event_store, tmp_path
    ) -> None:
        """Runnable API check for failed detached auto result retrieval."""
        job_manager = JobManager(event_store)
        store = AutoStore(tmp_path)
        inner = MagicMock(spec=AutoHandler)
        inner.handle = AsyncMock(
            return_value=Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text="detached auto failed: seed repair did not converge",
                        ),
                    ),
                    is_error=True,
                    meta={
                        "auto_session_id": "auto_failed_result",
                        "status": "failed",
                        "error": {
                            "code": "seed_repair_exhausted",
                            "message": "Seed repair did not converge",
                        },
                    },
                )
            )
        )
        handler = StartAutoHandler(
            event_store=event_store,
            job_manager=job_manager,
            store=store,
        )
        handler._inner_auto = inner

        started = await handler.handle({"goal": "surface detached auto failure"})

        assert started.is_ok
        job_id = started.value.meta["job_id"]
        auto_session_id = started.value.meta["auto_session_id"]
        wait_handler = JobWaitHandler(event_store=event_store, job_manager=job_manager)
        result_handler = JobResultHandler(event_store=event_store, job_manager=job_manager)

        terminal_wait = None
        cursor = 0
        # Advance the cursor and use a blocking per-wait timeout under a generous
        # wall-clock deadline so the background runner reaches terminal even on a
        # slow/loaded CI runner. A fixed tiny busy-poll budget races the dispatch.
        deadline = asyncio.get_running_loop().time() + 10.0
        while asyncio.get_running_loop().time() < deadline:
            wait_result = await wait_handler.handle(
                {"job_id": job_id, "cursor": cursor, "timeout_seconds": 2, "view": "summary"}
            )
            assert wait_result.is_ok
            cursor = wait_result.value.meta["cursor"]
            if wait_result.value.meta["is_terminal"]:
                terminal_wait = wait_result.value
                break

        assert terminal_wait is not None
        assert terminal_wait.is_error is True
        assert terminal_wait.meta["job_id"] == job_id
        assert terminal_wait.meta["session_id"] == auto_session_id
        assert terminal_wait.meta["status"] == "failed"
        assert terminal_wait.meta["lifecycle_status"] == "failed"
        assert terminal_wait.meta["status_category"] == "terminal"
        assert terminal_wait.meta["is_terminal"] is True
        assert terminal_wait.meta["result_available"] is True

        first = await result_handler.handle({"job_id": job_id})
        second = await result_handler.handle({"job_id": job_id})

        assert first.is_ok
        assert second.is_ok
        assert first.value.is_error is True
        assert second.value.is_error is True
        assert _serializable_tool_result(first.value) == _serializable_tool_result(second.value)
        assert first.value.content == (
            MCPContentItem(
                type=ContentType.TEXT,
                text="detached auto failed: seed repair did not converge",
            ),
        )
        assert first.value.text_content == "detached auto failed: seed repair did not converge"
        assert first.value.meta["job_id"] == job_id
        assert first.value.meta["session_id"] == auto_session_id
        assert first.value.meta["auto_session_id"] == "auto_failed_result"
        assert first.value.meta["status"] == "failed"
        assert first.value.meta["lifecycle_status"] == "failed"
        assert first.value.meta["status_category"] == "terminal"
        assert first.value.meta["is_terminal"] is True
        assert first.value.meta["result_available"] is True
        assert first.value.meta["error"] == {
            "code": "seed_repair_exhausted",
            "message": "Seed repair did not converge",
        }
        assert first.value.meta["result_payload"] == {
            "content": [
                {
                    "type": "text",
                    "text": "detached auto failed: seed repair did not converge",
                    "data": None,
                    "mime_type": None,
                    "uri": None,
                },
            ],
            "is_error": True,
            "meta": {
                "auto_session_id": "auto_failed_result",
                "status": "failed",
                "error": {
                    "code": "seed_repair_exhausted",
                    "message": "Seed repair did not converge",
                },
            },
            "text_content": "detached auto failed: seed repair did not converge",
        }
        inner.handle.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mcp_detached_auto_interrupted_result_surfaces_as_error(
        self, event_store, tmp_path
    ) -> None:
        """Interrupted is terminal-and-error; result/status must not report success."""
        job_manager = JobManager(event_store)
        store = AutoStore(tmp_path)
        inner = MagicMock(spec=AutoHandler)
        inner.handle = AsyncMock(
            return_value=Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text="detached auto interrupted: budget exhausted mid-run",
                        ),
                    ),
                    is_error=True,
                    meta={
                        "auto_session_id": "auto_interrupted_result",
                        "status": "interrupted",
                        "error": {
                            "code": "wall_clock_exhausted",
                            "message": "Interrupted before terminal verdict",
                        },
                    },
                )
            )
        )
        handler = StartAutoHandler(
            event_store=event_store,
            job_manager=job_manager,
            store=store,
        )
        handler._inner_auto = inner

        started = await handler.handle({"goal": "surface detached auto interruption"})

        assert started.is_ok
        job_id = started.value.meta["job_id"]
        wait_handler = JobWaitHandler(event_store=event_store, job_manager=job_manager)
        status_handler = JobStatusHandler(event_store=event_store, job_manager=job_manager)
        result_handler = JobResultHandler(event_store=event_store, job_manager=job_manager)

        terminal_wait = None
        cursor = 0
        deadline = asyncio.get_running_loop().time() + 10.0
        while asyncio.get_running_loop().time() < deadline:
            wait_result = await wait_handler.handle(
                {"job_id": job_id, "cursor": cursor, "timeout_seconds": 2, "view": "summary"}
            )
            assert wait_result.is_ok
            cursor = wait_result.value.meta["cursor"]
            if wait_result.value.meta["is_terminal"]:
                terminal_wait = wait_result.value
                break

        assert terminal_wait is not None
        # The regression: an interrupted terminal job must not be reported as success.
        assert terminal_wait.is_error is True
        assert terminal_wait.meta["status"] == "interrupted"
        assert terminal_wait.meta["lifecycle_status"] == "interrupted"
        assert terminal_wait.meta["status_category"] == "terminal"

        status = await status_handler.handle({"job_id": job_id})
        assert status.is_ok
        assert status.value.is_error is True
        assert status.value.meta["status"] == "interrupted"

        result = await result_handler.handle({"job_id": job_id})
        assert result.is_ok
        assert result.value.is_error is True
        assert result.value.meta["status"] == "interrupted"
        assert result.value.meta["is_terminal"] is True
        assert result.value.meta["result_payload"]["is_error"] is True
        inner.handle.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mcp_detached_auto_result_invalid_handle_returns_stable_failure(
        self, event_store
    ) -> None:
        job_id = "job_missing_detached_auto"
        handler = JobResultHandler(event_store=event_store)

        first = await handler.handle({"job_id": job_id})
        second = await handler.handle({"job_id": job_id})

        assert first.is_err
        assert second.is_err
        assert first.error.tool_name == "ouroboros_job_result"
        assert first.error.error_code == "job_handle_not_found"
        assert first.error.message == f"Job handle not found: {job_id}. Result unavailable."
        assert first.error.details == {
            "job_id": job_id,
            "lifecycle_status": "invalid",
            "is_terminal": True,
            "result_available": False,
            "reason": "not_found",
            "source_error": f"Job not found: {job_id}",
        }
        assert second.error.tool_name == first.error.tool_name
        assert second.error.error_code == first.error.error_code
        assert second.error.message == first.error.message
        assert second.error.details == first.error.details

    @pytest.mark.asyncio
    async def test_new_structured_goal_preallocates_seed_ready_ledger(
        self, event_store, tmp_path, fake_inner_auto
    ) -> None:
        job_manager = MagicMock()
        job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
        snapshot = MagicMock()
        snapshot.job_id = "job_auto_structured"

        async def _start_job(*, runner, **_):
            if inspect.iscoroutine(runner):
                runner.close()
            return snapshot

        job_manager.start_job = AsyncMock(side_effect=_start_job)
        store = AutoStore(tmp_path)
        h = StartAutoHandler(event_store=event_store, job_manager=job_manager, store=store)
        h._inner_auto = fake_inner_auto

        result = await h.handle({"goal": _STRUCTURED_OBSERVATION_GOAL, "cwd": str(tmp_path)})

        assert result.is_ok
        state = store.load(result.value.meta["auto_session_id"])
        assert "runtime_context" in state.user_preferences
        assert "non_goals" in state.user_preferences
        assert "failure_modes" in state.user_preferences
        assert SeedDraftLedger.from_dict(state.ledger).open_gaps() == []
        fake_inner_auto.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_new_coding_goal_preallocates_coding_policy_defaults(
        self, event_store, tmp_path, fake_inner_auto
    ) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
        job_manager = MagicMock()
        job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
        snapshot = MagicMock()
        snapshot.job_id = "job_auto_coding_defaults"

        async def _start_job(*, runner, **_):
            if inspect.iscoroutine(runner):
                runner.close()
            return snapshot

        job_manager.start_job = AsyncMock(side_effect=_start_job)
        store = AutoStore(tmp_path / "store")
        h = StartAutoHandler(event_store=event_store, job_manager=job_manager, store=store)
        h._inner_auto = fake_inner_auto

        result = await h.handle({"goal": "build a CLI", "cwd": str(tmp_path)})

        assert result.is_ok
        state = store.load(result.value.meta["auto_session_id"])
        assert state.active_domain_profile_name == "coding"
        assert state.commit_policy is AutoCommitPolicy.AC_CHECKPOINT
        assert state.worktree_policy is AutoWorktreePolicy.AUTO

    @pytest.mark.asyncio
    async def test_new_auto_policy_args_override_profile_defaults(
        self, event_store, tmp_path, fake_inner_auto
    ) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
        job_manager = MagicMock()
        job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
        snapshot = MagicMock()
        snapshot.job_id = "job_auto_policy_override"

        async def _start_job(*, runner, **_):
            if inspect.iscoroutine(runner):
                runner.close()
            return snapshot

        job_manager.start_job = AsyncMock(side_effect=_start_job)
        store = AutoStore(tmp_path / "store")
        h = StartAutoHandler(event_store=event_store, job_manager=job_manager, store=store)
        h._inner_auto = fake_inner_auto

        result = await h.handle(
            {
                "goal": "build a CLI",
                "cwd": str(tmp_path),
                "commit_policy": "none",
                "worktree_policy": "current",
            }
        )

        assert result.is_ok
        state = store.load(result.value.meta["auto_session_id"])
        assert state.active_domain_profile_name == "coding"
        assert state.commit_policy is AutoCommitPolicy.NONE
        assert state.worktree_policy is AutoWorktreePolicy.CURRENT

    @pytest.mark.asyncio
    async def test_new_auto_policy_args_accept_worktree_aliases(
        self, event_store, tmp_path, fake_inner_auto
    ) -> None:
        job_manager = MagicMock()
        job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
        snapshot = MagicMock()
        snapshot.job_id = "job_auto_policy_alias"

        async def _start_job(*, runner, **_):
            if inspect.iscoroutine(runner):
                runner.close()
            return snapshot

        job_manager.start_job = AsyncMock(side_effect=_start_job)
        store = AutoStore(tmp_path / "store")
        h = StartAutoHandler(event_store=event_store, job_manager=job_manager, store=store)
        h._inner_auto = fake_inner_auto

        result = await h.handle(
            {
                "goal": "build a CLI",
                "cwd": str(tmp_path),
                "worktree_policy": "create_isolated_worktree",
            }
        )

        assert result.is_ok
        state = store.load(result.value.meta["auto_session_id"])
        assert state.worktree_policy is AutoWorktreePolicy.ALWAYS

    @pytest.mark.asyncio
    async def test_complete_product_with_short_timeout_fails_fast(
        self, event_store, tmp_path, fake_inner_auto
    ) -> None:
        job_manager = MagicMock()
        job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
        store = AutoStore(tmp_path / "store")
        h = StartAutoHandler(event_store=event_store, job_manager=job_manager, store=store)
        h._inner_auto = fake_inner_auto

        result = await h.handle(
            {
                "goal": "build a product",
                "cwd": str(tmp_path),
                "complete_product": True,
                "pipeline_timeout_seconds": 100,
            }
        )

        assert result.is_err
        assert "complete_product=true requires pipeline_timeout_seconds >= 1800" in str(
            result.error
        )
        job_manager.start_job.assert_not_called()
        fake_inner_auto.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_fresh_structured_goal_runner_resumes_without_preference_override(
        self, event_store, tmp_path
    ) -> None:
        store = AutoStore(tmp_path)
        job_manager = MagicMock()
        job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
        snapshot = MagicMock()
        snapshot.job_id = "job_auto_structured_runner"
        captured: dict[str, object] = {}

        async def _start_job(*, runner, **_):
            captured["runner"] = runner
            return snapshot

        job_manager.start_job = AsyncMock(side_effect=_start_job)
        h = StartAutoHandler(event_store=event_store, job_manager=job_manager, store=store)
        inner = MagicMock(spec=AutoHandler)
        inner.handle = AsyncMock(
            return_value=Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="ran"),),
                    is_error=False,
                    meta={"auto_session_id": "auto_structured"},
                )
            )
        )
        h._inner_auto = inner

        result = await h.handle(
            {
                "goal": _STRUCTURED_OBSERVATION_GOAL,
                "cwd": str(tmp_path),
                "user_preferences": {"constraints": "Keep changes local and reversible."},
            }
        )

        assert result.is_ok
        auto_session_id = result.value.meta["auto_session_id"]
        state = store.load(auto_session_id)
        assert "runtime_context" in state.user_preferences
        assert state.user_preferences["constraints"] == "Keep changes local and reversible."

        await captured["runner"]
        inner.handle.assert_awaited_once()
        runner_args = inner.handle.await_args.args[0]
        assert runner_args["resume"] == auto_session_id
        assert "user_preferences" not in runner_args

    @pytest.mark.asyncio
    async def test_mcp_plugin_mode_wires_seed_qa_with_demoted_authoring_handler(
        self, event_store, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Plugin auto must not bypass the pre-run Seed QA gate."""

        captured: dict[str, object] = {}

        class FakeQAHandler:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                captured["qa_handler"] = self

        class FakeAutoPipeline:
            def __init__(self, *_args, **kwargs):
                captured["pipeline_kwargs"] = kwargs

            async def run(self, state):
                captured["state"] = state
                return AutoPipelineResult(
                    status="blocked",
                    auto_session_id=state.auto_session_id,
                    phase=str(state.phase.value),
                )

        monkeypatch.setattr("ouroboros.mcp.tools.auto_handler.QAHandler", FakeQAHandler)
        monkeypatch.setattr("ouroboros.mcp.tools.auto_handler.AutoPipeline", FakeAutoPipeline)

        h = AutoHandler(
            store=AutoStore(tmp_path),
            event_store=event_store,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )

        result = await h._run({"goal": "build a CLI"})

        assert result.status == "blocked"
        qa_handler = captured["qa_handler"]
        assert qa_handler.kwargs["agent_runtime_backend"] == "opencode"
        assert qa_handler.kwargs["opencode_mode"] == "subprocess"
        kwargs = captured["pipeline_kwargs"]
        assert kwargs["seed_qa_evaluator"] is not None
        assert kwargs["seed_qa_evaluator"].qa_handler is qa_handler
        assert kwargs["evaluator"] is None

    @pytest.mark.asyncio
    async def test_plugin_mode_returns_subagent_without_enqueue(
        self, event_store, tmp_path, fake_inner_auto
    ) -> None:
        job_manager = MagicMock()
        job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
        job_manager.start_job = AsyncMock()
        store = AutoStore(tmp_path)
        h = StartAutoHandler(
            event_store=event_store,
            job_manager=job_manager,
            store=store,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )
        h._inner_auto = fake_inner_auto

        result = await h.handle({"goal": "build a CLI"})

        assert result.is_ok
        meta = result.value.meta
        assert meta["job_id"] is None
        assert meta["status"] == "delegated_to_plugin"
        assert meta["dispatch_mode"] == "plugin"
        assert meta["runtime_backend"] == "opencode"
        assert meta["llm_backend"]
        assert meta["efficiency_mode"] == "adaptive"
        assert meta["frugality_assurance"] == "observe"
        assert isinstance(meta["auto_session_id"], str)
        assert store.path_for(meta["auto_session_id"]).exists()
        assert meta["_subagent"]["tool_name"] == "ouroboros_start_auto"
        assert meta["_subagent"]["context"]["arguments"]["resume"] == meta["auto_session_id"]
        assert isinstance(meta["_subagent"]["context"]["arguments"]["_start_auto_lease_token"], str)
        body = json.loads(result.value.content[0].text)
        assert body["auto_session_id"] == meta["auto_session_id"]
        assert body["runtime_backend"] == "opencode"
        assert body["llm_backend"] == meta["llm_backend"]
        assert body["efficiency_mode"] == "adaptive"
        assert body["frugality_assurance"] == "observe"
        job_manager.start_job.assert_not_called()
        fake_inner_auto.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_stage_routed_interview_bypasses_opencode_plugin_dispatch(
        self, event_store, tmp_path, fake_inner_auto, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-OpenCode interview stages should run as normal jobs."""

        from ouroboros.mcp.tools import auto_handler as auto_module

        monkeypatch.setattr(
            auto_module,
            "resolve_auto_stage_runtime_plan",
            lambda **_kwargs: AutoStageRuntimePlan(
                default=StageRuntime("opencode", "plugin"),
                interview=StageRuntime("gjc", None),
                execute=StageRuntime("pi", None),
                evaluate=StageRuntime("opencode", "plugin"),
                reflect=StageRuntime("opencode", "plugin"),
            ),
        )
        job_manager = MagicMock()
        job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
        snapshot = MagicMock()
        snapshot.job_id = "job_auto_stage_routed"

        async def _start_job(*, runner, **_):
            if inspect.iscoroutine(runner):
                runner.close()
            return snapshot

        job_manager.start_job = AsyncMock(side_effect=_start_job)
        store = AutoStore(tmp_path)
        h = StartAutoHandler(
            event_store=event_store,
            job_manager=job_manager,
            store=store,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )
        h._inner_auto = fake_inner_auto

        result = await h.handle({"goal": "build a CLI"})

        assert result.is_ok
        assert result.value.meta["dispatch_mode"] == "job"
        assert result.value.meta["job_id"] == "job_auto_stage_routed"
        job_manager.start_job.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stage_routed_execute_plugin_dispatches_before_job_enqueue(
        self, event_store, tmp_path, fake_inner_auto, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Execute-stage plugin dispatch must be visible to the bridge."""

        from ouroboros.mcp.tools import auto_handler as auto_module

        monkeypatch.setattr(
            auto_module,
            "resolve_auto_stage_runtime_plan",
            lambda **_kwargs: AutoStageRuntimePlan(
                default=StageRuntime("opencode", "plugin"),
                interview=StageRuntime("gjc", None),
                execute=StageRuntime("opencode", "plugin"),
                evaluate=StageRuntime("opencode", "plugin"),
                reflect=StageRuntime("opencode", "plugin"),
            ),
        )
        job_manager = MagicMock()
        job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
        job_manager.start_job = AsyncMock()
        store = AutoStore(tmp_path)
        h = StartAutoHandler(
            event_store=event_store,
            job_manager=job_manager,
            store=store,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )
        h._inner_auto = fake_inner_auto

        result = await h.handle({"goal": "build a CLI"})

        assert result.is_ok
        meta = result.value.meta
        assert meta["job_id"] is None
        assert meta["dispatch_mode"] == "plugin"
        assert meta["status"] == "delegated_to_plugin"
        assert meta["_subagent"]["context"]["arguments"]["resume"] == meta["auto_session_id"]
        job_manager.start_job.assert_not_called()
        fake_inner_auto.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_plugin_detached_auto_response_is_deterministic_after_scrubbing_handles(
        self, event_store, tmp_path, fake_inner_auto
    ) -> None:
        async def _invoke(store_path):
            job_manager = MagicMock()
            job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
            job_manager.start_job = AsyncMock()
            handler = StartAutoHandler(
                event_store=event_store,
                job_manager=job_manager,
                store=AutoStore(store_path),
                agent_runtime_backend="opencode",
                opencode_mode="plugin",
            )
            handler._inner_auto = fake_inner_auto

            result = await handler.handle(
                {
                    "goal": "document detached auto polling",
                    "skip_run": True,
                    "user_preferences": {
                        "constraints": "Keep the UX contract stable.",
                        "non_goals": ["No nested auto execution."],
                    },
                }
            )

            assert result.is_ok
            body = json.loads(result.value.text_content)
            assert result.value.text_content == json.dumps(
                body,
                sort_keys=True,
                separators=(",", ":"),
            )
            job_manager.start_job.assert_not_called()
            return _normalize_detached_auto_response(_serializable_tool_result(result.value))

        first = await _invoke(tmp_path / "first")
        second = await _invoke(tmp_path / "second")

        assert first == second
        fake_inner_auto.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_resume_uses_persisted_plugin_runtime_for_dispatch(
        self, event_store, tmp_path, fake_inner_auto
    ) -> None:
        store = AutoStore(tmp_path)
        state = AutoPipelineState(goal="build a CLI", cwd=str(tmp_path))
        state.runtime_backend = "opencode"
        state.opencode_mode = "plugin"
        state.efficiency_mode = "quality_first"
        state.frugality_assurance = "off"
        store.save(state)
        job_manager = MagicMock()
        job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
        job_manager.start_job = AsyncMock()
        h = StartAutoHandler(event_store=event_store, job_manager=job_manager, store=store)
        h._inner_auto = fake_inner_auto

        result = await h.handle({"resume": state.auto_session_id})

        assert result.is_ok
        assert result.value.meta["dispatch_mode"] == "plugin"
        assert result.value.meta["job_id"] is None
        assert result.value.meta["runtime_backend"] == "opencode"
        assert result.value.meta["efficiency_mode"] == "quality_first"
        assert result.value.meta["frugality_assurance"] == "off"
        job_manager.start_job.assert_not_called()
        fake_inner_auto.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_resume_uses_persisted_subprocess_runtime_for_dispatch(
        self, event_store, tmp_path, fake_inner_auto
    ) -> None:
        store = AutoStore(tmp_path)
        state = AutoPipelineState(goal="build a CLI", cwd=str(tmp_path))
        state.runtime_backend = "opencode"
        state.opencode_mode = "subprocess"
        state.efficiency_mode = "quality_first"
        state.frugality_assurance = "off"
        store.save(state)
        snapshot = MagicMock()
        snapshot.job_id = "job_subprocess_resume"

        async def _start_job(*, runner, **_):
            if inspect.iscoroutine(runner):
                runner.close()
            return snapshot

        job_manager = MagicMock()
        job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
        job_manager.start_job = AsyncMock(side_effect=_start_job)
        h = StartAutoHandler(
            event_store=event_store,
            job_manager=job_manager,
            store=store,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )
        h._inner_auto = fake_inner_auto

        result = await h.handle({"resume": state.auto_session_id})

        assert result.is_ok
        assert result.value.meta["dispatch_mode"] == "job"
        assert result.value.meta["job_id"] == "job_subprocess_resume"
        assert result.value.meta["runtime_backend"] == "opencode"
        assert result.value.meta["efficiency_mode"] == "quality_first"
        assert result.value.meta["frugality_assurance"] == "off"
        job_manager.start_job.assert_awaited_once()
        fake_inner_auto.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_enqueue_failure_returns_persisted_auto_session_id(
        self, event_store, tmp_path, fake_inner_auto
    ) -> None:
        job_manager = MagicMock()
        job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
        job_manager.start_job = AsyncMock(side_effect=RuntimeError("queue unavailable"))
        store = AutoStore(tmp_path)
        h = StartAutoHandler(event_store=event_store, job_manager=job_manager, store=store)
        h._inner_auto = fake_inner_auto

        result = await h.handle({"goal": "build a CLI"})

        assert result.is_err
        persisted = list(tmp_path.glob("auto_*.json"))
        assert len(persisted) == 1
        auto_session_id = persisted[0].stem
        assert auto_session_id in result.error.message
        assert result.error.details["auto_session_id"] == auto_session_id
        assert "resume" in result.error.message
        fake_inner_auto.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_acceptance_pending_preserves_structured_job_receipt(
        self,
        event_store,
        tmp_path,
        fake_inner_auto,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        expected = MCPToolError(
            "Detached worker acceptance is still pending",
            tool_name="ouroboros_start_auto",
            error_code="detached_job_acceptance_pending",
            details={
                "status": "acceptance_pending",
                "job_id": "job_pending",
                "status_check": {
                    "tool": "ouroboros_job_status",
                    "arguments": {"job_id": "job_pending"},
                },
            },
        )

        async def fail_with_receipt(**_kwargs):  # type: ignore[no-untyped-def]
            raise expected

        monkeypatch.setattr(
            "ouroboros.mcp.tools.auto_handler.start_background_tool_job",
            fail_with_receipt,
        )
        handler = StartAutoHandler(
            event_store=event_store,
            job_manager=JobManager(event_store),
            store=AutoStore(tmp_path),
        )
        handler._inner_auto = fake_inner_auto

        result = await handler.handle({"goal": "build a CLI"})

        assert result.is_err
        assert result.error is expected
        assert result.error.details["job_id"] == "job_pending"
        assert result.error.details["status_check"] == {
            "tool": "ouroboros_job_status",
            "arguments": {"job_id": "job_pending"},
        }

    @pytest.mark.asyncio
    async def test_plugin_dispatch_failure_returns_persisted_auto_session_id(
        self, tmp_path, fake_inner_auto
    ) -> None:
        event_store = MagicMock()
        event_store.initialize = AsyncMock(side_effect=RuntimeError("event store down"))
        job_manager = MagicMock()
        job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
        job_manager.start_job = AsyncMock()
        store = AutoStore(tmp_path)
        h = StartAutoHandler(
            event_store=event_store,
            job_manager=job_manager,
            store=store,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )
        h._inner_auto = fake_inner_auto

        result = await h.handle({"goal": "build a CLI"})

        assert result.is_err
        persisted = list(tmp_path.glob("auto_*.json"))
        assert len(persisted) == 1
        auto_session_id = persisted[0].stem
        assert auto_session_id in result.error.message
        assert result.error.details["auto_session_id"] == auto_session_id
        assert "resume" in result.error.message
        assert not persisted[0].with_suffix(".start_auto_lease.json").exists()
        job_manager.start_job.assert_not_called()
        fake_inner_auto.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_active_background_job_for_session_errors_before_enqueue(
        self, event_store, tmp_path, fake_inner_auto
    ) -> None:
        store = AutoStore(tmp_path)
        state = AutoPipelineState(goal="build a CLI", cwd=str(tmp_path))
        store.save(state)
        active_snapshot = MagicMock()
        active_snapshot.job_id = "job_auto_active"
        active_snapshot.status.value = "running"
        job_manager = MagicMock()
        job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
        job_manager.find_active_job_by_session = AsyncMock(return_value=active_snapshot)
        job_manager.start_job = AsyncMock()
        h = StartAutoHandler(event_store=event_store, job_manager=job_manager, store=store)
        h._inner_auto = fake_inner_auto

        result = await h.handle(
            {"resume": state.auto_session_id, "_start_auto_lease_token": "lease_active"}
        )

        assert result.is_err
        assert state.auto_session_id in result.error.message
        assert result.error.details["job_id"] == "job_auto_active"
        job_manager.start_job.assert_not_called()
        fake_inner_auto.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_job_lease_allows_resume_after_restart_gap(
        self, event_store, tmp_path, fake_inner_auto
    ) -> None:
        store = AutoStore(tmp_path)
        state = AutoPipelineState(goal="build a CLI", cwd=str(tmp_path))
        store.save(state)
        store.path_for(state.auto_session_id).with_suffix(".start_auto_lease.json").write_text(
            json.dumps(
                {
                    "token": "lease_job",
                    "mode": "job",
                    "job_id": "job_stale",
                    "created_at": datetime.now(UTC).isoformat(),
                    "updated_at": datetime.now(UTC).isoformat(),
                    "owner_pid": os.getpid(),
                    "owner_start_time": 0.0,
                }
            ),
            encoding="utf-8",
        )
        stale_snapshot = MagicMock()
        stale_snapshot.job_id = "job_stale"
        stale_snapshot.is_terminal = False
        stale_snapshot.status.value = "queued"
        new_snapshot = MagicMock()
        new_snapshot.job_id = "job_new"

        class RestartedJobManager:
            def __init__(self) -> None:
                self.start_job = AsyncMock(side_effect=self._start_job)

            async def allocate_job_id(self) -> str:
                return "job_alloc"

            async def get_snapshot(self, job_id):
                assert job_id == "job_stale"
                return stale_snapshot

            def has_live_job_task(self, job_id):
                assert job_id == "job_stale"
                return False

            async def find_active_job_by_session(self, session_id, *, job_type=None):
                assert session_id == state.auto_session_id
                assert job_type == "auto"
                return stale_snapshot

            async def _start_job(self, *, runner, **_):
                if inspect.iscoroutine(runner):
                    runner.close()
                return new_snapshot

        job_manager = RestartedJobManager()
        h = StartAutoHandler(
            event_store=event_store,
            job_manager=job_manager,  # type: ignore[arg-type]
            store=store,
        )
        h._inner_auto = fake_inner_auto

        result = await h.handle({"resume": state.auto_session_id})

        assert result.is_ok
        assert result.value.meta["job_id"] == "job_new"
        job_manager.start_job.assert_awaited_once()
        fake_inner_auto.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_live_job_lease_blocks_other_process_without_local_task(
        self, event_store, tmp_path, fake_inner_auto
    ) -> None:
        store = AutoStore(tmp_path)
        state = AutoPipelineState(goal="build a CLI", cwd=str(tmp_path))
        store.save(state)
        store.path_for(state.auto_session_id).with_suffix(".start_auto_lease.json").write_text(
            json.dumps(
                {
                    "token": "lease_job",
                    "mode": "job",
                    "job_id": "job_live_elsewhere",
                    "created_at": datetime.now(UTC).isoformat(),
                    "updated_at": datetime.now(UTC).isoformat(),
                    "owner_pid": os.getpid(),
                    "owner_start_time": None,
                }
            ),
            encoding="utf-8",
        )
        active_snapshot = MagicMock()
        active_snapshot.job_id = "job_live_elsewhere"
        active_snapshot.is_terminal = False
        active_snapshot.status.value = "running"

        class OtherProcessJobManager:
            start_job = AsyncMock()

            async def get_snapshot(self, job_id):
                assert job_id == "job_live_elsewhere"
                return active_snapshot

            def has_live_job_task(self, job_id):
                assert job_id == "job_live_elsewhere"
                return False

            async def find_active_job_by_session(self, session_id, *, job_type=None):
                assert session_id == state.auto_session_id
                assert job_type == "auto"
                return active_snapshot

        job_manager = OtherProcessJobManager()
        h = StartAutoHandler(
            event_store=event_store,
            job_manager=job_manager,  # type: ignore[arg-type]
            store=store,
        )
        h._inner_auto = fake_inner_auto

        result = await h.handle({"resume": state.auto_session_id})

        assert result.is_err
        assert "active background job" in result.error.message
        assert result.error.details["job_id"] == "job_live_elsewhere"
        job_manager.start_job.assert_not_called()
        fake_inner_auto.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_pending_lease_blocks_concurrent_resume_before_job_row_exists(
        self, event_store, tmp_path, fake_inner_auto
    ) -> None:
        store = AutoStore(tmp_path)
        state = AutoPipelineState(goal="build a CLI", cwd=str(tmp_path))
        store.save(state)
        started = asyncio.Event()
        release = asyncio.Event()
        job_manager = MagicMock()
        job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
        job_manager.find_active_job_by_session = AsyncMock(return_value=None)
        snapshot = MagicMock()
        snapshot.job_id = "job_auto_lease"

        async def _start_job(*, runner, **_):
            if inspect.iscoroutine(runner):
                runner.close()
            started.set()
            await release.wait()
            return snapshot

        job_manager.start_job = AsyncMock(side_effect=_start_job)
        h = StartAutoHandler(event_store=event_store, job_manager=job_manager, store=store)
        h._inner_auto = fake_inner_auto

        first = asyncio.create_task(h.handle({"resume": state.auto_session_id}))
        await asyncio.wait_for(started.wait(), timeout=2.0)
        second = await h.handle({"resume": state.auto_session_id})
        release.set()
        first_result = await first

        assert second.is_err
        assert "pending start lease" in second.error.message
        assert second.error.details["auto_session_id"] == state.auto_session_id
        assert first_result.is_ok
        assert first_result.value.meta["job_id"] == "job_auto_lease"
        fake_inner_auto.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_active_plugin_lease_for_session_errors_before_redispatch(
        self, event_store, tmp_path, fake_inner_auto
    ) -> None:
        store = AutoStore(tmp_path)
        state = AutoPipelineState(goal="build a CLI", cwd=str(tmp_path))
        store.save(state)
        store.path_for(state.auto_session_id).with_suffix(".start_auto_lease.json").write_text(
            json.dumps(
                {
                    "token": "lease_active",
                    "mode": "plugin_dispatched",
                    "created_at": datetime.now(UTC).isoformat(),
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        job_manager = MagicMock()
        job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
        job_manager.find_active_job_by_session = AsyncMock(return_value=None)
        job_manager.start_job = AsyncMock()
        h = StartAutoHandler(
            event_store=event_store,
            job_manager=job_manager,
            store=store,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )
        h._inner_auto = fake_inner_auto

        result = await h.handle({"resume": state.auto_session_id})

        assert result.is_err
        assert "active plugin dispatch" in result.error.message
        assert result.error.details["auto_session_id"] == state.auto_session_id
        job_manager.start_job.assert_not_called()
        fake_inner_auto.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_expired_plugin_lease_allows_redispatch(
        self, event_store, tmp_path, fake_inner_auto
    ) -> None:
        store = AutoStore(tmp_path)
        state = AutoPipelineState(goal="build a CLI", cwd=str(tmp_path))
        store.save(state)
        store.path_for(state.auto_session_id).with_suffix(".start_auto_lease.json").write_text(
            json.dumps(
                {
                    "token": "lease_stale",
                    "mode": "plugin_dispatched",
                    "created_at": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
                    "expires_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        job_manager = MagicMock()
        job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
        job_manager.find_active_job_by_session = AsyncMock(return_value=None)
        job_manager.start_job = AsyncMock()
        h = StartAutoHandler(
            event_store=event_store,
            job_manager=job_manager,
            store=store,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )
        h._inner_auto = fake_inner_auto

        result = await h.handle({"resume": state.auto_session_id})

        assert result.is_ok
        assert result.value.meta["status"] == "delegated_to_plugin"
        job_manager.start_job.assert_not_called()
        fake_inner_auto.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_dead_owner_plugin_lease_allows_redispatch(
        self, event_store, tmp_path, fake_inner_auto
    ) -> None:
        store = AutoStore(tmp_path)
        state = AutoPipelineState(goal="build a CLI", cwd=str(tmp_path))
        store.save(state)
        store.path_for(state.auto_session_id).with_suffix(".start_auto_lease.json").write_text(
            json.dumps(
                {
                    "token": "lease_stale",
                    "mode": "plugin_dispatched",
                    "created_at": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    "owner_pid": os.getpid(),
                    "owner_start_time": 0.0,
                }
            ),
            encoding="utf-8",
        )
        job_manager = MagicMock()
        job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
        job_manager.find_active_job_by_session = AsyncMock(return_value=None)
        job_manager.start_job = AsyncMock()
        h = StartAutoHandler(
            event_store=event_store,
            job_manager=job_manager,
            store=store,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )
        h._inner_auto = fake_inner_auto

        result = await h.handle({"resume": state.auto_session_id})

        assert result.is_ok
        assert result.value.meta["status"] == "delegated_to_plugin"
        job_manager.start_job.assert_not_called()
        fake_inner_auto.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_plugin_dispatch_lease_uses_short_handoff_timeout(
        self, event_store, tmp_path, fake_inner_auto
    ) -> None:
        store = AutoStore(tmp_path)
        h = StartAutoHandler(
            event_store=event_store,
            job_manager=MagicMock(),
            store=store,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )
        h._inner_auto = fake_inner_auto

        result = await h.handle({"goal": "build a CLI", "pipeline_timeout_seconds": 900})

        assert result.is_ok
        auto_session_id = result.value.meta["auto_session_id"]
        lease = json.loads(
            store.path_for(auto_session_id)
            .with_suffix(".start_auto_lease.json")
            .read_text(encoding="utf-8")
        )
        lease_window = datetime.fromisoformat(lease["expires_at"]) - datetime.fromisoformat(
            lease["updated_at"]
        )
        assert lease["mode"] == "plugin_dispatched"
        assert lease_window <= timedelta(seconds=_START_AUTO_PENDING_LEASE_SECONDS + 1)

    @pytest.mark.asyncio
    async def test_plugin_child_claim_refreshes_lease_while_auto_is_running(
        self, event_store, tmp_path, fake_inner_auto, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ouroboros.mcp.tools import auto_handler as auto_module

        store = AutoStore(tmp_path)
        state = AutoPipelineState(goal="build a CLI", cwd=str(tmp_path))
        state.pipeline_timeout_seconds = 900
        state.runtime_backend = "opencode"
        state.opencode_mode = "plugin"
        store.save(state)
        lease_path = store.path_for(state.auto_session_id).with_suffix(".start_auto_lease.json")
        started = asyncio.Event()
        release = asyncio.Event()
        now = datetime.now(UTC)
        lease_path.write_text(
            json.dumps(
                {
                    "token": "lease_active",
                    "mode": "plugin_dispatched",
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                    "expires_at": (
                        now + timedelta(seconds=_START_AUTO_PENDING_LEASE_SECONDS)
                    ).isoformat(),
                    "owner_pid": 99999999,
                    "owner_start_time": None,
                }
            ),
            encoding="utf-8",
        )

        class StubAutoHandler(AutoHandler):
            async def _run(self, arguments):
                started.set()
                await release.wait()
                return AutoPipelineResult(
                    status="running",
                    auto_session_id=state.auto_session_id,
                    phase="interview",
                    pending_question="Which runtime?",
                )

        class LaterDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = now + timedelta(seconds=_START_AUTO_PENDING_LEASE_SECONDS + 1)
                return value if tz is not None else value.replace(tzinfo=None)

        child = asyncio.create_task(
            StubAutoHandler(store=store).handle(
                {"resume": state.auto_session_id, "_start_auto_lease_token": "lease_active"}
            )
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=2.0)
            lease = json.loads(lease_path.read_text(encoding="utf-8"))
            lease_window = datetime.fromisoformat(lease["expires_at"]) - datetime.fromisoformat(
                lease["updated_at"]
            )
            assert lease["mode"] == "plugin_claimed"
            assert lease["owner_pid"] == os.getpid()
            assert lease_window > timedelta(seconds=_START_AUTO_PENDING_LEASE_SECONDS)

            monkeypatch.setattr(auto_module, "datetime", LaterDateTime)
            job_manager = MagicMock()
            job_manager.allocate_job_id = AsyncMock(return_value="job_alloc")
            job_manager.find_active_job_by_session = AsyncMock(return_value=None)
            job_manager.start_job = AsyncMock()
            h = StartAutoHandler(
                event_store=event_store,
                job_manager=job_manager,
                store=store,
                agent_runtime_backend="opencode",
                opencode_mode="plugin",
            )
            h._inner_auto = fake_inner_auto

            result = await h.handle({"resume": state.auto_session_id})

            assert result.is_err
            assert "active plugin dispatch" in result.error.message
            job_manager.start_job.assert_not_called()
            fake_inner_auto.handle.assert_not_called()
        finally:
            release.set()
            await child

    @pytest.mark.asyncio
    async def test_plugin_subagent_arguments_claim_and_release_parent_handoff(
        self, event_store, tmp_path, fake_inner_auto
    ) -> None:
        store = AutoStore(tmp_path)
        starter = StartAutoHandler(
            event_store=event_store,
            job_manager=MagicMock(),
            store=store,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )
        starter._inner_auto = fake_inner_auto

        dispatched = await starter.handle({"goal": "build a CLI", "pipeline_timeout_seconds": 900})

        assert dispatched.is_ok
        subagent = dispatched.value.meta["_subagent"]
        child_arguments = subagent["context"]["arguments"]
        auto_session_id = dispatched.value.meta["auto_session_id"]
        lease_path = store.path_for(auto_session_id).with_suffix(".start_auto_lease.json")

        class StubAutoHandler(AutoHandler):
            async def _run(self, arguments):
                lease = json.loads(lease_path.read_text(encoding="utf-8"))
                lease_window = datetime.fromisoformat(lease["expires_at"]) - datetime.fromisoformat(
                    lease["updated_at"]
                )
                assert lease["mode"] == "plugin_claimed"
                assert lease["owner_pid"] == os.getpid()
                assert lease_window > timedelta(seconds=_START_AUTO_PENDING_LEASE_SECONDS)
                return AutoPipelineResult(
                    status="running",
                    auto_session_id=auto_session_id,
                    phase="interview",
                    pending_question="Which runtime?",
                )

        claimed = await StubAutoHandler(store=store).handle(child_arguments)

        assert claimed.is_ok
        assert not lease_path.exists()


class TestAutoHandlerLeaseRelease:
    @pytest.mark.asyncio
    async def test_nonterminal_auto_result_releases_start_auto_lease(self, tmp_path) -> None:
        store = AutoStore(tmp_path)
        state = AutoPipelineState(goal="build a CLI", cwd=str(tmp_path))
        store.save(state)
        store.path_for(state.auto_session_id).with_suffix(".start_auto_lease.json").write_text(
            json.dumps(
                {
                    "token": "lease_active",
                    "mode": "plugin_dispatched",
                    "created_at": datetime.now(UTC).isoformat(),
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                }
            ),
            encoding="utf-8",
        )

        class StubAutoHandler(AutoHandler):
            async def _run(self, arguments):
                return AutoPipelineResult(
                    status="running",
                    auto_session_id=state.auto_session_id,
                    phase="interview",
                    pending_question="Which runtime?",
                )

        h = StubAutoHandler(store=store)
        result = await h.handle(
            {"resume": state.auto_session_id, "_start_auto_lease_token": "lease_active"}
        )

        assert result.is_ok
        assert (
            not store.path_for(state.auto_session_id).with_suffix(".start_auto_lease.json").exists()
        )

    @pytest.mark.asyncio
    async def test_failed_auto_result_releases_start_auto_lease(self, tmp_path) -> None:
        store = AutoStore(tmp_path)
        state = AutoPipelineState(goal="build a CLI", cwd=str(tmp_path))
        store.save(state)
        store.path_for(state.auto_session_id).with_suffix(".start_auto_lease.json").write_text(
            json.dumps(
                {
                    "token": "lease_active",
                    "mode": "plugin_dispatched",
                    "created_at": datetime.now(UTC).isoformat(),
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                }
            ),
            encoding="utf-8",
        )

        class StubAutoHandler(AutoHandler):
            async def _run(self, arguments):
                raise RuntimeError("child failed")

        h = StubAutoHandler(store=store)
        result = await h.handle(
            {"resume": state.auto_session_id, "_start_auto_lease_token": "lease_active"}
        )

        assert result.is_err
        assert (
            not store.path_for(state.auto_session_id).with_suffix(".start_auto_lease.json").exists()
        )

    @pytest.mark.asyncio
    async def test_start_auto_token_can_only_claim_plugin_handoff_once(self, tmp_path) -> None:
        store = AutoStore(tmp_path)
        state = AutoPipelineState(goal="build a CLI", cwd=str(tmp_path))
        store.save(state)
        lease_path = store.path_for(state.auto_session_id).with_suffix(".start_auto_lease.json")
        lease_path.write_text(
            json.dumps(
                {
                    "token": "lease_active",
                    "mode": "plugin_claimed",
                    "created_at": datetime.now(UTC).isoformat(),
                    "updated_at": datetime.now(UTC).isoformat(),
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                }
            ),
            encoding="utf-8",
        )

        class StubAutoHandler(AutoHandler):
            async def _run(self, arguments):
                raise AssertionError("_run must not run for an already-claimed token")

        result = await StubAutoHandler(store=store).handle(
            {"resume": state.auto_session_id, "_start_auto_lease_token": "lease_active"}
        )

        assert result.is_err
        assert "not claimable" in result.error.message
        assert json.loads(lease_path.read_text(encoding="utf-8"))["mode"] == "plugin_claimed"

    @pytest.mark.asyncio
    async def test_start_auto_job_token_claim_promotes_pending_to_job_mode(self, tmp_path) -> None:
        store = AutoStore(tmp_path)
        state = AutoPipelineState(goal="build a CLI", cwd=str(tmp_path))
        state.pipeline_timeout_seconds = 900
        store.save(state)
        lease_path = store.path_for(state.auto_session_id).with_suffix(".start_auto_lease.json")
        lease_path.write_text(
            json.dumps(
                {
                    "token": "lease_active",
                    "mode": "job_pending",
                    "job_id": "job_active",
                    "created_at": datetime.now(UTC).isoformat(),
                    "updated_at": datetime.now(UTC).isoformat(),
                    "owner_pid": 99999999,
                    "owner_start_time": None,
                }
            ),
            encoding="utf-8",
        )

        class StubAutoHandler(AutoHandler):
            async def _run(self, arguments):
                lease = json.loads(lease_path.read_text(encoding="utf-8"))
                assert lease["mode"] == "job"
                assert lease["job_id"] == "job_active"
                assert lease["owner_pid"] == os.getpid()
                assert "expires_at" not in lease
                return AutoPipelineResult(
                    status="running",
                    auto_session_id=state.auto_session_id,
                    phase="interview",
                    pending_question="Which runtime?",
                )

        result = await StubAutoHandler(store=store).handle(
            {"resume": state.auto_session_id, "_start_auto_lease_token": "lease_active"}
        )

        assert result.is_ok
        assert not lease_path.exists()

    @pytest.mark.asyncio
    async def test_direct_auto_resume_without_token_respects_start_auto_lease(
        self, tmp_path
    ) -> None:
        store = AutoStore(tmp_path)
        state = AutoPipelineState(goal="build a CLI", cwd=str(tmp_path))
        store.save(state)
        lease_path = store.path_for(state.auto_session_id).with_suffix(".start_auto_lease.json")
        lease_path.write_text(
            json.dumps(
                {
                    "token": "lease_active",
                    "mode": "plugin_dispatched",
                    "created_at": datetime.now(UTC).isoformat(),
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                }
            ),
            encoding="utf-8",
        )

        class StubAutoHandler(AutoHandler):
            async def _run(self, arguments):
                return AutoPipelineResult(
                    status="running",
                    auto_session_id=state.auto_session_id,
                    phase="interview",
                    pending_question="Which runtime?",
                )

        h = StubAutoHandler(store=store)
        result = await h.handle({"resume": state.auto_session_id})

        assert result.is_err
        assert "pending start lease" in result.error.message
        assert lease_path.exists()

    @pytest.mark.asyncio
    async def test_direct_auto_resume_rejects_forged_start_auto_token(self, tmp_path) -> None:
        store = AutoStore(tmp_path)
        state = AutoPipelineState(goal="build a CLI", cwd=str(tmp_path))
        store.save(state)
        lease_path = store.path_for(state.auto_session_id).with_suffix(".start_auto_lease.json")
        lease_path.write_text(
            json.dumps(
                {
                    "token": "real_token",
                    "mode": "plugin_dispatched",
                    "created_at": datetime.now(UTC).isoformat(),
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                }
            ),
            encoding="utf-8",
        )

        class StubAutoHandler(AutoHandler):
            async def _run(self, arguments):
                raise AssertionError("_run must not run with a forged lease token")

        h = StubAutoHandler(store=store)
        result = await h.handle(
            {"resume": state.auto_session_id, "_start_auto_lease_token": "forged_token"}
        )

        assert result.is_err
        assert "Invalid start_auto lease token" in result.error.message
        assert json.loads(lease_path.read_text(encoding="utf-8"))["token"] == "real_token"

    @pytest.mark.asyncio
    async def test_start_auto_token_without_resume_is_rejected(self, tmp_path) -> None:
        class StubAutoHandler(AutoHandler):
            async def _run(self, arguments):
                raise AssertionError("_run must not run with a stray lease token")

        h = StubAutoHandler(store=AutoStore(tmp_path))
        result = await h.handle({"goal": "build a CLI", "_start_auto_lease_token": "forged"})

        assert result.is_err
        assert "_start_auto_lease_token is reserved" in result.error.message

    @pytest.mark.asyncio
    async def test_direct_auto_resume_acquires_and_releases_own_lease(self, tmp_path) -> None:
        store = AutoStore(tmp_path)
        state = AutoPipelineState(goal="build a CLI", cwd=str(tmp_path))
        state.pipeline_timeout_seconds = 900
        store.save(state)
        lease_path = store.path_for(state.auto_session_id).with_suffix(".start_auto_lease.json")

        class StubAutoHandler(AutoHandler):
            async def _run(self, arguments):
                lease = json.loads(lease_path.read_text(encoding="utf-8"))
                assert lease["mode"] == "direct_auto"
                assert lease["owner_pid"] == os.getpid()
                return AutoPipelineResult(
                    status="running",
                    auto_session_id=state.auto_session_id,
                    phase="interview",
                    pending_question="Which runtime?",
                )

        h = StubAutoHandler(store=store)
        result = await h.handle({"resume": state.auto_session_id})

        assert result.is_ok
        assert not lease_path.exists()

    @pytest.mark.asyncio
    async def test_direct_auto_resume_recovers_dead_owner_lease(self, tmp_path) -> None:
        store = AutoStore(tmp_path)
        state = AutoPipelineState(goal="build a CLI", cwd=str(tmp_path))
        state.pipeline_timeout_seconds = 900
        store.save(state)
        lease_path = store.path_for(state.auto_session_id).with_suffix(".start_auto_lease.json")
        lease_path.write_text(
            json.dumps(
                {
                    "token": "dead_direct",
                    "mode": "direct_auto",
                    "created_at": datetime.now(UTC).isoformat(),
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    "owner_pid": os.getpid(),
                    "owner_start_time": 0.0,
                }
            ),
            encoding="utf-8",
        )

        class StubAutoHandler(AutoHandler):
            async def _run(self, arguments):
                lease = json.loads(lease_path.read_text(encoding="utf-8"))
                assert lease["mode"] == "direct_auto"
                assert lease["token"] != "dead_direct"
                return AutoPipelineResult(
                    status="running",
                    auto_session_id=state.auto_session_id,
                    phase="interview",
                    pending_question="Which runtime?",
                )

        h = StubAutoHandler(store=store)
        result = await h.handle({"resume": state.auto_session_id})

        assert result.is_ok
        assert not lease_path.exists()
