"""Schema-layer tests for the v0.4 plugin manifest (#939 PR F-1).

v0.4 promotes the tool-call hook family
(``before_tool_call`` / ``after_tool_call``) into the v1 ``HookKind``
vocabulary and adds the matching permission scopes
(``plugin:tool:intercept`` / ``plugin:tool:observe``) plus the four
reserved ``plugin.tool.*`` audit event names locked in
``docs/rfc/plugin-tool-call-hook-contract.md``.

What this test file covers:

* v0.4 manifests with lifecycle hooks still load (backward compatible).
* v0.4 manifests with tool-call hooks load when permission / failure
  policy combinations match the schema rules.
* v0.4 manifests with tool-call hooks fail at the schema layer when
  permission / failure policy combinations violate the rules.
* v0.4 schema still rejects deferred artifact/state and excluded
  hook names at ``/hooks/0/name``.
* v0.3 manifests continue to reject tool-call hook names — PR F-1
  does not retroactively relax v0.3 behavior.
* ``standard_events_for_schema("0.4")`` returns the expanded event
  tuple that includes the four ``plugin.tool.*`` reserved names.

Runtime dispatch wiring lands in PR F-2; these tests only assert the
manifest-layer contract.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from ouroboros.plugin.hooks import (
    HOOK_LIFECYCLE_POLICY_SCOPE,
    HOOK_LIFECYCLE_READ_SCOPE,
    HOOK_TOOL_CALL_AUDIT_EVENTS,
    HOOK_TOOL_INTERCEPT_BLOCKED_EVENT,
    HOOK_TOOL_INTERCEPT_COMPLETED_EVENT,
    HOOK_TOOL_INTERCEPT_REQUESTED_EVENT,
    HOOK_TOOL_INTERCEPT_SCOPE,
    HOOK_TOOL_OBSERVE_RECORDED_EVENT,
    HOOK_TOOL_OBSERVE_SCOPE,
    HookKind,
)
from ouroboros.plugin.manifest import (
    SUPPORTED_SCHEMA_VERSIONS,
    AuditSpec,
    PluginManifestError,
    load_manifest,
)
from tests.unit.plugin.test_manifest import REFERENCE_MANIFEST


def _v04_manifest() -> dict:
    payload = deepcopy(REFERENCE_MANIFEST)
    payload["schema_version"] = "0.4"
    payload["permissions"].append(
        {
            "scope": HOOK_LIFECYCLE_READ_SCOPE,
            "risk": "read_only",
            "required": True,
            "reason": "Allow v1 lifecycle hook observation.",
        }
    )
    payload["permissions"].append(
        {
            "scope": HOOK_LIFECYCLE_POLICY_SCOPE,
            "risk": "read_only",
            "required": True,
            "reason": "Allow v1 lifecycle hook policy decisions.",
        }
    )
    # PR F-1: tool-call hook permission scopes must also be declared as
    # top-level permissions so the firewall trust boundary remains
    # authoritative once PR F-2 wires runtime dispatch.
    payload["permissions"].append(
        {
            "scope": HOOK_TOOL_INTERCEPT_SCOPE,
            "risk": "write",
            "required": True,
            "reason": "Allow tool-call interception by lifecycle hooks.",
        }
    )
    payload["permissions"].append(
        {
            "scope": HOOK_TOOL_OBSERVE_SCOPE,
            "risk": "read_only",
            "required": True,
            "reason": "Allow tool-call observation by lifecycle hooks.",
        }
    )
    return payload


def _v03_manifest_for_negative_test() -> dict:
    """v0.3 manifest for regression tests that v0.3 still rejects tool-call names."""

    payload = deepcopy(REFERENCE_MANIFEST)
    payload["schema_version"] = "0.3"
    payload["permissions"].append(
        {
            "scope": HOOK_LIFECYCLE_READ_SCOPE,
            "risk": "read_only",
            "required": True,
            "reason": "Allow v1 lifecycle hook observation.",
        }
    )
    payload["permissions"].append(
        {
            "scope": HOOK_LIFECYCLE_POLICY_SCOPE,
            "risk": "read_only",
            "required": True,
            "reason": "Allow v1 lifecycle hook policy decisions.",
        }
    )
    return payload


def _write(tmp_path: Path, payload: dict) -> Path:
    target = tmp_path / "ouroboros.plugin.json"
    target.write_text(json.dumps(payload))
    return target


def _lifecycle_hook(name: str = "before_invocation", failure_policy: str = "fail_closed") -> dict:
    return {
        "name": name,
        "description": "Inspect invocation metadata.",
        "entrypoint": {
            "type": "command",
            "command": "python -m plugin_hooks before",
        },
        "permissions": [
            HOOK_LIFECYCLE_POLICY_SCOPE
            if failure_policy == "fail_closed"
            else HOOK_LIFECYCLE_READ_SCOPE
        ],
        "failure_policy": failure_policy,
        "timeout_seconds": 5,
    }


def _tool_call_hook(
    name: str = "before_tool_call",
    failure_policy: str = "fail_closed",
    scope: str | None = None,
) -> dict:
    if scope is None:
        scope = (
            HOOK_TOOL_INTERCEPT_SCOPE
            if failure_policy == "fail_closed"
            else HOOK_TOOL_OBSERVE_SCOPE
        )
    return {
        "name": name,
        "description": "Observe (or gate) a plugin-mediated tool call.",
        "entrypoint": {
            "type": "command",
            "command": "python -m plugin_hooks tool_call",
        },
        "permissions": [scope],
        "failure_policy": failure_policy,
        "timeout_seconds": 5,
    }


class TestSupportedSchemaVersions:
    def test_0_4_included_in_support_window(self) -> None:
        assert "0.4" in SUPPORTED_SCHEMA_VERSIONS

    def test_0_3_remains_supported(self) -> None:
        # v0.3 must stay supported during the transition.
        assert "0.3" in SUPPORTED_SCHEMA_VERSIONS


class TestV04LifecycleBackwardCompatibility:
    """v0.4 manifests must still accept the v0.3 lifecycle hook contract."""

    def test_before_invocation_accepted(self, tmp_path: Path) -> None:
        payload = _v04_manifest()
        payload["hooks"] = [_lifecycle_hook(name="before_invocation")]
        manifest = load_manifest(_write(tmp_path, payload))
        assert manifest.schema_version == "0.4"
        assert manifest.hooks[0].name == "before_invocation"

    def test_after_invocation_fail_open_accepted(self, tmp_path: Path) -> None:
        payload = _v04_manifest()
        payload["hooks"] = [_lifecycle_hook(name="after_invocation", failure_policy="fail_open")]
        manifest = load_manifest(_write(tmp_path, payload))
        assert manifest.hooks[0].name == "after_invocation"

    def test_on_error_fail_open_accepted(self, tmp_path: Path) -> None:
        payload = _v04_manifest()
        payload["hooks"] = [_lifecycle_hook(name="on_error", failure_policy="fail_open")]
        manifest = load_manifest(_write(tmp_path, payload))
        assert manifest.hooks[0].name == "on_error"


class TestV04ToolCallHookEnum:
    """v0.4 manifests accept the tool-call hook names at the schema layer."""

    def test_before_tool_call_intercept_fail_closed_accepted(self, tmp_path: Path) -> None:
        payload = _v04_manifest()
        payload["hooks"] = [_tool_call_hook(name="before_tool_call", failure_policy="fail_closed")]
        manifest = load_manifest(_write(tmp_path, payload))
        assert manifest.schema_version == "0.4"
        assert manifest.hooks[0].name == HookKind.BEFORE_TOOL_CALL.value
        assert manifest.hooks[0].failure_policy == "fail_closed"
        assert HOOK_TOOL_INTERCEPT_SCOPE in manifest.hooks[0].permissions

    def test_before_tool_call_intercept_fail_open_accepted(self, tmp_path: Path) -> None:
        # `plugin:tool:intercept` may opt down to `fail_open` per §5 of the contract.
        payload = _v04_manifest()
        payload["hooks"] = [
            _tool_call_hook(
                name="before_tool_call",
                failure_policy="fail_open",
                scope=HOOK_TOOL_INTERCEPT_SCOPE,
            )
        ]
        manifest = load_manifest(_write(tmp_path, payload))
        assert manifest.hooks[0].failure_policy == "fail_open"

    def test_before_tool_call_observe_only_fail_open_accepted(self, tmp_path: Path) -> None:
        payload = _v04_manifest()
        payload["hooks"] = [
            _tool_call_hook(
                name="before_tool_call",
                failure_policy="fail_open",
                scope=HOOK_TOOL_OBSERVE_SCOPE,
            )
        ]
        manifest = load_manifest(_write(tmp_path, payload))
        assert manifest.hooks[0].permissions == (HOOK_TOOL_OBSERVE_SCOPE,)

    def test_after_tool_call_observe_fail_open_accepted(self, tmp_path: Path) -> None:
        payload = _v04_manifest()
        payload["hooks"] = [
            _tool_call_hook(
                name="after_tool_call",
                failure_policy="fail_open",
                scope=HOOK_TOOL_OBSERVE_SCOPE,
            )
        ]
        manifest = load_manifest(_write(tmp_path, payload))
        assert manifest.hooks[0].name == HookKind.AFTER_TOOL_CALL.value


class TestV04ToolCallSchemaRules:
    """v0.4 schema constraints on failure_policy and permissions."""

    def test_after_tool_call_fail_closed_rejected(self, tmp_path: Path) -> None:
        # after_tool_call is observation-only — fail_closed is rejected.
        payload = _v04_manifest()
        payload["hooks"] = [
            _tool_call_hook(
                name="after_tool_call",
                failure_policy="fail_closed",
                scope=HOOK_TOOL_INTERCEPT_SCOPE,
            )
        ]
        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write(tmp_path, payload))
        assert exc_info.value.json_pointer == "/hooks/0/failure_policy"

    def test_before_tool_call_fail_closed_without_intercept_rejected(self, tmp_path: Path) -> None:
        # fail_closed before_tool_call must declare plugin:tool:intercept.
        payload = _v04_manifest()
        payload["hooks"] = [
            _tool_call_hook(
                name="before_tool_call",
                failure_policy="fail_closed",
                scope=HOOK_TOOL_OBSERVE_SCOPE,
            )
        ]
        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write(tmp_path, payload))
        assert exc_info.value.json_pointer == "/hooks/0/permissions"

    def test_lifecycle_fail_closed_still_requires_lifecycle_policy(self, tmp_path: Path) -> None:
        # The v0.3 invariant must remain — a lifecycle fail_closed hook
        # cannot satisfy the gate with a tool-call permission scope.
        payload = _v04_manifest()
        payload["hooks"] = [_lifecycle_hook(name="before_invocation", failure_policy="fail_closed")]
        payload["hooks"][0]["permissions"] = [HOOK_TOOL_INTERCEPT_SCOPE]
        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write(tmp_path, payload))
        assert exc_info.value.json_pointer == "/hooks/0/permissions"


class TestV04DeferredAndExcludedHookNames:
    """v0.4 still rejects deferred artifact/state and excluded hook names."""

    @pytest.mark.parametrize(
        "deferred_name",
        ["before_artifact_write", "after_artifact_write"],
    )
    def test_artifact_state_names_rejected_at_schema_layer(
        self, tmp_path: Path, deferred_name: str
    ) -> None:
        payload = _v04_manifest()
        payload["hooks"] = [_lifecycle_hook(name=deferred_name)]
        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write(tmp_path, payload))
        assert exc_info.value.json_pointer == "/hooks/0/name"

    @pytest.mark.parametrize(
        "excluded_name",
        [
            "before_runtime_start",
            "after_runtime_start",
            "before_state_commit",
            "after_state_commit",
            "on_event",
            "on_rewind",
        ],
    )
    def test_excluded_name_rejected_at_schema_layer(
        self, tmp_path: Path, excluded_name: str
    ) -> None:
        payload = _v04_manifest()
        payload["hooks"] = [_lifecycle_hook(name=excluded_name)]
        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write(tmp_path, payload))
        assert exc_info.value.json_pointer == "/hooks/0/name"


class TestV03DoesNotInheritToolCall:
    """v0.3 manifests must still reject tool-call hook names at the schema layer.

    PR F-1 moves the names from ``DeferredHookKind`` into ``HookKind`` but
    must not relax the v0.3 JSON Schema enum; v0.3 plugins relying on
    that boundary continue to fail closed.
    """

    @pytest.mark.parametrize(
        "tool_call_name",
        ["before_tool_call", "after_tool_call"],
    )
    def test_v03_rejects_tool_call_name(self, tmp_path: Path, tool_call_name: str) -> None:
        payload = _v03_manifest_for_negative_test()
        payload["hooks"] = [_tool_call_hook(name=tool_call_name, failure_policy="fail_open")]
        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write(tmp_path, payload))
        assert exc_info.value.json_pointer == "/hooks/0/name"


class TestV04AuditEvents:
    def test_standard_events_for_schema_v04_includes_tool_call_events(self) -> None:
        spec = AuditSpec.standard_events_for_schema("0.4")
        assert HOOK_TOOL_INTERCEPT_REQUESTED_EVENT in spec.events
        assert HOOK_TOOL_INTERCEPT_COMPLETED_EVENT in spec.events
        assert HOOK_TOOL_INTERCEPT_BLOCKED_EVENT in spec.events
        assert HOOK_TOOL_OBSERVE_RECORDED_EVENT in spec.events

    def test_v04_audit_default_in_schema_includes_tool_call_events(self) -> None:
        schema_path = (
            Path(__file__).resolve().parents[3]
            / "src/ouroboros/plugin/schemas/0.4/plugin.schema.json"
        )
        schema = json.loads(schema_path.read_text())
        default_events = set(schema["properties"]["audit"]["default"]["events"])
        assert default_events >= HOOK_TOOL_CALL_AUDIT_EVENTS

    def test_v04_audit_enum_accepts_tool_call_events(self, tmp_path: Path) -> None:
        payload = _v04_manifest()
        payload["hooks"] = [
            _tool_call_hook(name="before_tool_call", failure_policy="fail_closed"),
        ]
        payload["audit"] = {
            "events": [
                "plugin.invoked",
                "plugin.permission_used",
                "plugin.completed",
                "plugin.failed",
                "plugin.hook.invoked",
                "plugin.hook.completed",
                "plugin.hook.blocked",
                "plugin.hook.failed",
                HOOK_TOOL_INTERCEPT_REQUESTED_EVENT,
                HOOK_TOOL_INTERCEPT_COMPLETED_EVENT,
                HOOK_TOOL_INTERCEPT_BLOCKED_EVENT,
                HOOK_TOOL_OBSERVE_RECORDED_EVENT,
            ]
        }
        manifest = load_manifest(_write(tmp_path, payload))
        assert HOOK_TOOL_INTERCEPT_REQUESTED_EVENT in manifest.audit.events


class TestV04AuditEventDefaultForLoader:
    """Confirm the manifest loader's standard_events_for_schema match the JSON Schema default for v0.4."""

    def test_loader_and_schema_default_match(self) -> None:
        schema_path = (
            Path(__file__).resolve().parents[3]
            / "src/ouroboros/plugin/schemas/0.4/plugin.schema.json"
        )
        schema = json.loads(schema_path.read_text())
        default_events = tuple(schema["properties"]["audit"]["default"]["events"])
        loader_events = AuditSpec.standard_events_for_schema("0.4").events
        assert default_events == loader_events
