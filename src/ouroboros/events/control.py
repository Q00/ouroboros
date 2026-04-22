"""Event factories for the Phase 2 Event Journal — directive emissions.

This module corresponds to the **Event Journal** layer in the Phase 2
Agent OS framing (RFC #476). Existing event categories — ``decomposition``,
``evaluation``, ``interview``, ``lineage``, ``ontology`` — capture *what*
was produced. This category captures *why* the run moved from one step
to the next, so the journal can answer both questions from the same
replayable source.

The Event Journal is the causal source of truth. Every control-plane
decision persists here before any live projection observes it. Consumers
that later react to directives (TUI lineage timelines, drift monitors,
future ControlBus subscribers) are projections of these events — they
do not decide anything the journal does not already record.

Event types:
    control.directive.emitted — a workflow site emitted a ``Directive``

Emission stance:

This PR is **observational-first**. It persists the event; no emission
site is wired, and no reactive consumer is added. Reactive consumption
via a ControlBus subscription surface is a separate, later concern so
the primitive stays stable while projections evolve. The TUI lineage
renderer is the intended first projection.

Payload shape:

- ``emitted_by``   — logical source, e.g. ``"evaluator"``, ``"evolver"``,
                     ``"resilience.lateral"``. Free-form to keep new
                     emission sites from requiring a schema change.
- ``directive``    — the ``Directive`` member's string value, so downstream
                     consumers classify events without importing the enum.
- ``is_terminal``  — denormalized terminality flag, for the same reason.
- ``reason``       — short audit rationale; the structured source of
                     truth for "why" remains the surrounding lineage.
- ``context_snapshot_id`` — optional link into the context snapshot
                     captured at emission; omitted when absent so stored
                     rows stay compact.
- ``extra``        — optional forward-compatibility slot; omitted when
                     unused. Prefer promoting fields to named arguments
                     over expanding ``extra`` in the long run.

This module adds only the factory and its unit tests; follow-up changes
wire it into individual decision sites one at a time.
"""

from __future__ import annotations

from typing import Any

from ouroboros.core.directive import Directive
from ouroboros.events.base import BaseEvent


def create_control_directive_emitted_event(
    execution_id: str,
    emitted_by: str,
    directive: Directive,
    reason: str,
    context_snapshot_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> BaseEvent:
    """Create an event recording a control-plane directive emission.

    Args:
        execution_id: Identifier of the execution the directive belongs to.
            Used as the aggregate id so the directive timeline of a single
            run can be reconstructed by aggregate-id query.
        emitted_by: Logical source of the directive, e.g., ``"evaluator"``,
            ``"evolver"``, ``"resilience.lateral"``. Free-form so new
            emission sites do not require schema changes.
        directive: The Directive being emitted.
        reason: Short human-readable rationale. The *structured* source of
            truth for "why" is the surrounding event lineage; this field is
            intended for audit and debugging, not for programmatic routing.
        context_snapshot_id: Optional reference to a context snapshot
            captured at emission time. ``None`` when the emission site has
            no relevant snapshot to link.
        extra: Optional additional key-value pairs to include in the payload.
            Intended for forward-compatibility during the migration; if a
            callers needs a new structured field, prefer adding it to this
            factory's signature rather than through ``extra``.

    Returns:
        BaseEvent of type ``control.directive.emitted``.

    Example:
        event = create_control_directive_emitted_event(
            execution_id="exec_123",
            emitted_by="evaluator",
            directive=Directive.RETRY,
            reason="Stage 1 mechanical checks failed; retry budget remains.",
        )
    """
    data: dict[str, Any] = {
        "emitted_by": emitted_by,
        "directive": directive.value,
        "is_terminal": directive.is_terminal,
        "reason": reason,
    }
    if context_snapshot_id is not None:
        data["context_snapshot_id"] = context_snapshot_id
    if extra:
        data["extra"] = dict(extra)

    return BaseEvent(
        type="control.directive.emitted",
        aggregate_type="control",
        aggregate_id=execution_id,
        data=data,
    )
