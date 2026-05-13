"""Typed Workflow IR for fat-harness execution planning.

This module introduces a small, repo-native intermediate representation
that the orchestrator can validate **before** dispatching work. The shape
is deliberately minimal and additive:

* :class:`WorkflowSpec` — a versioned envelope with a list of nodes and
  edges plus optional source metadata.
* :class:`WorkflowNode` — a typed unit of work owned by the harness, an
  agent, a plugin, a verifier, or a human gate.
* :class:`WorkflowEdge` — a typed transition between nodes (direct,
  conditional, fan-out, fan-in / barrier, terminal).
* :func:`validate_workflow` — a deterministic validator that rejects
  invalid graphs (dangling edges, duplicate node ids, unreachable
  terminal state, missing required evidence/output schemas) and returns a
  :class:`WorkflowValidationResult`.

The IR is a harness substrate, not a user-facing workflow product. It
does not import or depend on Microsoft Agent Framework or any external
workflow SDK; the goal is to learn the harness pattern, not adopt a
framework.

This PR contains *only* the schema and validator. Read-only adapters
from Seed/AC into Workflow IR — and the runtime wiring that consumes the
IR — land in follow-up PRs so this surface can be reviewed
independently. See issue #956 for the full design context.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)

WORKFLOW_IR_SCHEMA_VERSION = 1
"""Initial schema version for the Workflow IR."""


# ---------------------------------------------------------------------------
# Internal helpers — immutable metadata / hints + identifier hygiene
# ---------------------------------------------------------------------------


def _freeze_value(value: Any) -> Any:
    """Recursively copy common JSON-like containers into immutable views."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze_value(item) for item in value)
    return value


def _thaw_value(value: Any) -> Any:
    """Convert immutable container views back to JSON-serializable containers."""
    if isinstance(value, Mapping):
        return {key: _thaw_value(item) for key, item in value.items()}
    if isinstance(value, tuple | frozenset):
        return [_thaw_value(item) for item in value]
    return value


def _coerce_to_mapping(value: Any) -> Mapping[str, Any]:
    """Normalize incoming mapping-shaped input into a recursively frozen view."""
    if value is None:
        return MappingProxyType({})
    if isinstance(value, Mapping):
        frozen = _freeze_value(value)
        if isinstance(frozen, MappingProxyType):
            return frozen
    msg = f"mapping field must be a mapping, got {type(value).__name__}"
    raise ValueError(msg)


def _ensure_frozen_after(value: Any) -> Mapping[str, Any]:
    """Final-stage wrapper guaranteeing recursively immutable mapping values."""
    if isinstance(value, Mapping):
        frozen = _freeze_value(value)
        if isinstance(frozen, MappingProxyType):
            return frozen
    msg = f"mapping field must be a mapping, got {type(value).__name__}"
    raise ValueError(msg)


def _empty_frozen_mapping() -> Mapping[str, Any]:
    """Default factory that returns an empty read-only mapping."""
    return MappingProxyType({})


FrozenMapping = Annotated[
    Mapping[str, Any],
    BeforeValidator(_coerce_to_mapping),
    AfterValidator(_ensure_frozen_after),
    PlainSerializer(lambda value: _thaw_value(value), return_type=dict, when_used="always"),
]
"""Mapping field that blocks top-level mutation (``__setitem__`` etc.)."""


def _normalize_capability_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
    """Reject blank capability names while trimming surrounding whitespace."""
    normalized: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str):
            msg = f"capability name at index {index} must be a string; got {type(value).__name__}"
            raise TypeError(msg)
        stripped = value.strip()
        if not stripped:
            msg = f"capability name at index {index} is empty or whitespace-only"
            raise ValueError(msg)
        normalized.append(stripped)
    return tuple(normalized)


CapabilityTuple = Annotated[tuple[str, ...], AfterValidator(_normalize_capability_tuple)]
"""Capability-envelope tuple type that rejects blank entries."""


def _normalize_schema_ref(value: str) -> str:
    """Trim and reject blank schema-reference strings."""
    if not isinstance(value, str):
        msg = f"schema reference must be a string; got {type(value).__name__}"
        raise TypeError(msg)
    stripped = value.strip()
    if not stripped:
        msg = "schema reference must be non-blank"
        raise ValueError(msg)
    return stripped


