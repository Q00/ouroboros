"""Capability-derived execution control-plane hints."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ouroboros.orchestrator.capabilities import (
    CapabilityApprovalClass,
    CapabilityGraph,
    CapabilityInterruptibility,
    CapabilityParallelSafety,
)
from ouroboros.orchestrator.policy import PolicyDecision


class ControlPlaneExecutionMode(StrEnum):
    """Execution-shape hints derived from capability semantics."""

    PARALLEL = "parallel"
    SERIALIZED = "serialized"
    ISOLATED = "isolated"


@dataclass(frozen=True, slots=True)
class CapabilityExecutionHint:
    """Control-plane hint for one capability."""

    stable_id: str
    name: str
    execution_mode: ControlPlaneExecutionMode
    interruptibility: CapabilityInterruptibility
    approval_class: CapabilityApprovalClass
    visible: bool
    executable: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ControlPlaneState:
    """Serialized engine control-plane state for a runtime session."""

    hints: tuple[CapabilityExecutionHint, ...] = field(default_factory=tuple)


def build_control_plane_state(
    graph: CapabilityGraph,
    decisions: tuple[PolicyDecision, ...],
) -> ControlPlaneState:
    """Build control-plane hints from capability semantics and policy decisions."""
    decisions_by_id = {decision.stable_id: decision for decision in decisions}
    hints: list[CapabilityExecutionHint] = []

    for descriptor in graph.capabilities:
        decision = decisions_by_id.get(descriptor.stable_id)
        if decision is None:
            continue
        if descriptor.semantics.parallel_safety is CapabilityParallelSafety.SAFE:
            mode = ControlPlaneExecutionMode.PARALLEL
        elif (
            descriptor.semantics.parallel_safety
            is CapabilityParallelSafety.ISOLATED_SESSION_REQUIRED
        ):
            mode = ControlPlaneExecutionMode.ISOLATED
        else:
            mode = ControlPlaneExecutionMode.SERIALIZED

        hints.append(
            CapabilityExecutionHint(
                stable_id=descriptor.stable_id,
                name=descriptor.name,
                execution_mode=mode,
                interruptibility=descriptor.semantics.interruptibility,
                approval_class=decision.approval_class,
                visible=decision.visible,
                executable=decision.executable,
                reasons=decision.reasons,
            )
        )

    return ControlPlaneState(hints=tuple(hints))


def serialize_control_plane_state(
    state: ControlPlaneState,
) -> list[dict[str, Any]]:
    """Serialize control-plane hints into JSON-safe metadata."""
    return [
        {
            "stable_id": hint.stable_id,
            "name": hint.name,
            "execution_mode": hint.execution_mode.value,
            "interruptibility": hint.interruptibility.value,
            "approval_class": hint.approval_class.value,
            "visible": hint.visible,
            "executable": hint.executable,
            "reasons": list(hint.reasons),
        }
        for hint in state.hints
    ]


__all__ = [
    "CapabilityExecutionHint",
    "ControlPlaneExecutionMode",
    "ControlPlaneState",
    "build_control_plane_state",
    "serialize_control_plane_state",
]
