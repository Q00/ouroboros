"""Record verification that could not run without failing the work.

Verifier infrastructure is orthogonal to worker execution. An unavailable
verifier leaves a successful worker result successful, stores the unavailable
gate outcome, emits an operator-visible event, and never consumes retries.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from ouroboros.events.base import BaseEvent
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.parallel_executor_models import ACExecutionResult

if TYPE_CHECKING:
    from ouroboros.core.seed import AcceptanceCriterionSpec

VERIFY_UNVERIFIABLE_EVENT = "execution.verify.unverifiable"

log = get_logger(__name__)


def quarantine_reason(gate_reason: str | None) -> str:
    """Explain that verification, not the worker, was unavailable."""
    detail = f"{gate_reason} " if gate_reason else ""
    return f"Verification unavailable: {detail}work result requires confirmation."


def render_unverifiable_summary(display_indices: list[int]) -> str:
    """Render successful work whose machine verification was unavailable."""
    rendered = ", ".join(str(index) for index in display_indices)
    return (
        f"Succeeded without machine verification ({len(display_indices)}), "
        f"needs confirmation: AC {rendered}"
    )


async def quarantine_unverifiable_result(
    *,
    result: ACExecutionResult,
    spec: AcceptanceCriterionSpec,
    ac_content: str,
    ac_index: int,
    gate_reason: str | None,
    verify_gate_outcome: object,
    session_id: str,
    execution_id: str,
    emit_event: Callable[[BaseEvent], Awaitable[Any]],
) -> ACExecutionResult:
    """Record an unavailable verifier while preserving the worker result."""
    await emit_event(
        BaseEvent(
            type=VERIFY_UNVERIFIABLE_EVENT,
            aggregate_type="execution",
            aggregate_id=execution_id or session_id,
            data={
                "session_id": session_id,
                "execution_id": execution_id,
                "ac_index": ac_index,
                "ac_content": ac_content,
                "verify_command": spec.verify_command,
                "reason": gate_reason,
                "status": "unverified",
                "verify_cause": getattr(verify_gate_outcome, "cause", None),
            },
        )
    )
    log.warning(
        "parallel_executor.ac.verify_gate_unverifiable",
        session_id=session_id,
        ac_index=ac_index,
        reason=gate_reason,
    )
    return replace(result, verify_gate_outcome=verify_gate_outcome)
