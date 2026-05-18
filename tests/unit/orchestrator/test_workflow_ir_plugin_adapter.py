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
    workflow_spec_from_plugin_descriptor,
)
from ouroboros.plugin.manifest import load_manifest
from tests.unit.plugin.test_manifest import REFERENCE_MANIFEST


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


def test_plugin_descriptor_projection_is_metadata_only_not_entrypoint_dispatch(tmp_path: Path) -> None:
    manifest = load_manifest(_write(tmp_path, REFERENCE_MANIFEST))

    spec_json = workflow_spec_from_plugin_descriptor(manifest.to_descriptor()).model_dump_json()

    assert "python -m github_pr_ops" not in spec_json
    assert "dispatch_enabled" in spec_json
    assert "github-pr-ops" in spec_json


def test_plugin_descriptor_requires_at_least_one_action(tmp_path: Path) -> None:
    manifest = load_manifest(_write(tmp_path, REFERENCE_MANIFEST))
    descriptor = replace(manifest.to_descriptor(), actions=())

    with pytest.raises(ValueError, match="at least one action"):
        workflow_spec_from_plugin_descriptor(descriptor)
