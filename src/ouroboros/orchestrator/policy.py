"""Engine policy decisions derived from capability semantics."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ouroboros.orchestrator.capabilities import (
    CapabilityApprovalClass,
    CapabilityDescriptor,
    CapabilityGraph,
    CapabilityMutationClass,
)


class PolicySessionRole(StrEnum):
    """Supported engine-level session roles."""

    IMPLEMENTATION = "implementation"
    COORDINATOR = "coordinator"
    INTERVIEW = "interview"
    EVALUATION = "evaluation"


class PolicyExecutionPhase(StrEnum):
    """Supported execution phases for capability policy."""

    IMPLEMENTATION = "implementation"
    COORDINATOR_REVIEW = "coordinator_review"
    INTERVIEW = "interview"
    EVALUATION = "evaluation"


@dataclass(frozen=True, slots=True)
class PolicyContext:
    """Inputs for engine capability-policy evaluation."""

    runtime_backend: str | None
    session_role: PolicySessionRole
    execution_phase: PolicyExecutionPhase


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Engine decision for a single capability."""

    stable_id: str
    name: str
    visible: bool
    executable: bool
    approval_class: CapabilityApprovalClass
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class RoleCapabilityProfile:
    """Declarative envelope for a session role."""

    max_mutation_class: CapabilityMutationClass
    preferred_tool_names: tuple[str, ...] = ()
    allow_destructive: bool = False


_MUTATION_CLASS_ORDER = {
    CapabilityMutationClass.READ_ONLY: 0,
    CapabilityMutationClass.WORKSPACE_WRITE: 1,
    CapabilityMutationClass.EXTERNAL_SIDE_EFFECT: 2,
    CapabilityMutationClass.DESTRUCTIVE: 3,
}

_ROLE_PROFILES = {
    PolicySessionRole.IMPLEMENTATION: RoleCapabilityProfile(
        max_mutation_class=CapabilityMutationClass.DESTRUCTIVE,
        allow_destructive=True,
    ),
    PolicySessionRole.COORDINATOR: RoleCapabilityProfile(
        max_mutation_class=CapabilityMutationClass.EXTERNAL_SIDE_EFFECT,
        preferred_tool_names=("Read", "Bash", "Edit", "Grep", "Glob"),
    ),
    PolicySessionRole.INTERVIEW: RoleCapabilityProfile(
        max_mutation_class=CapabilityMutationClass.READ_ONLY,
        preferred_tool_names=("Read", "Grep", "Glob", "WebFetch", "WebSearch"),
    ),
    PolicySessionRole.EVALUATION: RoleCapabilityProfile(
        max_mutation_class=CapabilityMutationClass.READ_ONLY,
        preferred_tool_names=("Read", "Grep", "Glob", "WebFetch", "WebSearch"),
    ),
}


def _is_mutation_allowed(
    descriptor: CapabilityDescriptor,
    profile: RoleCapabilityProfile,
) -> bool:
    mutation_rank = _MUTATION_CLASS_ORDER[descriptor.semantics.mutation_class]
    if descriptor.semantics.mutation_class is CapabilityMutationClass.DESTRUCTIVE:
        return profile.allow_destructive
    return mutation_rank <= _MUTATION_CLASS_ORDER[profile.max_mutation_class]


def evaluate_capability_policy(
    graph: CapabilityGraph,
    context: PolicyContext,
) -> tuple[PolicyDecision, ...]:
    """Evaluate visible/executable capability decisions for a session role."""
    profile = _ROLE_PROFILES[context.session_role]
    decisions: list[PolicyDecision] = []

    for descriptor in graph.capabilities:
        reasons: list[str] = []
        visible = _is_mutation_allowed(descriptor, profile)
        executable = visible

        if visible and profile.preferred_tool_names:
            if descriptor.name not in profile.preferred_tool_names:
                visible = False
                executable = False
                reasons.append(
                    f"{context.session_role.value} profile does not include {descriptor.name}"
                )
        elif not visible:
            reasons.append(
                f"mutation_class {descriptor.semantics.mutation_class.value} exceeds "
                f"{context.session_role.value} policy"
            )

        decisions.append(
            PolicyDecision(
                stable_id=descriptor.stable_id,
                name=descriptor.name,
                visible=visible,
                executable=executable,
                approval_class=descriptor.semantics.approval_class,
                reasons=tuple(reasons),
            )
        )

    return tuple(decisions)


def allowed_capability_names(
    graph: CapabilityGraph,
    context: PolicyContext,
) -> list[str]:
    """Return executable capability names for a given policy context."""
    return [
        decision.name
        for decision in evaluate_capability_policy(graph, context)
        if decision.visible and decision.executable
    ]


__all__ = [
    "PolicyContext",
    "PolicyDecision",
    "PolicyExecutionPhase",
    "PolicySessionRole",
    "RoleCapabilityProfile",
    "allowed_capability_names",
    "evaluate_capability_policy",
]
