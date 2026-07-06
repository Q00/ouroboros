"""Shared dashboard-URL resolution for the MCP execute/auto handlers.

The live dashboard daemon is DB-scoped, so a handler must point it at the exact
EventStore its run writes to. Resolution is *tri-state* on the injected store:

* **no store injected** → the daemon's own default (home DB) is correct; pass
  ``db_path=None`` and let it resolve as before;
* **store with a real SQLite file** → point the daemon at that file so the
  dashboard tails the DB the run actually writes to;
* **store present but not tailable** (``:memory:`` / non-SQLite backend) →
  SUPPRESS the dashboard URL entirely. Falling back to the home DB here would
  publish a live dashboard for an *unrelated* database, which is worse than no
  dashboard at all.

All resolution is strictly best-effort: any failure yields ``None`` so a run is
never blocked or broken by observability.
"""

from __future__ import annotations

import asyncio

from ouroboros.persistence.event_store import EventStore


def _daemon_db_target(store: EventStore | None) -> tuple[bool, str | None]:
    """Resolve the daemon's DB target for ``store``.

    Returns ``(suppress, db_path)``:

    * ``(False, None)``  — no store: use the daemon default (home DB);
    * ``(False, path)``  — store backed by ``path``: scope the daemon to it;
    * ``(True, None)``   — store present but not dashboard-addressable: omit.
    """
    if store is None:
        return (False, None)
    try:
        path = store.sqlite_path()
    except Exception:  # noqa: BLE001 - observability must never break a run
        path = None
    if path is None:
        # A store WAS injected but has no local file to tail; never silently
        # fall back to the home DB (that would show unrelated runs).
        return (True, None)
    return (False, path)


async def resolve_dashboard_run_url(
    execution_id: str | None, store: EventStore | None
) -> str | None:
    """Best-effort ``?run=`` dashboard URL for a run, scoped to ``store``'s DB.

    Blocking daemon work (healthz probe / first-time spawn wait) is off-loaded to
    a thread so it never blocks the event loop.
    """
    if not execution_id:
        return None
    suppress, db_path = _daemon_db_target(store)
    if suppress:
        return None
    try:
        from ouroboros.dashboard_web import dashboard_url_for_run

        return await asyncio.to_thread(dashboard_url_for_run, execution_id, db_path=db_path)
    except Exception:  # noqa: BLE001 - observability must never break a run
        return None


async def resolve_dashboard_base_url(store: EventStore | None) -> str | None:
    """Best-effort daemon base URL (no run pinned), scoped to ``store``'s DB.

    Used by ``auto``, whose execution id only appears after interview+seed: the
    page auto-selects the latest active run, so the bare base URL is the link.
    """
    suppress, db_path = _daemon_db_target(store)
    if suppress:
        return None
    try:
        from ouroboros.dashboard_web import dashboard_base_url

        return await asyncio.to_thread(dashboard_base_url, db_path=db_path)
    except Exception:  # noqa: BLE001 - observability must never break a run
        return None


__all__ = ["resolve_dashboard_base_url", "resolve_dashboard_run_url"]
