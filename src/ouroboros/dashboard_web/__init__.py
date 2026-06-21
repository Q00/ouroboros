"""Provider-agnostic live web dashboard for ouroboros runs.

A run (``ooo run`` / ``ooo auto``) writes its activity to the EventStore (SSOT).
This package serves that activity as a live Kanban in the browser — every worker
sub-agent shows up as a card regardless of provider (codex / claude / opencode /
hermes …), because the dashboard reads the provider-neutral event stream, not any
single tool's UI. This is the meta-harness answer to "see the sub-agents": one
board, any provider, any stage.

Dependency-free by design: a stdlib ``http.server`` + SSE tail of the EventStore
SQLite file (read-only). No FastAPI/uvicorn — keeps the supply chain pinned.
"""

from __future__ import annotations

from ouroboros.dashboard_web.daemon import (
    DashboardInfo,
    dashboard_base_url,
    dashboard_url_for_run,
    ensure_dashboard,
    is_enabled,
)
from ouroboros.dashboard_web.kanban import reduce_board
from ouroboros.dashboard_web.launcher import DashboardHandle, serve_dashboard

__all__ = [
    "DashboardHandle",
    "DashboardInfo",
    "dashboard_base_url",
    "dashboard_url_for_run",
    "ensure_dashboard",
    "is_enabled",
    "reduce_board",
    "serve_dashboard",
]
