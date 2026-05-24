"""Tests for read-only plugin descriptor/action projection."""

from __future__ import annotations

import json
from pathlib import Path

from ouroboros.plugin.hooks import (
    HOOK_COMPLETED_EVENT,
    HOOK_FAILED_EVENT,
    HOOK_INVOKED_EVENT,
    HOOK_LIFECYCLE_POLICY_SCOPE,
    HOOK_LIFECYCLE_READ_SCOPE,
)
from ouroboros.plugin.manifest import (
    SUPPORTED_SCHEMA_VERSIONS,
    PluginActionDescriptor,
    PluginDescriptor,
    load_manifest,
    plugin_descriptor_from_manifest,
)
from tests.unit.plugin.test_manifest import REFERENCE_MANIFEST, _issue29_command_metadata_manifest


def _write(tmp_path: Path, payload: dict) -> Path:
    target = tmp_path / "ouroboros.plugin.json"
    target.write_text(json.dumps(payload))
    return target


def _v03_manifest_with_hooks() -> dict:
    payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    payload["schema_version"] = "0.3"
    payload["permissions"].extend(
        [
            {
                "scope": HOOK_LIFECYCLE_READ_SCOPE,
                "risk": "read_only",
                "required": True,
                "reason": "Allow lifecycle hook observation.",
            },
            {
                "scope": HOOK_LIFECYCLE_POLICY_SCOPE,
                "risk": "read_only",
                "required": True,
                "reason": "Allow lifecycle policy decisions.",
            },
            {
                "scope": "github:write",
                "risk": "write",
                "required": False,
                "reason": "Optional mutation scope.",
            },
        ]
    )
    payload["hooks"] = [
        {
            "name": "before_invocation",
            "entrypoint": {"type": "command", "command": "python before_hook.py"},
            "failure_policy": "fail_closed",
            "permissions": [HOOK_LIFECYCLE_POLICY_SCOPE],
            "timeout_seconds": 5,
        },
        {
            "name": "after_invocation",
            "entrypoint": {"type": "command", "command": "python after_hook.py"},
            "failure_policy": "fail_open",
            "permissions": [HOOK_LIFECYCLE_READ_SCOPE],
            "timeout_seconds": 5,
        },
    ]
    return payload


def test_manifest_projects_descriptor_without_runtime_execution(tmp_path: Path) -> None:
    manifest = load_manifest(_write(tmp_path, _v03_manifest_with_hooks()))

    descriptor = plugin_descriptor_from_manifest(manifest)

    assert isinstance(descriptor, PluginDescriptor)
    assert descriptor.component_id == "github-pr-ops"
    assert descriptor.plugin_id == "github-pr-ops"
    assert descriptor.kind == "plugin"
    assert descriptor.schema_version == "0.3"
    assert descriptor.source.type == "local_path"
    assert descriptor.entrypoint.command == "python -m github_pr_ops"
    assert descriptor.capabilities_declared == manifest.capabilities
    assert descriptor.permissions_declared == manifest.permissions
    assert descriptor.lifecycle_hooks == manifest.hooks
    assert descriptor.audit_events == manifest.audit.events
    assert descriptor.compatibility == SUPPORTED_SCHEMA_VERSIONS


def test_descriptor_projects_command_as_harness_action(tmp_path: Path) -> None:
    manifest = load_manifest(_write(tmp_path, _v03_manifest_with_hooks()))

    action = manifest.to_descriptor().actions[0]

    assert isinstance(action, PluginActionDescriptor)
    assert action.action_id == "github-pr-ops:github-pr:review"
    assert action.namespace == "github-pr"
    assert action.name == "review"
    assert action.usage == "ooo github-pr review <pull-request-url>"
    assert action.risk == "read_only"
    assert action.entrypoint.command == "python -m github_pr_ops"
    assert action.permissions == ()
    assert action.required_permissions == (
        "github:read",
        HOOK_LIFECYCLE_READ_SCOPE,
        HOOK_LIFECYCLE_POLICY_SCOPE,
    )
    assert action.optional_permissions == ("github:write",)
    assert [argument.name for argument in action.arguments] == ["pull_request_url"]


def test_descriptor_preserves_command_level_agentos_metadata(tmp_path: Path) -> None:
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

    action = manifest.to_descriptor().actions[0]

    assert action.permissions == ("github:read", "github:write")
    assert action.required_permissions == ("github:read",)
    assert action.optional_permissions == ("github:write",)
    assert action.upstream == {"capability": "repository-inspection", "mode": "pinned_checkout"}
    assert action.artifacts == {
        "writes": ["result.json", "report.md", "handoff.json"],
        "bounded": True,
    }
    assert action.handoff == {
        "produces": True,
        "consumer": "ooo auto",
        "description": "Continue from inspection evidence.",
    }
    assert action.timeout_seconds == 30
    assert action.result_states == ("completed", "blocked", "failed")
    assert action.redaction == {"rules": ["no secrets", "bounded metadata only"]}


def test_descriptor_reflects_schema_default_audit_events(tmp_path: Path) -> None:
    manifest = load_manifest(_write(tmp_path, _v03_manifest_with_hooks()))

    descriptor = manifest.to_descriptor()

    assert HOOK_INVOKED_EVENT in descriptor.audit_events
    assert HOOK_COMPLETED_EVENT in descriptor.audit_events
    assert HOOK_FAILED_EVENT in descriptor.audit_events
    assert "plugin.invoked" in descriptor.audit_events
    assert "plugin.completed" in descriptor.audit_events


def test_v01_descriptor_has_no_hooks_and_standard_action_projection(tmp_path: Path) -> None:
    manifest = load_manifest(_write(tmp_path, REFERENCE_MANIFEST))

    descriptor = manifest.to_descriptor()

    assert descriptor.schema_version == "0.1"
    assert descriptor.lifecycle_hooks == ()
    assert descriptor.audit_events == (
        "plugin.invoked",
        "plugin.permission_used",
        "plugin.completed",
        "plugin.failed",
    )
    assert len(descriptor.actions) == 1
    assert descriptor.actions[0].required_permissions == ("github:read",)
    assert descriptor.actions[0].optional_permissions == ()
