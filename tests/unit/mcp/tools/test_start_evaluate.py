"""Tests for StartEvaluateHandler — async/fire-and-forget evaluate wrapper.

Background: the synchronous ``ouroboros_evaluate`` tool routinely exceeds an
MCP client's 120s tool-call timeout when the three-stage pipeline (mechanical
+ semantic + optional consensus) runs end-to-end. ``StartEvaluateHandler``
mirrors :class:`StartExecuteSeedHandler` and :class:`StartEvolveStepHandler`:
return a ``job_id`` immediately, run the evaluation in a JobManager-backed
background task, let callers poll ``job_status`` / ``job_wait`` / ``job_result``.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.tools.evaluation_handlers import (
    EvaluateHandler,
    StartEvaluateHandler,
)
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
            p.name for p in inner.definition.parameters
        }


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
    async def test_returns_job_id_immediately(self, event_store, fake_inner_handler) -> None:
        job_manager = MagicMock()
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
        # Inner evaluate must NOT have been called synchronously — fire-and-forget.
        fake_inner_handler.handle.assert_not_called()
        job_manager.start_job.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_job_type_and_links(self, event_store, fake_inner_handler) -> None:
        job_manager = MagicMock()
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
    def handler(self, event_store):
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
