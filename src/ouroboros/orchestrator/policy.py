"""Engine policy decisions derived from capability semantics."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ouroboros.orchestrator.capabilities import (
    CapabilityApprovalClass,
    CapabilityDescriptor,
    CapabilityGraph,
    CapabilityMutationClass,
    CapabilityOrigin,
    CapabilityScope,
    build_capability_graph,
)
from ouroboros.orchestrator.mcp_tools import (
    assemble_session_tool_catalog,
    enumerate_runtime_builtin_tool_definitions,
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
    """Inputs for engine capability-policy evaluation.

    Only ``session_role`` drives the decision today; ``runtime_backend``
    and ``execution_phase`` are persisted in the
    ``policy.capabilities.evaluated`` audit event so replay and
    debugging can attribute a decision to a specific backend/phase.
    These fields exist as forward-compatibility hooks for
    provider-specific or phase-specific policy branching; they are
    deliberately part of the contract now so downstream consumers do
    not have to migrate schemas when that branching lands.
    """

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
    allowed_origins: tuple[CapabilityOrigin, ...] = ()
    allowed_scopes: tuple[CapabilityScope, ...] = ()
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
        allowed_origins=(CapabilityOrigin.PROVIDER_NATIVE, CapabilityOrigin.FUTURE_RUNTIME),
        allowed_scopes=(
            CapabilityScope.KERNEL,
            CapabilityScope.SIDECAR,
            CapabilityScope.SHELL_ONLY,
        ),
    ),
    PolicySessionRole.INTERVIEW: RoleCapabilityProfile(
        max_mutation_class=CapabilityMutationClass.READ_ONLY,
        preferred_tool_names=("Read", "Grep", "Glob", "WebFetch", "WebSearch"),
        allowed_origins=(CapabilityOrigin.PROVIDER_NATIVE, CapabilityOrigin.FUTURE_RUNTIME),
        allowed_scopes=(CapabilityScope.KERNEL, CapabilityScope.SIDECAR),
    ),
    PolicySessionRole.EVALUATION: RoleCapabilityProfile(
        max_mutation_class=CapabilityMutationClass.READ_ONLY,
        preferred_tool_names=("Read", "Grep", "Glob", "WebFetch", "WebSearch"),
        allowed_origins=(CapabilityOrigin.PROVIDER_NATIVE, CapabilityOrigin.FUTURE_RUNTIME),
        allowed_scopes=(CapabilityScope.KERNEL, CapabilityScope.SIDECAR),
    ),
}

_NON_EXECUTABLE_SOURCE_KINDS = frozenset({"inherited_capability"})


def _is_mutation_allowed(
    descriptor: CapabilityDescriptor,
    profile: RoleCapabilityProfile,
) -> bool:
    mutation_rank = _MUTATION_CLASS_ORDER[descriptor.semantics.mutation_class]
    if descriptor.semantics.mutation_class is CapabilityMutationClass.DESTRUCTIVE:
        return profile.allow_destructive
    return mutation_rank <= _MUTATION_CLASS_ORDER[profile.max_mutation_class]


def _matches_role_selector(
    descriptor: CapabilityDescriptor,
    profile: RoleCapabilityProfile,
) -> bool:
    """Does ``descriptor`` satisfy the profile's admission selectors?

    Selectors are combined as OR of two independent clauses, by design:

    1. ``preferred_tool_names`` — explicit name allowlist for the
       baseline built-in envelope (e.g., ``Read`` always admitted for
       INTERVIEW even though its origin is BUILTIN, not PROVIDER_NATIVE).
    2. ``allowed_origins`` AND ``allowed_scopes`` — semantic-class
       admission, used by read-only roles to accept provider-native
       capabilities without enumerating every tool name.

    The OR is intentional: an explicit name whitelist is a stronger
    signal of "this is definitely in the envelope" than origin/scope
    alignment.  If you want to restrict a named tool to a specific
    origin/scope, remove the name from ``preferred_tool_names`` and
    rely on the semantic clause instead.
    """
    if not (profile.preferred_tool_names or profile.allowed_origins or profile.allowed_scopes):
        return True

    if descriptor.name in profile.preferred_tool_names:
        return True

    origin_matches = (
        not profile.allowed_origins or descriptor.semantics.origin in profile.allowed_origins
    )
    scope_matches = (
        not profile.allowed_scopes or descriptor.semantics.scope in profile.allowed_scopes
    )
    return origin_matches and scope_matches


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

        if visible and not _matches_role_selector(descriptor, profile):
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

        if descriptor.source_kind in _NON_EXECUTABLE_SOURCE_KINDS:
            executable = False
            reasons.append(
                f"{descriptor.source_kind} requires live provider discovery before execution"
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


def allowed_runtime_builtin_tool_names(
    context: PolicyContext,
    *,
    builtin_tools: tuple[str, ...] | None = None,
) -> list[str]:
    """Return executable built-in runtime tools for a policy context."""
    tool_names = builtin_tools or tuple(
        definition.name for definition in enumerate_runtime_builtin_tool_definitions()
    )
    graph = build_capability_graph(assemble_session_tool_catalog(tool_names))
    return allowed_capability_names(graph, context)


__all__ = [
    "PolicyContext",
    "PolicyDecision",
    "PolicyExecutionPhase",
    "PolicySessionRole",
    "RoleCapabilityProfile",
    "allowed_capability_names",
    "allowed_runtime_builtin_tool_names",
    "evaluate_capability_policy",
]
