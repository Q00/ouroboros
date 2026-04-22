"""Event factories for the Phase 2 Event Journal — directive emissions.

This module corresponds to the **Event Journal** layer in the Phase 2
Agent OS framing (RFC #476). Existing event categories — ``decomposition``,
``evaluation``, ``interview``, ``lineage``, ``ontology`` — capture *what*
was produced. This category captures *why* the run moved from one step
to the next, so the journal can answer both questions from the same
replayable source.

Target-oriented aggregation
---------------------------

Every ``control.directive.emitted`` event is aggregated by the object the
decision is *about*: ``(target_type, target_id)``. Using the targeted
aggregate — rather than a neutral ``"control"`` bucket — means existing
projectors that filter by aggregate (for example, a lineage projector
reading ``aggregate_type="lineage"``) naturally see the interleaved
directive stream alongside their state events. That is the difference
between *storing* a control event and *earning* a replayable decision
journal (per #476 maintainer feedback on #478).

Canonical target types:

- ``"session"``       — a whole orchestrator session
- ``"execution"``     — a specific execution/run
- ``"lineage"``       — a lineage chain (evolution loop)
- ``"agent_process"`` — Phase 3 agent process (forward-compatible)

The field is intentionally a string, not an enum, so new target types
(e.g. a future ``"agent_process"``) land without changing the event type
or the event schema.

Correlation fields (``session_id``, ``execution_id``, ``lineage_id``,
``generation_number``, ``phase``) are optional and stored in the payload
only when provided. They let projections filter directive streams by
additional axes without needing to join back to lineage state events.

Emission stance
---------------

This module is **observational-first**. It persists the event; no
emission site is wired, and no reactive consumer is added. Reactive
consumption via a ControlBus subscription surface is a separate, later
concern so the primitive stays stable while projections evolve. The TUI
lineage renderer is the intended first projection.

Event types:
    control.directive.emitted — a workflow site emitted a ``Directive``
"""

from __future__ import annotations

from typing import Any

from ouroboros.core.directive import Directive
from ouroboros.events.base import BaseEvent


def create_control_directive_emitted_event(
    target_type: str,
    target_id: str,
    emitted_by: str,
    directive: Directive,
    reason: str,
    *,
    session_id: str | None = None,
    execution_id: str | None = None,
    lineage_id: str | None = None,
    generation_number: int | None = None,
    phase: str | None = None,
    context_snapshot_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> BaseEvent:
    """Create an event recording a control-plane directive emission.

    The event is aggregated by ``(target_type, target_id)`` so that any
    projector filtering by a specific aggregate — e.g. a LineageProjector
    querying ``aggregate_type="lineage"`` and ``aggregate_id=<lineage_id>`` —
    naturally sees the interleaved directive stream alongside its state
    events, without having to read the control bucket separately.

    Args:
        target_type: What kind of object this decision is about.
            Canonical values: ``"session"``, ``"execution"``, ``"lineage"``,
            ``"agent_process"``. Kept as a free-form string so new target
            types land without a schema change.
        target_id: Identifier of the targeted object. Becomes
            ``aggregate_id`` on the stored event.
        emitted_by: Logical source — e.g. ``"evaluator"``, ``"evolver"``,
            ``"resilience.lateral"``. Free-form so new emission sites do
            not require a schema change.
        directive: The ``Directive`` being emitted.
        reason: Short human-readable rationale. Audit-level field; the
            structured source of truth for "why" remains the surrounding
            event lineage.
        session_id: Optional correlation into the owning session.
        execution_id: Optional correlation into a specific execution.
        lineage_id: Optional correlation into a lineage chain. For
            lineage-targeted events, typically equal to ``target_id``.
        generation_number: Optional lineage generation index.
        phase: Optional phase name (e.g. ``"wondering"``, ``"reflecting"``).
        context_snapshot_id: Optional reference to a context snapshot
            captured at emission time. Omitted from the payload when
            ``None`` to keep stored rows compact.
        extra: Optional forward-compatibility slot. Prefer promoting
            fields to named arguments as they stabilize.

    Returns:
        BaseEvent of type ``control.directive.emitted`` aggregated by
        ``(target_type, target_id)``.

    Example:
        event = create_control_directive_emitted_event(
            target_type="lineage",
            target_id="ralph-zepia-20260420-v3",
            emitted_by="evolver",
            directive=Directive.RETRY,
            reason="Reflect failed; retry budget remains.",
            lineage_id="ralph-zepia-20260420-v3",
            generation_number=2,
            phase="reflecting",
        )
    """
    data: dict[str, Any] = {
        "target_type": target_type,
        "target_id": target_id,
        "emitted_by": emitted_by,
        "directive": directive.value,
        "is_terminal": directive.is_terminal,
        "reason": reason,
    }
    # Optional correlation + context fields land in the payload only when
    # provided, so absence is distinguishable from an explicit None and
    # stored rows stay compact.
    if session_id is not None:
        data["session_id"] = session_id
    if execution_id is not None:
        data["execution_id"] = execution_id
    if lineage_id is not None:
        data["lineage_id"] = lineage_id
    if generation_number is not None:
        data["generation_number"] = generation_number
    if phase is not None:
        data["phase"] = phase
    if context_snapshot_id is not None:
        data["context_snapshot_id"] = context_snapshot_id
    if extra:
        data["extra"] = dict(extra)

    return BaseEvent(
        type="control.directive.emitted",
        aggregate_type=target_type,
        aggregate_id=target_id,
        data=data,
    )
