"""Tests for plugin descriptor -> Workflow IR contract projection."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from ouroboros.orchestrator.workflow_ir import NodeKind, NodeOwner, SourceKind, validate_workflow
from ouroboros.orchestrator.workflow_ir_adapter import (
    DEFAULT_PLUGIN_ACTION_EVIDENCE_SCHEMA_REF,
    DEFAULT_PLUGIN_ACTION_INPUT_SCHEMA_REF,
    workflow_lifecycle_from_plugin_invocation_result,
    workflow_spec_from_plugin_descriptor,
)
from ouroboros.orchestrator.workflow_lifecycle import (
    WorkflowLifecycleEventType,
    validate_workflow_lifecycle_conformance,
)
from ouroboros.plugin.firewall import InvocationResult
from ouroboros.plugin.manifest import load_manifest
from tests.conformance.workflow_ir.fixtures import FIXTURE_EPOCH
from tests.unit.plugin.test_manifest import REFERENCE_MANIFEST, _issue29_command_metadata_manifest


def _write(tmp_path: Path, payload: dict) -> Path:
    target = tmp_path / "ouroboros.plugin.json"
    target.write_text(json.dumps(payload))
    return target


def test_plugin_descriptor_projects_contract_only_plugin_nodes(tmp_path: Path) -> None:
    manifest = load_manifest(_write(tmp_path, REFERENCE_MANIFEST))
    descriptor = manifest.to_descriptor()

    spec = workflow_spec_from_plugin_descriptor(descriptor)

    assert validate_workflow(spec).ok is True
    assert spec.source is SourceKind.PLUGIN
    assert spec.source_ref == descriptor.plugin_id
    assert spec.metadata["plugin_id"] == descriptor.plugin_id
    assert spec.metadata["dispatch_enabled"] is False

    plugin_node = spec.nodes[0]
    assert plugin_node.kind is NodeKind.TASK
    assert plugin_node.owner is NodeOwner.PLUGIN
    assert plugin_node.input_schema_ref == DEFAULT_PLUGIN_ACTION_INPUT_SCHEMA_REF
    assert plugin_node.evidence_schema_ref == DEFAULT_PLUGIN_ACTION_EVIDENCE_SCHEMA_REF
    assert plugin_node.runtime_hints["contract_only"] is True
    assert plugin_node.runtime_hints["dispatch_enabled"] is False
    assert plugin_node.metadata["action_id"] == "github-pr-ops:github-pr:review"
    assert plugin_node.metadata["required_permission_scopes"] == ("github:read",)
    assert plugin_node.metadata["dispatch_enabled"] is False

    terminal_node = spec.nodes[-1]
    assert terminal_node.kind is NodeKind.TERMINAL
    assert spec.edges[0].source == plugin_node.node_id
    assert spec.edges[0].target == terminal_node.node_id
    assert spec.edges[0].metadata["dispatch_enabled"] is False


def test_plugin_descriptor_projects_command_level_metadata_to_workflow_ir(
    tmp_path: Path,
) -> None:
    payload = _issue29_command_metadata_manifest()
    payload["permissions"].append(
        {
            "scope": "github:write",
            "risk": "write",
            "required": False,
            "reason": "Optional mutation scope.",
        }
    )
    payload["commands"][0]["permissions"] = ["github:read", "github:write"]
    manifest = load_manifest(_write(tmp_path, payload))

    spec = workflow_spec_from_plugin_descriptor(manifest.to_descriptor())

    node = spec.nodes[0]
    assert node.metadata["command_permission_scopes"] == ("github:read", "github:write")
    assert node.metadata["required_permission_scopes"] == ("github:read",)
    assert node.metadata["optional_permission_scopes"] == ("github:write",)
    assert node.metadata["upstream"] == {
        "capability": "repository-inspection",
        "mode": "pinned_checkout",
    }
    artifacts = dict(node.metadata["artifacts"])
    assert artifacts["writes"] == ("result.json", "report.md", "handoff.json")
    assert artifacts["bounded"] is True
    assert node.metadata["handoff"]["consumer"] == "ooo auto"
    assert node.metadata["timeout_seconds"] == 30
    assert node.metadata["result_states"] == ("completed", "blocked", "failed")
    assert dict(node.metadata["redaction"])["rules"] == ("no secrets", "bounded metadata only")


def test_plugin_descriptor_projection_is_metadata_only_not_entrypoint_dispatch(
    tmp_path: Path,
) -> None:
    manifest = load_manifest(_write(tmp_path, REFERENCE_MANIFEST))

    spec_json = workflow_spec_from_plugin_descriptor(manifest.to_descriptor()).model_dump_json()

    assert "python -m github_pr_ops" not in spec_json
    assert "dispatch_enabled" in spec_json
    assert "github-pr-ops" in spec_json


def test_plugin_descriptor_ids_are_collision_proof_for_registered_action_shape(
    tmp_path: Path,
) -> None:
    payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    payload["commands"] = [
        {
            **payload["commands"][0],
            "namespace": "github-pr",
            "name": "review",
            "usage": "ooo github-pr review",
        },
        {
            **payload["commands"][0],
            "namespace": "github-pr",
            "name": "review-deep",
            "usage": "ooo github-pr review-deep",
        },
    ]
    manifest = load_manifest(_write(tmp_path, payload))

    spec = workflow_spec_from_plugin_descriptor(manifest.to_descriptor())

    assert validate_workflow(spec).ok is True
    task_nodes = [node for node in spec.nodes if node.kind is NodeKind.TASK]
    assert len(task_nodes) == 2
    assert len({node.node_id for node in task_nodes}) == 2
    assert len({edge.edge_id for edge in spec.edges}) == 2
    assert {(node.metadata["namespace"], node.metadata["command_name"]) for node in task_nodes} == {
        ("github-pr", "review"),
        ("github-pr", "review-deep"),
    }


def test_multi_action_success_projection_does_not_complete_whole_run(tmp_path: Path) -> None:
    payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    payload["commands"] = [
        {
            **payload["commands"][0],
            "namespace": "github-pr",
            "name": "review",
            "usage": "ooo github-pr review",
        },
        {
            **payload["commands"][0],
            "namespace": "github-pr",
            "name": "review-deep",
            "usage": "ooo github-pr review-deep",
        },
    ]
    manifest = load_manifest(_write(tmp_path, payload))
    spec = workflow_spec_from_plugin_descriptor(manifest.to_descriptor())
    first_task = next(node for node in spec.nodes if node.owner is NodeOwner.PLUGIN)

    history = workflow_lifecycle_from_plugin_invocation_result(
        spec=spec,
        node_id=first_task.node_id,
        result=InvocationResult(status="success", exit_code=0),
        timestamp=FIXTURE_EPOCH,
    )

    projected_types = tuple(event.event_type for event in history)
    assert projected_types == (
        WorkflowLifecycleEventType.RUN_CREATED,
        WorkflowLifecycleEventType.NODE_COMPLETED,
    )
    assert WorkflowLifecycleEventType.RUN_COMPLETED not in projected_types


def test_multi_action_success_projections_compose_one_lifecycle_run(tmp_path: Path) -> None:
    payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    payload["commands"] = [
        {
            **payload["commands"][0],
            "namespace": "github-pr",
            "name": "review",
            "usage": "ooo github-pr review",
        },
        {
            **payload["commands"][0],
            "namespace": "github-pr",
            "name": "review-deep",
            "usage": "ooo github-pr review-deep",
        },
    ]
    manifest = load_manifest(_write(tmp_path, payload))
    spec = workflow_spec_from_plugin_descriptor(manifest.to_descriptor())
    task_nodes = [node for node in spec.nodes if node.owner is NodeOwner.PLUGIN]

    first = workflow_lifecycle_from_plugin_invocation_result(
        spec=spec,
        node_id=task_nodes[0].node_id,
        result=InvocationResult(status="success", exit_code=0),
        timestamp=FIXTURE_EPOCH,
    )
    second = workflow_lifecycle_from_plugin_invocation_result(
        spec=spec,
        node_id=task_nodes[1].node_id,
        result=InvocationResult(status="success", exit_code=0),
        timestamp=FIXTURE_EPOCH.replace(second=10),
        completed_plugin_node_ids={task_nodes[0].node_id},
        include_run_created=False,
    )

    history = first + second
    projected_types = tuple(event.event_type for event in history)
    assert projected_types == (
        WorkflowLifecycleEventType.RUN_CREATED,
        WorkflowLifecycleEventType.NODE_COMPLETED,
        WorkflowLifecycleEventType.NODE_COMPLETED,
        WorkflowLifecycleEventType.RUN_COMPLETED,
    )
    report = validate_workflow_lifecycle_conformance(spec, history)
    assert not [issue for issue in report.issues if issue.severity == "error"]


def test_plugin_descriptor_rejects_unregistered_multi_namespace_shape(
    tmp_path: Path,
) -> None:
    payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    payload["commands"] = [
        {
            **payload["commands"][0],
            "namespace": "abc-d",
            "name": "ef",
            "usage": "ooo abc-d ef",
        },
        {
            **payload["commands"][0],
            "namespace": "abc",
            "name": "d-ef",
            "usage": "ooo abc d-ef",
        },
    ]
    manifest = load_manifest(_write(tmp_path, payload))

    with pytest.raises(ValueError, match="multiple namespaces"):
        workflow_spec_from_plugin_descriptor(manifest.to_descriptor())


def test_plugin_descriptor_rejects_duplicate_command_names(tmp_path: Path) -> None:
    payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    payload["commands"] = [
        {**payload["commands"][0], "usage": "ooo github-pr review"},
        {**payload["commands"][0], "usage": "ooo github-pr review-again"},
    ]
    manifest = load_manifest(_write(tmp_path, payload))

    with pytest.raises(ValueError, match="duplicate command name"):
        workflow_spec_from_plugin_descriptor(manifest.to_descriptor())


def test_plugin_descriptor_requires_at_least_one_action(tmp_path: Path) -> None:
    manifest = load_manifest(_write(tmp_path, REFERENCE_MANIFEST))
    descriptor = replace(manifest.to_descriptor(), actions=())

    with pytest.raises(ValueError, match="at least one action"):
        workflow_spec_from_plugin_descriptor(descriptor)
