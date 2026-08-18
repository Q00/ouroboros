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


def pending_host_dispatches(bridge: Any | None, snapshot: Any) -> list[dict[str, Any]]:
    """Return open host-execution dispatches correlated to this job's session.

    Terminal jobs list nothing: a dispatch that outlived its job is stale by
    definition and surfacing it would send the host to work for a run that can
    no longer accept the result.
    """
    if bridge is None or snapshot.is_terminal:
        return []
    session_id = getattr(snapshot.links, "session_id", None)
    try:
        return list(bridge.pending_for_session(session_id))
    except Exception:  # pragma: no cover - defensive: never break job polling
        log.warning("mcp.tool.job.pending_host_dispatches_failed", exc_info=True)
        return []


def pending_host_dispatches_meta_field(pending: list[dict[str, Any]]) -> dict[str, Any]:
    """Meta-dict fragment to splice via ``**`` — empty when there is nothing pending."""
    return {"pending_host_dispatches": pending} if pending else {}


def pending_host_dispatch_suffix(pending: list[dict[str, Any]]) -> str:
    return (
        f"\n\nPending host dispatches: {len(pending)} — spawn one subagent per "
        "entry in `meta.pending_host_dispatches`, giving it the entry's worker "
        "`prompt` and running it in the entry's `subagents[0].context."
        "working_directory`. Then call `ouroboros_submit_fanout_results` with "
        "the entry's `fanout_id`, `session_id`, `correlation_key` = the "
        "entry's `result_correlation_key`, and results = "
        '[{"key": "result", "content": <the subagent\'s final output>}]. '
        "Each dispatch is announced once — spawn each `dispatch_id` exactly "
        "once, never again on later polls (a `reannounce: true` entry means "
        "your earlier worker was lost; spawn again only then). Keep pumping "
        "ouroboros_job_wait afterwards."
    )
