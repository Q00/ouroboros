"""Regression: the dashboard daemon must be scoped to the handler's actual DB.

``ooo mcp --db-path`` injects an ``EventStore`` for a custom SQLite file into the
execute/auto handlers. Because the dashboard daemon is DB-scoped (defaults to
``~/.ouroboros/ouroboros.db``), resolution is tri-state on the injected store:

* no store injected → daemon default (home DB) — pass ``db_path=None``;
* store with a real SQLite file → point the daemon at that file;
* store present but not tailable (``:memory:`` / non-SQLite) → SUPPRESS the URL,
  never fall back to an unrelated home-DB dashboard.
"""

from __future__ import annotations

import ouroboros.dashboard_web as dashboard_web
from ouroboros.mcp.tools import _dashboard
from ouroboros.mcp.tools.auto_handler import StartAutoHandler
from ouroboros.mcp.tools.execution_handlers import (
    ExecuteSeedHandler,
    StartExecuteSeedHandler,
)
from ouroboros.persistence.event_store import EventStore

_CUSTOM_URL = "sqlite+aiosqlite:////tmp/ooo-custom/session.db"
_CUSTOM_PATH = "/tmp/ooo-custom/session.db"
_MEMORY_URL = "sqlite+aiosqlite:///:memory:"


class TestDaemonDbTarget:
    """The tri-state core the resolvers branch on."""

    def test_no_store_uses_daemon_default(self) -> None:
        assert _dashboard._daemon_db_target(None) == (False, None)

    def test_custom_file_store_is_scoped_to_its_path(self) -> None:
        assert _dashboard._daemon_db_target(EventStore(_CUSTOM_URL)) == (False, _CUSTOM_PATH)

    def test_memory_store_is_suppressed(self) -> None:
        assert _dashboard._daemon_db_target(EventStore(_MEMORY_URL)) == (True, None)

    def test_non_sqlite_store_is_suppressed(self) -> None:
        assert _dashboard._daemon_db_target(EventStore("postgresql+asyncpg://h/db")) == (True, None)

    def test_handlers_expose_injected_custom_store_to_resolution(self) -> None:
        # Each handler must surface the custom store via the exact attribute the
        # resolver reads, so the daemon is scoped to the right DB end-to-end.
        store = EventStore(_CUSTOM_URL)
        assert _dashboard._daemon_db_target(ExecuteSeedHandler(event_store=store).event_store) == (
            False,
            _CUSTOM_PATH,
        )
        assert _dashboard._daemon_db_target(
            StartExecuteSeedHandler(event_store=store)._event_store
        ) == (False, _CUSTOM_PATH)
        assert _dashboard._daemon_db_target(StartAutoHandler(event_store=store)._event_store) == (
            False,
            _CUSTOM_PATH,
        )


class TestResolveRunUrl:
    async def test_custom_store_scopes_the_daemon(self, monkeypatch) -> None:
        captured: dict[str, object] = {}

        def _fake(run_id, *, db_path=None, **_kw):
            captured["run_id"] = run_id
            captured["db_path"] = db_path
            return "http://localhost:9999/?run=" + run_id

        monkeypatch.setattr(dashboard_web, "dashboard_url_for_run", _fake)

        url = await _dashboard.resolve_dashboard_run_url("exec_1", EventStore(_CUSTOM_URL))
        assert url == "http://localhost:9999/?run=exec_1"
        assert captured == {"run_id": "exec_1", "db_path": _CUSTOM_PATH}

    async def test_no_store_falls_back_to_daemon_default(self, monkeypatch) -> None:
        captured: dict[str, object] = {}

        def _fake(_run_id, *, db_path=None, **_kw):
            captured["db_path"] = db_path
            return "u"

        monkeypatch.setattr(dashboard_web, "dashboard_url_for_run", _fake)
        await _dashboard.resolve_dashboard_run_url("exec_1", None)
        assert captured == {"db_path": None}  # daemon default (home DB) — unchanged

    async def test_memory_store_suppresses_url_without_touching_daemon(self, monkeypatch) -> None:
        called = False

        def _boom(*_a, **_k):
            nonlocal called
            called = True
            raise AssertionError("daemon must not be contacted for an unaddressable store")

        monkeypatch.setattr(dashboard_web, "dashboard_url_for_run", _boom)

        url = await _dashboard.resolve_dashboard_run_url("exec_1", EventStore(_MEMORY_URL))
        assert url is None
        assert called is False

    async def test_execute_handler_with_memory_store_gets_no_url(self, monkeypatch) -> None:
        # The seam the bot flagged: an in-memory execute handler must NOT publish a
        # dashboard for the unrelated home DB.
        def _boom(*_a, **_k):
            raise AssertionError("must not be called")

        monkeypatch.setattr(dashboard_web, "dashboard_url_for_run", _boom)
        handler = ExecuteSeedHandler(event_store=EventStore(_MEMORY_URL))
        assert await _dashboard.resolve_dashboard_run_url("exec_1", handler.event_store) is None


class TestResolveBaseUrl:
    async def test_custom_store_scopes_the_daemon(self, monkeypatch) -> None:
        captured: dict[str, object] = {}

        def _fake(*, db_path=None, **_kw):
            captured["db_path"] = db_path
            return "http://localhost:9999"

        monkeypatch.setattr(dashboard_web, "dashboard_base_url", _fake)
        url = await _dashboard.resolve_dashboard_base_url(EventStore(_CUSTOM_URL))
        assert url == "http://localhost:9999"
        assert captured == {"db_path": _CUSTOM_PATH}

    async def test_memory_store_suppresses_url(self, monkeypatch) -> None:
        def _boom(**_k):
            raise AssertionError("must not be called")

        monkeypatch.setattr(dashboard_web, "dashboard_base_url", _boom)
        # Both a bare store and an auto handler carrying it must suppress.
        assert await _dashboard.resolve_dashboard_base_url(EventStore(_MEMORY_URL)) is None
        handler = StartAutoHandler(event_store=EventStore(_MEMORY_URL))
        assert await _dashboard.resolve_dashboard_base_url(handler._event_store) is None

    async def test_no_store_falls_back_to_daemon_default(self, monkeypatch) -> None:
        captured: dict[str, object] = {}

        def _fake(*, db_path=None, **_kw):
            captured["db_path"] = db_path
            return "u"

        monkeypatch.setattr(dashboard_web, "dashboard_base_url", _fake)
        await _dashboard.resolve_dashboard_base_url(None)
        assert captured == {"db_path": None}
