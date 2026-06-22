"""Dashboard URL helpers use the active MCP EventStore path."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import ouroboros.dashboard_web as dashboard_web
from ouroboros.mcp.job_manager import JobLinks, JobSnapshot, JobStatus
from ouroboros.mcp.tools import auto_handler, execution_handlers
from ouroboros.mcp.tools.execution_handlers import StartExecuteSeedHandler
from ouroboros.mcp.types import MCPToolResult
from ouroboros.persistence.event_store import EventStore


@pytest.mark.asyncio
async def test_execute_dashboard_url_uses_injected_event_store_db(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def _fake_dashboard_url_for_run(run_id: str, *, db_path: str | None = None) -> str:
        seen["run_id"] = run_id
        seen["db_path"] = db_path
        return "http://localhost:1234/?run=exec_custom"

    monkeypatch.setattr(dashboard_web, "dashboard_url_for_run", _fake_dashboard_url_for_run)
    store = EventStore("sqlite+aiosqlite:////tmp/custom-ouroboros.db")

    url = await execution_handlers._resolve_dashboard_url("exec_custom", event_store=store)

    assert url == "http://localhost:1234/?run=exec_custom"
    assert seen == {"run_id": "exec_custom", "db_path": "/tmp/custom-ouroboros.db"}


@pytest.mark.asyncio
async def test_auto_dashboard_base_url_uses_injected_event_store_db(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def _fake_dashboard_base_url(*, db_path: str | None = None) -> str:
        seen["db_path"] = db_path
        return "http://localhost:1234"

    monkeypatch.setattr(dashboard_web, "dashboard_base_url", _fake_dashboard_base_url)
    store = EventStore("sqlite+aiosqlite:////tmp/custom-auto.db")

    url = await auto_handler._resolve_dashboard_base_url(event_store=store)

    assert url == "http://localhost:1234"
    assert seen == {"db_path": "/tmp/custom-auto.db"}


@pytest.mark.asyncio
async def test_start_execute_seed_dashboard_url_uses_handler_event_store(
    monkeypatch,
) -> None:
    seen: dict[str, object] = {}
    store = EventStore("sqlite+aiosqlite:////tmp/start-execute-custom.db")

    async def _fake_start_background_tool_job(**kwargs):
        now = datetime.now(UTC)
        return JobSnapshot(
            job_id="job_custom",
            job_type="execute_seed",
            status=JobStatus.QUEUED,
            message="queued",
            created_at=now,
            updated_at=now,
            links=JobLinks(session_id="session_custom", execution_id="exec_custom"),
        )

    async def _fake_resolve_dashboard_url(
        execution_id: str | None,
        *,
        event_store: EventStore | None = None,
    ) -> str | None:
        seen["execution_id"] = execution_id
        seen["event_store"] = event_store
        return "http://localhost:1234/?run=exec_custom"

    class _ExecuteHandler:
        agent_runtime_backend = "codex"
        llm_backend = "openai"

        async def handle(self, *args, **kwargs):  # pragma: no cover - runner is not awaited
            return MCPToolResult(content=())

    monkeypatch.setattr(
        execution_handlers,
        "start_background_tool_job",
        _fake_start_background_tool_job,
    )
    monkeypatch.setattr(
        execution_handlers,
        "_resolve_dashboard_url",
        _fake_resolve_dashboard_url,
    )

    handler = StartExecuteSeedHandler(
        execute_handler=_ExecuteHandler(),  # type: ignore[arg-type]
        event_store=store,
    )

    result = await handler.handle({"seed_content": "goal: demo"})

    assert result.is_ok
    assert seen == {"execution_id": "exec_custom", "event_store": store}
    assert result.value.meta["dashboard_url"] == "http://localhost:1234/?run=exec_custom"
