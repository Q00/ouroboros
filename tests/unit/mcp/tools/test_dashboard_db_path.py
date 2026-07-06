"""Regression: the dashboard daemon must be scoped to the handler's actual DB.

``ooo mcp --db-path`` injects an ``EventStore`` for a custom SQLite file into the
execute/auto handlers. Because the dashboard daemon is DB-scoped (defaults to
``~/.ouroboros/ouroboros.db``), the resolver must thread that store's path through
to ``dashboard_url_for_run`` / ``dashboard_base_url`` — otherwise the published
dashboard tails the wrong database and shows empty/unrelated runs.
"""

from __future__ import annotations

import ouroboros.dashboard_web as dashboard_web
from ouroboros.mcp.tools import auto_handler, execution_handlers
from ouroboros.mcp.tools.auto_handler import StartAutoHandler
from ouroboros.mcp.tools.execution_handlers import (
    ExecuteSeedHandler,
    StartExecuteSeedHandler,
)
from ouroboros.persistence.event_store import EventStore

_CUSTOM_URL = "sqlite+aiosqlite:////tmp/ooo-custom/session.db"
_CUSTOM_PATH = "/tmp/ooo-custom/session.db"


class TestEventStoreDbPathExtraction:
    def test_execute_helper_reads_custom_store_path(self) -> None:
        assert execution_handlers._event_store_db_path(EventStore(_CUSTOM_URL)) == _CUSTOM_PATH

    def test_auto_helper_reads_custom_store_path(self) -> None:
        assert auto_handler._event_store_db_path(EventStore(_CUSTOM_URL)) == _CUSTOM_PATH

    def test_none_store_yields_none(self) -> None:
        assert execution_handlers._event_store_db_path(None) is None
        assert auto_handler._event_store_db_path(None) is None

    def test_handlers_expose_the_injected_custom_store(self) -> None:
        # The handlers built by ``ooo mcp --db-path`` must surface the custom path
        # via the exact attribute the resolver reads at each call site.
        store = EventStore(_CUSTOM_URL)
        execute = ExecuteSeedHandler(event_store=store)
        assert execution_handlers._event_store_db_path(execute.event_store) == _CUSTOM_PATH

        start_execute = StartExecuteSeedHandler(event_store=store)
        assert execution_handlers._event_store_db_path(start_execute._event_store) == _CUSTOM_PATH

        start_auto = StartAutoHandler(event_store=store)
        assert auto_handler._event_store_db_path(start_auto._event_store) == _CUSTOM_PATH


class TestResolverThreadsDbPath:
    async def test_execute_resolver_forwards_db_path_to_daemon(self, monkeypatch) -> None:
        captured: dict[str, object] = {}

        def _fake(run_id, *, db_path=None, host="127.0.0.1"):
            captured["run_id"] = run_id
            captured["db_path"] = db_path
            return "http://localhost:9999/?run=" + run_id

        monkeypatch.setattr(dashboard_web, "dashboard_url_for_run", _fake)

        url = await execution_handlers._resolve_dashboard_url("exec_1", db_path=_CUSTOM_PATH)
        assert url == "http://localhost:9999/?run=exec_1"
        assert captured == {"run_id": "exec_1", "db_path": _CUSTOM_PATH}

    async def test_auto_resolver_forwards_db_path_to_daemon(self, monkeypatch) -> None:
        captured: dict[str, object] = {}

        def _fake(*, db_path=None, host="127.0.0.1"):
            captured["db_path"] = db_path
            return "http://localhost:9999"

        monkeypatch.setattr(dashboard_web, "dashboard_base_url", _fake)

        url = await auto_handler._resolve_dashboard_base_url(db_path=_CUSTOM_PATH)
        assert url == "http://localhost:9999"
        assert captured == {"db_path": _CUSTOM_PATH}

    async def test_execute_end_to_end_custom_store_reaches_daemon(self, monkeypatch) -> None:
        # The full seam: a custom-path handler's store path lands at the daemon.
        captured: dict[str, object] = {}

        def _fake(_run_id, *, db_path=None, **_kw):
            captured["db_path"] = db_path

        monkeypatch.setattr(dashboard_web, "dashboard_url_for_run", _fake)
        handler = ExecuteSeedHandler(event_store=EventStore(_CUSTOM_URL))
        await execution_handlers._resolve_dashboard_url(
            "exec_1", db_path=execution_handlers._event_store_db_path(handler.event_store)
        )
        assert captured["db_path"] == _CUSTOM_PATH
