"""Event factories for control-plane directive emissions.

This module provides the factory for persisting control-plane decisions
(continue / evaluate / evolve / unstuck / retry / compact / wait / cancel /
converge) to the EventStore. Existing event categories (decomposition,
evaluation, interview, lineage, ontology) capture data causality — what was
produced — but not decision causality — *why* the run moved from one step to
the next. This module adds the missing category without modifying existing
emission sites.

Event Types:
    control.directive.emitted - A workflow site emitted a Directive

Follow-up changes will wire this factory into individual decision sites so
the store contains a full directive timeline. This module adds only the
factory and its tests; no production emission is added here.
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
