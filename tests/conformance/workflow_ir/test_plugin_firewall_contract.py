"""Plugin firewall contract fixture (read-only, harness-side).

This test exists at the #939 / #956 boundary. It does NOT change plugin
dispatch behavior (which lives on #939) and does NOT introduce a new event
family. It locks ONE invariant the conformance harness depends on:

    A plugin invocation that is blocked at the permission gate
    cannot be projected as a successful workflow-node completion.

Concretely:

  * ``invoke_plugin`` MUST return ``status="blocked"`` when a required
    permission is not trusted.
  * The blocked path MUST emit only ``plugin.failed`` with
    ``result.status == "blocked"``. It MUST NOT emit ``plugin.invoked``
    or ``plugin.completed``.
  * When a harness builds a Workflow IR lifecycle history from a blocked
    invocation, it cannot legally emit a ``workflow.node.completed`` for
    the plugin-owned node — only ``workflow.node.failed`` with a
    ``reason_code`` derived from the firewall's blocked outcome.

The test is offline-deterministic: a temporary plugin manifest is written
to ``tmp_path``, no network/cloud is touched, and the firewall is invoked
with no trust record so the blocked path is exercised by construction.

Refs #1131, #956, #939 (read-only boundary).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ouroboros.orchestrator.workflow_ir import (
    NodeKind,
    NodeOwner,
    SourceKind,
    WorkflowNode,
    WorkflowSpec,
    validate_workflow,
)
from ouroboros.orchestrator.workflow_ir_adapter import (
    workflow_lifecycle_from_plugin_invocation_result,
)
from ouroboros.orchestrator.workflow_lifecycle import (
    WorkflowLifecycleEventType,
    validate_workflow_lifecycle_conformance,
)
from ouroboros.plugin.firewall import invoke_plugin
from ouroboros.plugin.manifest import load_manifest
from ouroboros.plugin.userlevel_registry import UserLevelProgramRegistry
from tests.conformance.workflow_ir.fixtures import FIXTURE_EPOCH, harness_terminal, terminal_edge

PLUGIN_INPUT_SCHEMA = "schema://conformance.plugin.input.v1"
PLUGIN_EVIDENCE_SCHEMA = "schema://conformance.plugin.evidence.v1"

# Minimal manifest with one required permission and one command. The plugin
# command is read-only (no destructive confirmation gate needed) so the
# blocked path is the only contract under test.
_BLOCKED_PLUGIN_MANIFEST: dict = {
    "schema_version": "0.1",
    "name": "conformance-blocked-plugin",
    "version": "0.0.1",
    "source": {
        "type": "local_path",
        "path": "plugins/conformance-blocked-plugin",
    },
    "commands": [
        {
            "namespace": "conformance",
            "name": "noop",
            "summary": "Never runs — used only to exercise the blocked path.",
            "usage": "ooo conformance noop",
            "risk": "read_only",
            "requires_confirmation": False,
        }
    ],
    "capabilities": [],
    "permissions": [
        {
            "scope": "conformance:required",
            "risk": "read_only",
            "required": True,
        }
    ],
    "entrypoint": {
        "type": "command",
        "command": "python -m conformance_blocked_plugin",
    },
}


@pytest.fixture()
def blocked_plugin_program(tmp_path: Path):
    """Write the manifest to tmp and register it without granting trust."""
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir(parents=True, exist_ok=True)
    manifest_path = plugin_root / "ouroboros.plugin.json"
    manifest_path.write_text(json.dumps(_BLOCKED_PLUGIN_MANIFEST))
    manifest = load_manifest(manifest_path)
    registry = UserLevelProgramRegistry()
    return registry.register(manifest)


def _plugin_node_spec() -> WorkflowSpec:
    """Build a small spec containing a plugin-owned task + terminal."""
    plugin_node = WorkflowNode(
        node_id="plugin_task",
        kind=NodeKind.TASK,
        owner=NodeOwner.PLUGIN,
        name="blocked plugin task",
        input_schema_ref=PLUGIN_INPUT_SCHEMA,
        evidence_schema_ref=PLUGIN_EVIDENCE_SCHEMA,
    )
    terminal = harness_terminal("terminal")
    return WorkflowSpec(
        spec_id="wfspec_plugin_blocked",
        source=SourceKind.SYNTHETIC,
        nodes=(plugin_node, terminal),
        edges=(
            terminal_edge(
                "edge_plugin_terminal",
                "plugin_task",
                "terminal",
            ),
        ),
        metadata={"fixture": "plugin_firewall_blocked"},
    )


def test_blocked_invocation_emits_only_plugin_failed(blocked_plugin_program) -> None:
    """The firewall's blocked path emits exactly one plugin.failed event."""
    events: list[dict] = []
    result = invoke_plugin(
        blocked_plugin_program,
        command_name="noop",
        argv=[],
        # No trust record — required permission is missing by construction.
        trust_record=None,
        event_sink=events.append,
        correlation_id="conformance-blocked",
    )
    assert result.status == "blocked", f"expected status='blocked'; got {result.status!r}"
    event_types = [event["event_type"] for event in events]
    assert event_types == ["plugin.failed"], (
        f"blocked path must emit exactly one plugin.failed event; got {event_types!r}"
    )
    assert events[0]["result"]["status"] == "blocked", (
        f"blocked-path plugin.failed must carry result.status='blocked'; "
        f"got {events[0]['result']!r}"
    )
    # The contract chokepoint: no plugin.invoked AND no plugin.completed
    # may appear when the firewall blocks at the trust gate.
    assert "plugin.invoked" not in event_types
    assert "plugin.completed" not in event_types


