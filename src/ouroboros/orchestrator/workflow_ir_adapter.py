"""Read-only adapters into the typed Workflow IR.

This module owns the narrow #956 PR-2 boundary: translate today's immutable
``Seed`` shape into a validated ``WorkflowSpec`` without changing Seed schema,
runtime dispatch, persistence, or projection records.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
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
from ouroboros.orchestrator.workflow_lifecycle import (
    WorkflowLifecycleEvent,
    WorkflowLifecycleEventType,
)
from ouroboros.plugin.firewall import InvocationResult
from ouroboros.plugin.manifest import PluginDescriptor

DEFAULT_SEED_AC_INPUT_SCHEMA_REF = "ouroboros://schemas/seed-acceptance-criterion-input/v1"
"""Canonical input-schema reference used for current string AC dispatch nodes."""

DEFAULT_SEED_AC_EVIDENCE_SCHEMA_REF = "ouroboros://schemas/seed-acceptance-evidence/v1"
"""Canonical evidence-schema reference used for current string AC dispatch nodes."""

DEFAULT_PLUGIN_ACTION_INPUT_SCHEMA_REF = "ouroboros://schemas/plugin-action-input/v1"
"""Contract ref for plugin action nodes represented in Workflow IR."""

DEFAULT_PLUGIN_ACTION_EVIDENCE_SCHEMA_REF = "ouroboros://schemas/plugin-action-evidence/v1"
"""Contract ref for evidence expected from plugin action nodes."""


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


def workflow_spec_from_plugin_descriptor(
    descriptor: PluginDescriptor,
    *,
    input_schema_ref: str = DEFAULT_PLUGIN_ACTION_INPUT_SCHEMA_REF,
    evidence_schema_ref: str = DEFAULT_PLUGIN_ACTION_EVIDENCE_SCHEMA_REF,
    metadata: Mapping[str, Any] | None = None,
) -> WorkflowSpec:
    """Project a plugin descriptor into contract-only Workflow IR metadata.

    The adapter represents plugin actions as planned plugin-owned nodes so #956
    can reason about graph shape without granting permissions or dispatching the
    plugin runtime. It consumes only the read-only manifest descriptor from #939.

    Raises:
        ValueError: If the descriptor has no actions or emits an invalid spec.
    """
    if not descriptor.actions:
        msg = "Plugin descriptor must contain at least one action to project Workflow IR"
        raise ValueError(msg)
    _validate_registered_plugin_action_contract(descriptor)

    nodes: list[WorkflowNode] = []
    edges: list[WorkflowEdge] = []
    plugin_id_segment = _identifier_segment(descriptor.plugin_id)
    terminal_node = WorkflowNode(
        node_id=f"plugin_{plugin_id_segment}_terminal",
        kind=NodeKind.TERMINAL,
        owner=NodeOwner.HARNESS,
        name=f"Plugin {descriptor.name} planning complete",
        metadata={
            "plugin_id": descriptor.plugin_id,
            "component_id": descriptor.component_id,
            "dispatch_enabled": False,
        },
    )

    declared_permission_scopes = tuple(
        permission.scope for permission in descriptor.permissions_declared
    )
    lifecycle_hook_names = tuple(hook.name for hook in descriptor.lifecycle_hooks)
    capability_names = tuple(capability.name for capability in descriptor.capabilities_declared)

    for action in descriptor.actions:
        node_id = (
            f"plugin_{plugin_id_segment}_"
            f"{_identifier_segment(action.namespace)}_{_identifier_segment(action.name)}"
        )
        nodes.append(
            WorkflowNode(
                node_id=node_id,
                kind=NodeKind.TASK,
                owner=NodeOwner.PLUGIN,
                name=f"{action.namespace} {action.name}",
                input_schema_ref=input_schema_ref,
                evidence_schema_ref=evidence_schema_ref,
                capability_envelope=capability_names,
                runtime_hints={
                    "dispatch_enabled": False,
                    "contract_only": True,
                },
                metadata={
                    "plugin_id": descriptor.plugin_id,
                    "component_id": descriptor.component_id,
                    "plugin_name": descriptor.name,
                    "plugin_version": descriptor.version,
                    "schema_version": descriptor.schema_version,
                    "action_id": action.action_id,
                    "namespace": action.namespace,
                    "command_name": action.name,
                    "risk": action.risk,
                    "requires_confirmation": action.requires_confirmation,
                    "declared_permission_scopes": declared_permission_scopes,
                    "required_permission_scopes": action.required_permissions,
                    "optional_permission_scopes": action.optional_permissions,
                    "lifecycle_hook_names": lifecycle_hook_names,
                    "dispatch_enabled": False,
                },
            )
        )
        edges.append(
            WorkflowEdge(
                edge_id=f"edge_{node_id}_{terminal_node.node_id}",
                source=node_id,
                target=terminal_node.node_id,
                kind=EdgeKind.TERMINAL,
                metadata={
                    "plugin_id": descriptor.plugin_id,
                    "action_id": action.action_id,
                    "dispatch_enabled": False,
                },
            )
        )

    spec_metadata: dict[str, Any] = dict(metadata or {})
    spec_metadata.update(
        {
            "plugin_id": descriptor.plugin_id,
            "component_id": descriptor.component_id,
            "plugin_name": descriptor.name,
            "plugin_version": descriptor.version,
            "schema_version": descriptor.schema_version,
            "actions_count": len(descriptor.actions),
            "dispatch_enabled": False,
        }
    )
    spec = WorkflowSpec(
        spec_id=f"wfspec_plugin_{plugin_id_segment}",
        source=SourceKind.PLUGIN,
        source_ref=descriptor.plugin_id,
        nodes=(*nodes, terminal_node),
        edges=tuple(edges),
        metadata=spec_metadata,
    )
    validation = validate_workflow(spec)
    if not validation.ok:
        details = ", ".join(error.code for error in validation.errors)
        msg = f"Plugin descriptor projected to invalid WorkflowSpec: {details}"
        raise ValueError(msg)
    return spec


def workflow_lifecycle_from_plugin_invocation_result(
    spec: WorkflowSpec,
    *,
    node_id: str,
    result: InvocationResult,
    timestamp: datetime | None = None,
) -> tuple[WorkflowLifecycleEvent, ...]:
    """Project a plugin firewall result into Workflow IR lifecycle events.

    This is the production adapter boundary between ``plugin.firewall`` and
    Workflow IR consumers. Only a successful firewall result may complete a
    plugin-owned node. Blocked permission-gate outcomes become
    ``NODE_FAILED``/``RUN_FAILED`` with ``reason_code='plugin_blocked'`` so a
    permission denial cannot be replayed as a successful workflow completion.
    """
    nodes_by_id = {node.node_id: node for node in spec.nodes}
    node = nodes_by_id.get(node_id)
    if node is None:
        msg = f"WorkflowSpec {spec.spec_id!r} has no node {node_id!r}"
        raise ValueError(msg)
    if node.owner is not NodeOwner.PLUGIN:
        msg = f"Workflow node {node_id!r} is not plugin-owned"
        raise ValueError(msg)

    started_at = timestamp or datetime.now(tz=UTC)
    if result.status == "success":
        events = [
            WorkflowLifecycleEvent(
                event_type=WorkflowLifecycleEventType.RUN_CREATED,
                workflow_id=spec.spec_id,
                timestamp=started_at,
            ),
            WorkflowLifecycleEvent(
                event_type=WorkflowLifecycleEventType.NODE_COMPLETED,
                workflow_id=spec.spec_id,
                node_id=node_id,
                attempt=1,
                timestamp=started_at + timedelta(seconds=1),
            ),
        ]
        plugin_task_nodes = [
            candidate
            for candidate in spec.nodes
            if candidate.owner is NodeOwner.PLUGIN and candidate.kind is NodeKind.TASK
        ]
        if len(plugin_task_nodes) == 1:
            events.append(
                WorkflowLifecycleEvent(
                    event_type=WorkflowLifecycleEventType.RUN_COMPLETED,
                    workflow_id=spec.spec_id,
                    timestamp=started_at + timedelta(seconds=2),
                )
            )
        return tuple(events)

    reason_code = "plugin_blocked" if result.status == "blocked" else "plugin_failed"
    return (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            timestamp=started_at,
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_FAILED,
            workflow_id=spec.spec_id,
            node_id=node_id,
            attempt=1,
            reason_code=reason_code,
            timestamp=started_at + timedelta(seconds=1),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_FAILED,
            workflow_id=spec.spec_id,
            reason_code=reason_code,
            timestamp=started_at + timedelta(seconds=2),
        ),
    )


def _validate_registered_plugin_action_contract(descriptor: PluginDescriptor) -> None:
    """Mirror the registry's registered-plugin action-shape invariants."""
    namespaces = {action.namespace for action in descriptor.actions}
    if len(namespaces) != 1:
        msg = (
            f"Plugin descriptor {descriptor.plugin_id!r} declares multiple namespaces "
            f"{sorted(namespaces)}; one plugin must own one namespace"
        )
        raise ValueError(msg)

    seen_names: set[str] = set()
    duplicate_names: list[str] = []
    for action in descriptor.actions:
        if action.name in seen_names and action.name not in duplicate_names:
            duplicate_names.append(action.name)
        seen_names.add(action.name)
    if duplicate_names:
        msg = (
            f"Plugin descriptor {descriptor.plugin_id!r} declares duplicate command name(s): "
            f"{sorted(duplicate_names)}"
        )
        raise ValueError(msg)


def _identifier_segment(value: str) -> str:
    """Encode one identifier segment without collapsing punctuation or boundaries."""
    return "u" + value.encode("utf-8").hex()


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
    "DEFAULT_PLUGIN_ACTION_EVIDENCE_SCHEMA_REF",
    "DEFAULT_PLUGIN_ACTION_INPUT_SCHEMA_REF",
    "DEFAULT_SEED_AC_EVIDENCE_SCHEMA_REF",
    "DEFAULT_SEED_AC_INPUT_SCHEMA_REF",
    "workflow_spec_from_plugin_descriptor",
    "workflow_lifecycle_from_plugin_invocation_result",
    "workflow_spec_from_seed",
]