def _has_schema_ref(value: str | None) -> bool:
    return isinstance(value, str) and bool(value.strip())


SchemaRef = Annotated[str, AfterValidator(_normalize_schema_ref)]
"""Schema-reference field type that rejects blank/whitespace-only refs."""


def _normalize_identifier(value: str) -> str:
    """Trim and reject blank workflow identifiers."""
    if not isinstance(value, str):
        msg = f"identifier must be a string; got {type(value).__name__}"
        raise TypeError(msg)
    stripped = value.strip()
    if not stripped:
        msg = "identifier must be non-blank"
        raise ValueError(msg)
    return stripped


Identifier = Annotated[str, AfterValidator(_normalize_identifier)]
"""Workflow identifier field type that trims and rejects blank values."""


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class NodeOwner(StrEnum):
    """Who executes a node when dispatched."""

    HARNESS = "harness"
    AGENT = "agent"
    PLUGIN = "plugin"
    HUMAN_GATE = "human_gate"
    VERIFIER = "verifier"


class NodeKind(StrEnum):
    """Discriminator for the kind of work a node represents.

    The set is small on purpose. New kinds are added in follow-up PRs as
    runtime support arrives; the IR itself does not assign semantics
    beyond carrying the discriminator.
    """

    TASK = "task"
    DECISION = "decision"
    FAN_OUT = "fan_out"
    FAN_IN = "fan_in"
    TERMINAL = "terminal"


class EdgeKind(StrEnum):
    """How an edge transitions between nodes."""

    DIRECT = "direct"
    CONDITIONAL = "conditional"
    FAN_OUT = "fan_out"
    FAN_IN = "fan_in"
    TERMINAL = "terminal"


class SourceKind(StrEnum):
    """Where a ``WorkflowSpec`` originated from."""

    SEED = "seed"
    PLUGIN = "plugin"
    FIRST_PARTY_PROGRAM = "first_party_program"
    SYNTHETIC = "synthetic"


# ---------------------------------------------------------------------------
# Model models
# ---------------------------------------------------------------------------


def _new_id(prefix: str) -> str:
    """Return a stable, prefixed identifier for IR nodes / specs."""
    return f"{prefix}_{uuid4().hex[:12]}"


class WorkflowNode(BaseModel, frozen=True):
    """A typed unit of work in the workflow graph.

    Attributes:
        node_id: Stable identifier; referenced by :class:`WorkflowEdge`.
        kind: Discriminator from :class:`NodeKind`.
        owner: Who executes the node (harness / agent / plugin / verifier
            / human gate).
        name: Short human-readable label for inspection tooling.
        input_schema_ref: Optional reference (URI or short id) to the
            input shape this node accepts. Required for agent / plugin
            owners that materialize work from an input payload; harness
            and human-gate nodes may omit it.
        evidence_schema_ref: Required when the node produces evidence (in
            practice: agent, plugin, verifier owners). Mirrors the typed
            evidence contract used downstream by the fat-harness loop.
        capability_envelope: Tuple of capability names the node may rely
            on; empty means no extra capabilities are requested.
        runtime_hints: Free-form hints for the runtime (model tier,
            timeouts, retry budgets). Hints are advisory; the runtime is
            authoritative.
        metadata: Free-form metadata bag; additive.
    """

    schema_version: int = Field(default=WORKFLOW_IR_SCHEMA_VERSION, ge=1)
    node_id: Identifier = Field(default_factory=lambda: _new_id("node"))
    kind: NodeKind
    owner: NodeOwner
    name: str = Field(default="", description="Short human-readable label")
    input_schema_ref: SchemaRef | None = Field(default=None)
    evidence_schema_ref: SchemaRef | None = Field(default=None)
    capability_envelope: CapabilityTuple = Field(default_factory=tuple)
    runtime_hints: FrozenMapping = Field(default_factory=_empty_frozen_mapping)
    metadata: FrozenMapping = Field(default_factory=_empty_frozen_mapping)

    @model_validator(mode="after")
    def _validate_schema_requirements(self) -> WorkflowNode:
        # Owners that produce evidence MUST declare an evidence_schema_ref.
        # Harness and human-gate nodes are exempt because they do not emit
        # evidence-bearing artifacts in the fat-harness loop.
        evidence_owners = {NodeOwner.AGENT, NodeOwner.PLUGIN, NodeOwner.VERIFIER}
        if self.owner in evidence_owners and not _has_schema_ref(self.evidence_schema_ref):
            msg = (
                f"WorkflowNode(owner={self.owner.value}) must declare "
                "evidence_schema_ref; see #956 validation rule "
                "'missing required evidence/output schema metadata'."
            )
            raise ValueError(msg)
        # Owners that consume a typed input payload MUST declare an
        # input_schema_ref so the harness can validate the payload shape
        # before dispatch. Verifier nodes are exempt because they consume
        # the evidence manifest itself rather than a free-form input.
        # Harness and human-gate nodes do not take a typed input.
        input_owners = {NodeOwner.AGENT, NodeOwner.PLUGIN}
        if self.owner in input_owners and not _has_schema_ref(self.input_schema_ref):
            msg = (
                f"WorkflowNode(owner={self.owner.value}) must declare "
                "input_schema_ref; the harness must validate the payload "
                "shape before dispatching agent/plugin work."
            )
            raise ValueError(msg)
        return self


