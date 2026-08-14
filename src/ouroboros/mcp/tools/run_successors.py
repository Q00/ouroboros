"""One place that wires the run job's successor chain.

A terminal ``execute_seed`` run enqueues a formal evaluation, and a rejected
verdict enqueues a bounded Ralph loop. That behaviour is not a property of the
run handler — it is a property of the *dependencies* the run handler was built
with. ``StartExecuteSeedHandler`` without a ``start_evaluate_handler`` silently
degrades to ``evaluation_status="enqueue_failed"``, and a ``StartEvaluateHandler``
without a ``start_ralph_handler`` degrades the same way one link further down.

Every composition root that builds a run handler therefore has to assemble the
same stack, and any that forgets a link produces a run nobody grades. That is
exactly how ``ooo auto`` ended up starting runs whose successors could never be
enqueued while the shipped stdio server wired them correctly.

This module owns that assembly so the composition roots can share one
definition instead of each keeping a partial copy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ouroboros.mcp.tools.evaluation_handlers import EvaluateHandler, StartEvaluateHandler
from ouroboros.mcp.tools.ralph_handlers import StartRalphHandler

if TYPE_CHECKING:
    from typing import Any

    from ouroboros.mcp.job_manager import JobManager
    from ouroboros.mcp.tools.seed_handoff import SeedHandoffRegistry
    from ouroboros.persistence.event_store import EventStore


def resolve_run_successor_handler(
    existing: Any,
    execute_handler: Any,
    job_manager: JobManager | None = None,
) -> StartEvaluateHandler:
    """Carry an already-wired successor owner across a run-handler rebuild.

    A composition root that rebuilds a run handler for a different runtime must
    not drop the stack the original was given; only build a fresh one when
    there is nothing to carry. Backends are read from the rebuilt execute
    handler so the successors judge on the same runtime the run used.
    """
    carried = getattr(existing, "start_evaluate_handler", None)
    if carried is not None:
        return carried
    return build_run_successor_handler(
        event_store=getattr(execute_handler, "event_store", None),
        job_manager=job_manager,
        llm_backend=getattr(execute_handler, "llm_backend", None),
        agent_runtime_backend=getattr(execute_handler, "agent_runtime_backend", None),
        opencode_mode=getattr(execute_handler, "opencode_mode", None),
    )


def build_run_successor_handler(
    *,
    event_store: EventStore | None = None,
    job_manager: JobManager | None = None,
    llm_backend: str | None = None,
    agent_runtime_backend: str | None = None,
    opencode_mode: str | None = None,
    seed_handoff_registry: SeedHandoffRegistry | None = None,
) -> StartEvaluateHandler:
    """Build the evaluate → ralph successor owner for a run handler.

    ``opencode_mode`` is deliberately **not** forwarded to the Ralph link.
    Automatic convergence is owned by the parent even in passive plugin mode, so
    its private Ralph surface must enqueue a real pollable job rather than emit
    a second plugin delegation envelope — mirroring the shipped stdio server.
    """
    return StartEvaluateHandler(
        evaluate_handler=EvaluateHandler(
            event_store=event_store,
            llm_backend=llm_backend,
            agent_runtime_backend=agent_runtime_backend,
            opencode_mode=opencode_mode,
        ),
        event_store=event_store,
        job_manager=job_manager,
        llm_backend=llm_backend,
        agent_runtime_backend=agent_runtime_backend,
        opencode_mode=opencode_mode,
        start_ralph_handler=StartRalphHandler(
            event_store=event_store,
            job_manager=job_manager,
            agent_runtime_backend=agent_runtime_backend,
            opencode_mode=None,
        ),
        seed_handoff_registry=seed_handoff_registry,
    )
