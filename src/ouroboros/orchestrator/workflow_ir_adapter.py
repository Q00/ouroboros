"""Read-only adapters into the typed Workflow IR.

This module owns the narrow #956 PR-2 boundary: translate today's immutable
``Seed`` shape into a validated ``WorkflowSpec`` without changing Seed schema,
runtime dispatch, persistence, or projection records.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ouroboros.core.seed import Seed
from ouroboros.orchestrator.workflow_ir import (
    EdgeKind,
    NodeKind,
    NodeOwner,
    SourceKind,
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
    validate_workflow,
)

DEFAULT_SEED_AC_INPUT_SCHEMA_REF = "ouroboros://schemas/seed-acceptance-criterion-input/v1"
"""Canonical input-schema reference used for current string AC dispatch nodes."""

DEFAULT_SEED_AC_EVIDENCE_SCHEMA_REF = "ouroboros://schemas/seed-acceptance-evidence/v1"
"""Canonical evidence-schema reference used for current string AC dispatch nodes."""


def workflow_spec_from_seed(
    seed: Seed,
    *,
    input_schema_ref: str = DEFAULT_SEED_AC_INPUT_SCHEMA_REF,
    evidence_schema_ref: str = DEFAULT_SEED_AC_EVIDENCE_SCHEMA_REF,
    profile_ref: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> WorkflowSpec:
    """Project a current ``Seed`` into a read-only ``WorkflowSpec``.

    The adapter intentionally does not mutate or migrate ``Seed``. Each
    non-blank acceptance criterion becomes one agent-owned task node with
    schema refs required by the Workflow IR validator. All task nodes flow
    into an explicit fan-in barrier before the shared terminal node so the
    graph preserves all-acceptance-criteria completion semantics while
    keeping the current string-AC execution vocabulary.

    Args:
        seed: Immutable Seed to project.
        input_schema_ref: Contract ref for each AC task input payload.
        evidence_schema_ref: Contract ref for evidence emitted for each AC.
        profile_ref: Optional runtime/profile anchor carried as metadata and
            runtime hint; not interpreted by this adapter.
        metadata: Optional additive spec metadata. Values are copied into the
            immutable ``WorkflowSpec.metadata`` mapping by the IR model.

    Raises:
        ValueError: If the Seed has no acceptance criteria, contains blank ACs,
            has no usable seed id, or if the emitted spec fails IR validation.

    Returns:
        A validated ``WorkflowSpec`` with ``source=SourceKind.SEED``.
    """
    seed_id = seed.metadata.seed_id.strip()
    if not seed_id:
        msg = "Seed metadata.seed_id must be non-blank to project Workflow IR"
        raise ValueError(msg)

    criteria = tuple(_normalize_acceptance_criteria(seed.acceptance_criteria))
    if not criteria:
        msg = "Seed must contain at least one acceptance criterion to project Workflow IR"
        raise ValueError(msg)

    join_node = WorkflowNode(
        node_id="seed_ac_join",
        kind=NodeKind.FAN_IN,
        owner=NodeOwner.HARNESS,
        name="All seed acceptance criteria complete",
        metadata={"seed_id": seed_id, "barrier": "all_acceptance_criteria"},
    )
    terminal_node = WorkflowNode(
        node_id="seed_terminal",
        kind=NodeKind.TERMINAL,
        owner=NodeOwner.HARNESS,
        name="Seed workflow complete",
        metadata={"seed_id": seed_id},
    )
    nodes: list[WorkflowNode] = []
    edges: list[WorkflowEdge] = []
    for zero_based_index, criterion in enumerate(criteria):
        ac_index = zero_based_index + 1
        node_id = f"seed_ac_{ac_index:03d}"
        node_metadata: dict[str, Any] = {
            "seed_id": seed_id,
            "seed_version": seed.metadata.version,
            "task_type": seed.task_type,
            "acceptance_criterion_index": ac_index,
            "acceptance_criterion": criterion,
        }
        runtime_hints: dict[str, Any] = {}
        if profile_ref is not None:
            runtime_hints["profile_ref"] = profile_ref
            node_metadata["profile_ref"] = profile_ref

        nodes.append(
            WorkflowNode(
                node_id=node_id,
                kind=NodeKind.TASK,
                owner=NodeOwner.AGENT,
                name=f"Acceptance criterion {ac_index}",
                input_schema_ref=input_schema_ref,
                evidence_schema_ref=evidence_schema_ref,
                runtime_hints=runtime_hints,
                metadata=node_metadata,
            )
        )
        edges.append(
            WorkflowEdge(
                edge_id=f"edge_{node_id}_join",
                source=node_id,
                target=join_node.node_id,
                kind=EdgeKind.FAN_IN,
                metadata={"acceptance_criterion_index": ac_index, "seed_id": seed_id},
            )
        )

    spec_metadata: dict[str, Any] = dict(metadata or {})
    spec_metadata.update(
        {
            "seed_id": seed_id,
            "seed_version": seed.metadata.version,
            "task_type": seed.task_type,
            "acceptance_criteria_count": len(criteria),
        }
    )
    if seed.metadata.interview_id is not None:
        spec_metadata["interview_id"] = seed.metadata.interview_id
    if profile_ref is not None:
        spec_metadata["profile_ref"] = profile_ref

    spec = WorkflowSpec(
        spec_id=f"wfspec_{seed_id}",
        source=SourceKind.SEED,
        source_ref=seed_id,
        nodes=(*nodes, join_node, terminal_node),
        edges=(
            *edges,
            WorkflowEdge(
                edge_id="edge_seed_ac_join_terminal",
                source=join_node.node_id,
                target=terminal_node.node_id,
                kind=EdgeKind.TERMINAL,
                metadata={"seed_id": seed_id, "barrier": "all_acceptance_criteria"},
            ),
        ),
        metadata=spec_metadata,
    )
    validation = validate_workflow(spec)
    if not validation.ok:
        details = ", ".join(error.code for error in validation.errors)
        msg = f"Seed projected to invalid WorkflowSpec: {details}"
        raise ValueError(msg)
    return spec


def _normalize_acceptance_criteria(criteria: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for zero_based_index, criterion in enumerate(criteria):
        if not isinstance(criterion, str):
            msg = (
                "Seed acceptance_criteria must contain only strings before the "
                f"PlannedAC migration; item {zero_based_index + 1} is "
                f"{type(criterion).__name__}"
            )
            raise ValueError(msg)
        stripped = criterion.strip()
        if not stripped:
            msg = f"Seed acceptance criterion {zero_based_index + 1} must be non-blank"
            raise ValueError(msg)
        normalized.append(stripped)
    return tuple(normalized)


__all__ = [
    "DEFAULT_SEED_AC_EVIDENCE_SCHEMA_REF",
    "DEFAULT_SEED_AC_INPUT_SCHEMA_REF",
    "workflow_spec_from_seed",
]
