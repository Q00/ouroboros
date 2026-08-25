"""``pending_host_dispatches`` surfacing for ``job_status``/``job_wait``.

Split out of ``job_handlers.py`` (module-size ratchet, #1797): the read-side
of the ``HostDispatchBridge`` contract — listing open dispatches for a job's
session and rendering the instructions a polling host acts on — has no other
dependency on the job-handler classes themselves.
"""

from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)


def pending_host_dispatches(
    bridge: Any | None,
    snapshot: Any,
    *,
    announce: bool = True,
) -> list[dict[str, Any]]:
    """Return host dispatches correlated to this job's session.

    ``announce=False`` is read-only observation for ``job_status``; only
    ``job_wait`` claims an actionable announcement.
    """
    if bridge is None or snapshot.is_terminal:
        return []
    session_id = getattr(snapshot.links, "session_id", None)
    try:
        return list(bridge.pending_for_session(session_id, announce=announce))
    except Exception:  # pragma: no cover - defensive: never break job polling
        log.warning("mcp.tool.job.pending_host_dispatches_failed", exc_info=True)
        return []


def pending_host_dispatches_meta_field(pending: list[dict[str, Any]]) -> dict[str, Any]:
    """Meta-dict fragment to splice via ``**`` — empty when there is nothing pending."""
    return {"pending_host_dispatches": pending} if pending else {}


def pending_host_dispatch_suffix(pending: list[dict[str, Any]]) -> str:
    if not any(item.get("actionable") is True for item in pending):
        return (
            f"\n\nPending host dispatches: {len(pending)} — informational only. "
            "Call `ouroboros_job_wait` for an actionable announcement; a "
            "read-only `ouroboros_job_status` poll never claims or consumes it."
        )
    return (
        f"\n\nPending host dispatches: {len(pending)} — spawn one subagent per "
        "actionable entry in `meta.pending_host_dispatches`, giving it the entry's "
        "worker `prompt` and running it in the entry's "
        "`subagents[0].context.working_directory`. Then call "
        "`ouroboros_submit_fanout_results` with the entry's `fanout_id`, "
        "`session_id`, `correlation_key` = the entry's "
        "`result_correlation_key`, and results = "
        '[{"key": "result", "content": <the subagent\'s final output>}]. '
        "Each live dispatch is announced exactly once — spawn each `dispatch_id` "
        "once and never start it again on later polls. If an announcement or "
        "worker is lost, let the attempt reach its deadline; retry will issue a "
        "fresh dispatch id. Keep pumping ouroboros_job_wait afterwards."
    )
