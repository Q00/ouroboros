"""Dashboard URL helpers use the active MCP EventStore path."""

from __future__ import annotations

import pytest

import ouroboros.dashboard_web as dashboard_web
from ouroboros.mcp.tools import auto_handler, execution_handlers
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
