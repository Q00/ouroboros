"""Schema-layer tests for the v0.3 plugin manifest.

Third slice of #939. v0.3 tightens the JSON Schema enum for
``hooks[].name`` to the v1 ``HookKind`` set, so non-Python loaders get
a tighter guard than v0.2. v0.2 remains supported with its broader
enum for backward compatibility.

What this test file covers:

* 0.3 manifests with v1 hook names load via
  :func:`ouroboros.plugin.manifest.load_manifest`.
* 0.3 manifests that reference a deferred or excluded hook name fail
  with the schema-layer error pointer (``/hooks/0/name``).
* 0.2 manifests with deferred names still load, preserving the
  compatibility contract of the still-supported v0.2 schema.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from ouroboros.plugin.hooks import HOOK_LIFECYCLE_READ_SCOPE
from ouroboros.plugin.manifest import (
    SUPPORTED_SCHEMA_VERSIONS,
    PluginManifestError,
    load_manifest,
)

# Re-use the canonical reference manifest from the existing manifest
# test module so the schema-compliant payload stays a single source of
# truth.
from tests.unit.plugin.test_manifest import REFERENCE_MANIFEST


def _v03_manifest() -> dict:
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
    return payload


def _v02_manifest() -> dict:
    payload = deepcopy(REFERENCE_MANIFEST)
    payload["schema_version"] = "0.2"
    return payload


def _write(tmp_path: Path, payload: dict) -> Path:
    target = tmp_path / "ouroboros.plugin.json"
    target.write_text(json.dumps(payload))
    return target


def _valid_hook(name: str = "before_invocation", failure_policy: str = "fail_closed") -> dict:
    return {
        "name": name,
        "description": "Inspect invocation metadata.",
        "entrypoint": {
            "type": "command",
            "command": "python -m plugin_hooks before",
        },
        "permissions": [HOOK_LIFECYCLE_READ_SCOPE],
        "failure_policy": failure_policy,
        "timeout_seconds": 5,
    }


class TestSupportedSchemaVersions:
    def test_0_3_included_in_support_window(self) -> None:
        assert "0.3" in SUPPORTED_SCHEMA_VERSIONS

    def test_0_2_remains_supported(self) -> None:
        # v0.2 must stay supported during the transition; otherwise
        # existing local manifests break on upgrade.
        assert "0.2" in SUPPORTED_SCHEMA_VERSIONS


class TestV03HookEnum:
    """v0.3 manifests must reject deferred/excluded names at the schema layer."""

    def test_before_invocation_accepted(self, tmp_path: Path) -> None:
        payload = _v03_manifest()
        payload["hooks"] = [_valid_hook(name="before_invocation")]
        manifest = load_manifest(_write(tmp_path, payload))
        assert manifest.schema_version == "0.3"
        assert manifest.hooks[0].name == "before_invocation"

    def test_after_invocation_accepted(self, tmp_path: Path) -> None:
        payload = _v03_manifest()
        payload["hooks"] = [_valid_hook(name="after_invocation")]
        manifest = load_manifest(_write(tmp_path, payload))
        assert manifest.hooks[0].name == "after_invocation"

    @pytest.mark.parametrize(
        "deferred_name",
        [
            "before_tool_call",
            "after_tool_call",
            "before_artifact_write",
            "after_artifact_write",
            "on_error",
            "on_cancel",
        ],
    )
    def test_deferred_name_rejected_at_schema_layer(
        self, tmp_path: Path, deferred_name: str
    ) -> None:
        payload = _v03_manifest()
        payload["hooks"] = [_valid_hook(name=deferred_name)]
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
        payload = _v03_manifest()
        payload["hooks"] = [_valid_hook(name=excluded_name)]
        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write(tmp_path, payload))
        assert exc_info.value.json_pointer == "/hooks/0/name"


class TestV02Compatibility:
    """The still-supported v0.2 schema keeps its broader hook enum.

    v0.3 is the tightened contract. v0.2 manifests that already used a
    deferred hook name must continue to load until the project removes
    v0.2 from SUPPORTED_SCHEMA_VERSIONS in a separate compatibility
    decision.
    """

    def test_v02_deferred_name_still_loads(self, tmp_path: Path) -> None:
        payload = _v02_manifest()
        payload["hooks"] = [_valid_hook(name="before_tool_call")]
        payload["hooks"][0]["permissions"] = []
        manifest = load_manifest(_write(tmp_path, payload))
        assert manifest.schema_version == "0.2"
        assert manifest.hooks[0].name == "before_tool_call"

    def test_v02_schema_still_rejects_name_outside_its_enum(self, tmp_path: Path) -> None:
        payload = _v02_manifest()
        payload["hooks"] = [_valid_hook(name="before_runtime_start")]
        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write(tmp_path, payload))
        assert exc_info.value.json_pointer == "/hooks/0/name"


class TestV03HookAuditEvents:
    def test_manifest_audit_default_matches_loader_standard_events(self, tmp_path: Path) -> None:
        payload = _v03_manifest()
        payload.pop("audit", None)
        manifest = load_manifest(_write(tmp_path, payload))
        assert manifest.audit.events == (
            "plugin.invoked",
            "plugin.permission_used",
            "plugin.completed",
            "plugin.failed",
        )

    def test_manifest_audit_events_accept_hook_wrapper_events(self, tmp_path: Path) -> None:
        payload = _v03_manifest()
        payload["audit"] = {"events": ["plugin.hook.blocked", "plugin.hook.failed"]}
        manifest = load_manifest(_write(tmp_path, payload))
        assert manifest.audit.events == ("plugin.hook.blocked", "plugin.hook.failed")