class WorkflowEdge(BaseModel, frozen=True):
    """A typed transition between two nodes.

    Attributes:
        edge_id: Stable identifier for the edge.
        source: Source ``node_id``.
        target: Target ``node_id``.
        kind: Discriminator from :class:`EdgeKind`.
        condition: Optional structured condition payload evaluated by the
            harness for ``CONDITIONAL`` edges. The shape is opaque to the
            IR; concrete condition grammars are owned by the runtime
            evaluator.
        metadata: Free-form metadata bag; additive.
    """

    schema_version: int = Field(default=WORKFLOW_IR_SCHEMA_VERSION, ge=1)
    edge_id: Identifier = Field(default_factory=lambda: _new_id("edge"))
    source: Identifier
    target: Identifier
    kind: EdgeKind = Field(default=EdgeKind.DIRECT)
    condition: FrozenMapping | None = Field(default=None)
    metadata: FrozenMapping = Field(default_factory=_empty_frozen_mapping)

    @field_validator("source", "target")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            msg = "WorkflowEdge endpoints must be non-empty node ids"
            raise ValueError(msg)
        return normalized

    @model_validator(mode="after")
    def _validate_edge_contract(self) -> WorkflowEdge:
        if self.source == self.target:
            msg = (
                f"WorkflowEdge {self.edge_id} forms a self-loop "
                f"({self.source} -> {self.target}); use a barrier or "
                "explicit cycle structure instead."
            )
            raise ValueError(msg)
        if self.kind is EdgeKind.CONDITIONAL and not self.condition:
            msg = "WorkflowEdge(kind=conditional) must include a condition payload"
            raise ValueError(msg)
        return self


class WorkflowSpec(BaseModel, frozen=True):
    """A complete typed workflow graph.

    Attributes:
        spec_id: Stable identifier for the workflow instance.
        source: Where the spec originated from (Seed projection, plugin,
            first-party program, synthetic test).
        source_ref: Optional reference to the originating artifact (a
            ``seed_id``, plugin id, etc.).
        nodes: Tuple of :class:`WorkflowNode` instances.
        edges: Tuple of :class:`WorkflowEdge` instances.
        metadata: Free-form metadata bag; additive.
    """

    schema_version: int = Field(default=WORKFLOW_IR_SCHEMA_VERSION, ge=1)
    spec_id: Identifier = Field(default_factory=lambda: _new_id("wfspec"))
    source: SourceKind
    source_ref: str | None = Field(default=None)
    nodes: tuple[WorkflowNode, ...] = Field(default_factory=tuple)
    edges: tuple[WorkflowEdge, ...] = Field(default_factory=tuple)
    metadata: FrozenMapping = Field(default_factory=_empty_frozen_mapping)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class WorkflowValidationError(BaseModel, frozen=True):
    """One concrete validation problem detected in a :class:`WorkflowSpec`.

    The validator collects all problems into a
    :class:`WorkflowValidationResult` rather than raising on the first
    issue so callers can surface a complete report.

    Attributes:
        code: Short stable string identifying the rule.
        message: Human-readable explanation suitable for CLI output.
        node_id: Optional node identifier the error refers to.
        edge_id: Optional edge identifier the error refers to.
    """

    code: Literal[
        "duplicate_node_id",
        "duplicate_edge_id",
        "dangling_edge",
        "unreachable_terminal",
        "no_terminal_node",
        "missing_evidence_schema",
        "missing_input_schema",
        "missing_condition",
        "isolated_node",
    ]
    message: str
    node_id: str | None = None
    edge_id: str | None = None


