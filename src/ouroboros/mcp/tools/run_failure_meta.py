"""Attach a closed failure cause to a failed/cancelled ``execute_seed`` result.

Reads only the durable, machine-readable evidence the executor already wrote
(verify-gate causes, retry exhaustion, judged outcomes, the audited session
error type) — never prose — and folds it through
``orchestrator.run_failure_cause`` so the MCP result meta, and from there the
``workflow_outcome`` telemetry event, can say why a run failed.
"""

from __future__ import annotations

from typing import Any

import structlog

from ouroboros.orchestrator.run_failure_cause import (
    UNKNOWN_RUN_FAILURE_CAUSE,
    derive_run_failure_cause,
    failure_reason_code_for_run_cause,
)
from ouroboros.orchestrator.session import SessionStatus
from ouroboros.persistence.event_store import EventStore

log = structlog.get_logger(__name__)

_RUN_FAILURE_EVIDENCE_EVENT_TYPES = (
    "execution.verify.failed",
    "execution.ac.recovery_exhausted",
    "execution.ac.attempt_judged",
)
_RUN_FAILURE_EVIDENCE_LIMIT = 5000


async def derive_run_failure_meta(
    event_store: EventStore,
    *,
    session_id: str,
    execution_id: str,
    session_status: SessionStatus | None,
) -> dict[str, Any]:
    """Attach a closed failure cause to a failed/cancelled run's result meta.

    Reads only the durable, machine-readable evidence the executor already
    wrote (verify-gate causes, retry exhaustion, judged outcomes, the audited
    session error type) — never prose. Best-effort: an unreadable store yields
    ``unknown`` rather than a failed tool result.
    """
    terminal_status = session_status.value if session_status is not None else None
    cause = UNKNOWN_RUN_FAILURE_CAUSE
    try:
        events: list[Any] = []
        for event_type in _RUN_FAILURE_EVIDENCE_EVENT_TYPES:
            events.extend(
                await event_store.query_events(
                    aggregate_id=execution_id,
                    event_type=event_type,
                    limit=_RUN_FAILURE_EVIDENCE_LIMIT,
                )
            )
        events.extend(
            await event_store.query_events(
                aggregate_id=session_id,
                event_type="orchestrator.session.failed",
                limit=_RUN_FAILURE_EVIDENCE_LIMIT,
            )
        )
        cause = derive_run_failure_cause(
            events, session_id=session_id, terminal_status=terminal_status
        )
    except Exception:
        log.warning(
            "mcp.tool.execute_seed.failure_cause_unavailable",
            session_id=session_id,
            execution_id=execution_id,
        )
    return {
        "failure_cause": cause,
        "failure_reason_code": failure_reason_code_for_run_cause(cause).value,
    }


__all__ = ["derive_run_failure_meta"]