def test_blocked_invocation_cannot_present_as_node_completion(
    blocked_plugin_program,
) -> None:
    """A blocked plugin invocation is projected as failure, never completion.

    This is the load-bearing #1131 contract: when the harness projects
    plugin-firewall outcomes into Workflow IR lifecycle rows, it MUST map
    ``status='blocked'`` to ``NODE_FAILED`` (with a discriminating
    reason_code). Encoding a blocked invocation as ``NODE_COMPLETED``
    would silently project a permission denial as a successful run, which
    is exactly the boundary #939 + #956 are designed to prevent.
    """
    events: list[dict] = []
    result = invoke_plugin(
        blocked_plugin_program,
        command_name="noop",
        argv=[],
        trust_record=None,
        event_sink=events.append,
        correlation_id="conformance-blocked-2",
    )
    # The firewall outcome the harness MUST consume.
    assert result.status == "blocked"
    assert result.exit_code is None

    # 1. Spec-level: the plugin-node spec is valid in isolation.
    spec = _plugin_node_spec()
    spec_result = validate_workflow(spec)
    assert spec_result.ok, f"plugin-node spec must validate cleanly; got {spec_result.errors!r}"

    # 2. Production adapter contract: blocked -> NODE_FAILED + RUN_FAILED.
    blocked_history = workflow_lifecycle_from_plugin_invocation_result(
        spec=spec,
        node_id="plugin_task",
        result=result,
        timestamp=FIXTURE_EPOCH,
    )
    projected_types = tuple(event.event_type for event in blocked_history)
    assert WorkflowLifecycleEventType.NODE_FAILED in projected_types
    assert WorkflowLifecycleEventType.RUN_FAILED in projected_types
    assert WorkflowLifecycleEventType.NODE_COMPLETED not in projected_types
    assert WorkflowLifecycleEventType.RUN_COMPLETED not in projected_types
    assert blocked_history[1].reason_code == "plugin_blocked"
    assert blocked_history[2].reason_code == "plugin_blocked"

    # 3. The projected blocked lifecycle remains legal Workflow IR history.
    report = validate_workflow_lifecycle_conformance(spec, blocked_history)
    assert report.ok, (
        "legal blocked projection (NODE_FAILED + RUN_FAILED) must conform; "
        f"got errors={[i.code for i in report.errors]!r}"
    )


def test_blocked_event_carries_plugin_command_identity(blocked_plugin_program) -> None:
    """The single emitted plugin.failed event names the plugin + command.

    Without this, a downstream projector could not map a blocked event back
    to the workflow node it was scheduled for — the projection would have
    to guess identity from correlation_id alone, which violates the #956
    boundary contract that lifecycle rows carry stable node identity.
    """
    events: list[dict] = []
    invoke_plugin(
        blocked_plugin_program,
        command_name="noop",
        argv=[],
        trust_record=None,
        event_sink=events.append,
        correlation_id="conformance-blocked-3",
    )
    assert len(events) == 1
    event = events[0]
    assert event["plugin"]["name"] == "conformance-blocked-plugin"
    command = event["command"]
    # The blocked path is the source of truth for plugin+command identity.
    # The firewall may attach an empty argv summary when argv=[] was passed,
    # so we assert namespace/name precisely and tolerate the bounded
    # argv/argv_summary observability fields the audit schema allows.
    assert command["namespace"] == "conformance"
    assert command["name"] == "noop"
    assert event["trust_state"] == "installed"
    assert event["result"]["status"] == "blocked"
    # The blocked message MUST name the missing scope so a CLI surface
    # can render an unambiguous remediation hint.
    assert "conformance:required" in event["result"]["message"]