class WorkflowValidationResult(BaseModel, frozen=True):
    """Result of validating a :class:`WorkflowSpec`.

    ``ok`` is True iff ``errors`` is empty. ``warnings`` carries
    non-fatal observations (e.g., isolated nodes that are reachable but
    contribute nothing).
    """

    ok: bool
    errors: tuple[WorkflowValidationError, ...] = Field(default_factory=tuple)
    warnings: tuple[WorkflowValidationError, ...] = Field(default_factory=tuple)


def validate_workflow(spec: WorkflowSpec) -> WorkflowValidationResult:
    """Validate a workflow specification.

    The validator enforces the rules listed in #956:

    * No duplicate node ids.
    * No duplicate edge ids.
    * No dangling edges (every edge endpoint must reference a known
      node).
    * At least one ``TERMINAL`` node exists.
    * Every ``TERMINAL`` node is reachable from at least one node, or is
      itself the only node.
    * Nodes that own evidence (agent / plugin / verifier) declare an
      ``evidence_schema_ref``. This duplicates the per-node validator so
      a fully-constructed spec is independently checkable from
      potentially-mutated dict input.

    Warnings (non-fatal):

    * Isolated nodes: nodes with no incident edges that are also not
      ``TERMINAL`` are surfaced as warnings.

    Args:
        spec: The workflow specification to validate.

    Returns:
        A :class:`WorkflowValidationResult` containing the verdict and
        the full list of errors and warnings.
    """
    errors: list[WorkflowValidationError] = []
    warnings: list[WorkflowValidationError] = []

    # 1. Duplicate node ids.
    seen_node_ids: set[str] = set()
    for node in spec.nodes:
        if node.node_id in seen_node_ids:
            errors.append(
                WorkflowValidationError(
                    code="duplicate_node_id",
                    message=f"Duplicate node id '{node.node_id}'.",
                    node_id=node.node_id,
                )
            )
        else:
            seen_node_ids.add(node.node_id)

    # 2. Missing evidence/input schemas (idempotent with per-node validator).
    evidence_owners = {NodeOwner.AGENT, NodeOwner.PLUGIN, NodeOwner.VERIFIER}
    input_owners = {NodeOwner.AGENT, NodeOwner.PLUGIN}
    for node in spec.nodes:
        if node.owner in evidence_owners and not _has_schema_ref(node.evidence_schema_ref):
            errors.append(
                WorkflowValidationError(
                    code="missing_evidence_schema",
                    message=(
                        f"Node '{node.node_id}' (owner={node.owner.value}) is "
                        "missing evidence_schema_ref."
                    ),
                    node_id=node.node_id,
                )
            )
        if node.owner in input_owners and not _has_schema_ref(node.input_schema_ref):
            errors.append(
                WorkflowValidationError(
                    code="missing_input_schema",
                    message=(
                        f"Node '{node.node_id}' (owner={node.owner.value}) is "
                        "missing input_schema_ref; agent/plugin nodes must "
                        "declare the payload contract before dispatch."
                    ),
                    node_id=node.node_id,
                )
            )

    # 3. Edges: duplicate ids, dangling endpoints.
    seen_edge_ids: set[str] = set()
    for edge in spec.edges:
        if edge.edge_id in seen_edge_ids:
            errors.append(
                WorkflowValidationError(
                    code="duplicate_edge_id",
                    message=f"Duplicate edge id '{edge.edge_id}'.",
                    edge_id=edge.edge_id,
                )
            )
        else:
            seen_edge_ids.add(edge.edge_id)
        if edge.kind is EdgeKind.CONDITIONAL and not edge.condition:
            errors.append(
                WorkflowValidationError(
                    code="missing_condition",
                    message=(
                        f"Conditional edge '{edge.edge_id}' must include a condition payload."
                    ),
                    edge_id=edge.edge_id,
                )
            )
        for endpoint, role in ((edge.source, "source"), (edge.target, "target")):
            if endpoint not in seen_node_ids:
                errors.append(
                    WorkflowValidationError(
                        code="dangling_edge",
                        message=(
                            f"Edge '{edge.edge_id}' references unknown {role} node '{endpoint}'."
                        ),
                        edge_id=edge.edge_id,
                        node_id=endpoint,
                    )
                )

    # 4. Terminal coverage and reachability.
    terminal_nodes = tuple(n for n in spec.nodes if n.kind is NodeKind.TERMINAL)
    if not terminal_nodes:
        errors.append(
            WorkflowValidationError(
                code="no_terminal_node",
                message="WorkflowSpec must contain at least one terminal node.",
            )
        )
    else:
        reachable = _compute_reachable(spec)
        for terminal in terminal_nodes:
            if terminal.node_id not in reachable:
                errors.append(
                    WorkflowValidationError(
                        code="unreachable_terminal",
                        message=(
                            f"Terminal node '{terminal.node_id}' is unreachable "
                            "from any non-terminal node."
                        ),
                        node_id=terminal.node_id,
                    )
                )
        terminal_reachable_from = _compute_nodes_that_can_reach_terminal(spec)
        for node in spec.nodes:
            if node.kind is NodeKind.TERMINAL:
                continue
            if node.node_id not in terminal_reachable_from:
                errors.append(
                    WorkflowValidationError(
                        code="unreachable_terminal",
                        message=(
                            f"Node '{node.node_id}' has no path to a terminal node; "
                            "all executable branches must be able to terminate."
                        ),
                        node_id=node.node_id,
                    )
                )

    # 5. Warnings — isolated non-terminal nodes (degree 0).
    incident_nodes: set[str] = set()
    for edge in spec.edges:
        incident_nodes.add(edge.source)
        incident_nodes.add(edge.target)
    for node in spec.nodes:
        if node.kind is NodeKind.TERMINAL:
            continue
        if len(spec.nodes) == 1:
            # no_terminal_node is already emitted for this degenerate stub.
            continue
        if node.node_id not in incident_nodes:
            warnings.append(
                WorkflowValidationError(
                    code="isolated_node",
                    message=(f"Node '{node.node_id}' is isolated (no incident edges)."),
                    node_id=node.node_id,
                )
            )

    return WorkflowValidationResult(
        ok=not errors,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def _compute_reachable(spec: WorkflowSpec) -> set[str]:
    """Return the set of nodes reachable from any non-terminal node.

    The reachability check is intentionally lenient: terminal nodes are
    reachable iff at least one non-terminal node leads to them through
    the graph. A spec consisting of a single terminal node fails the
    ``no_terminal_node`` check earlier in :func:`validate_workflow` only
    when no terminals exist; a sole terminal is treated as reachable.
    """
    non_terminal_starts = {
        node.node_id for node in spec.nodes if node.kind is not NodeKind.TERMINAL
    }
    if not non_terminal_starts:
        # Sole-terminal degenerate case: treat the terminal as reachable so
        # we do not over-flag minimal stubs used by tests.
        return {node.node_id for node in spec.nodes}

    adjacency: dict[str, list[str]] = {}
    for edge in spec.edges:
        adjacency.setdefault(edge.source, []).append(edge.target)

    visited: set[str] = set()
    stack = list(non_terminal_starts)
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        for neighbor in adjacency.get(current, ()):
            if neighbor not in visited:
                stack.append(neighbor)
    return visited


def _compute_nodes_that_can_reach_terminal(spec: WorkflowSpec) -> set[str]:
    """Return nodes with at least one directed path to a terminal node."""
    terminal_ids = {node.node_id for node in spec.nodes if node.kind is NodeKind.TERMINAL}
    reverse_adjacency: dict[str, list[str]] = {}
    for edge in spec.edges:
        reverse_adjacency.setdefault(edge.target, []).append(edge.source)

    visited: set[str] = set()
    stack = list(terminal_ids)
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        for predecessor in reverse_adjacency.get(current, ()):  # pragma: no branch
            if predecessor not in visited:
                stack.append(predecessor)
    return visited


__all__ = [
    "WORKFLOW_IR_SCHEMA_VERSION",
    "CapabilityTuple",
    "EdgeKind",
    "FrozenMapping",
    "Identifier",
    "NodeKind",
    "NodeOwner",
    "SchemaRef",
    "SourceKind",
    "WorkflowEdge",
    "WorkflowNode",
    "WorkflowSpec",
    "WorkflowValidationError",
    "WorkflowValidationResult",
    "validate_workflow",
]
