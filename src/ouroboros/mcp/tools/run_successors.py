"""One place that resolves the run job's successor chain.

A terminal ``execute_seed`` run enqueues a formal evaluation, and a rejected
verdict enqueues a bounded Ralph loop. That behaviour is not a property of the
run handler — it is a property of the *dependencies* the run handler was built
with, and every link degrades **silently** when its dependency is missing:

* ``StartExecuteSeedHandler`` without ``start_evaluate_handler`` terminates the
  run at ``evaluation_status="enqueue_failed"``;
* ``StartEvaluateHandler`` without ``start_ralph_handler`` reports
  ``chained_ralph_skipped`` one link further down;
* a hand-built ``StartRalphHandler`` gets an ``EvolveStepHandler`` with no
  ``EvolutionaryLoop``, so the Ralph job *is* enqueued and then dies on its
  first generation with ``EvolutionaryLoop not configured``.

The last one is why this module resolves the stack from the MCP composition
root instead of assembling it here. ``create_ouroboros_server`` is the only
place that wires the evolutionary loop's executor, evaluator, validator and
rewind observer, so any hand-rolled copy is a chain that looks complete and
is not — the same trap ``_build_configured_ralph_handler`` in the CLI already
documents for the complete-product path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ouroboros.mcp.tools.evaluation_handlers import StartEvaluateHandler

if TYPE_CHECKING:
    from ouroboros.mcp.job_manager import JobManager

_START_EVALUATE_TOOL = "ouroboros_start_evaluate"


def build_run_successor_handler(
    *,
    agent_runtime_backend: str | None = None,
    opencode_mode: str | None = None,
) -> StartEvaluateHandler:
    """Return the composition root's fully wired evaluate → ralph successor owner.

    Imported lazily: this module is reached from composition roots, and the
    server module pulls in the full pipeline graph.
    """
    from ouroboros.mcp.server.adapter import create_ouroboros_server

    server = create_ouroboros_server(
        runtime_backend=agent_runtime_backend,
        opencode_mode=opencode_mode,
    )
    handler = server._tool_handlers[_START_EVALUATE_TOOL]  # noqa: SLF001
    if not isinstance(handler, StartEvaluateHandler):
        msg = f"MCP composition root returned non-evaluate handler for {_START_EVALUATE_TOOL}"
        raise TypeError(msg)
    return handler


def resolve_run_successor_handler(
    existing: Any,
    execute_handler: Any,
    job_manager: JobManager | None = None,  # noqa: ARG001 - kept for call-site symmetry
) -> StartEvaluateHandler:
    """Carry an already-wired successor owner across a run-handler rebuild.

    A composition root that rebuilds a run handler for a different runtime must
    not drop the stack the original was given; only resolve a fresh one when
    there is nothing to carry. Backends are read from the rebuilt execute
    handler so the successors judge on the runtime the run actually used.
    """
    carried = getattr(existing, "start_evaluate_handler", None)
    if carried is not None:
        return carried
    return build_run_successor_handler(
        agent_runtime_backend=getattr(execute_handler, "agent_runtime_backend", None),
        opencode_mode=getattr(execute_handler, "opencode_mode", None),
    )
