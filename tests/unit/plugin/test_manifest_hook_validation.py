"""Manifest validator tests for the v1 hook contract.

Second slice of #939. For schema v0.3, validates that
``load_manifest`` rejects manifests whose ``hooks[].name`` is not in
the v1 :class:`ouroboros.plugin.hooks.HookKind` vocabulary, and whose
``hooks[].failure_policy`` is not one of ``fail_open`` /
``fail_closed``.

Existing hook tests in ``test_manifest.py`` cover the happy path and
the top-level permission requirement; this file only adds the new
rejection paths so the diff is focused on the new behaviour.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from ouroboros.plugin.hooks import HOOK_LIFECYCLE_POLICY_SCOPE, HOOK_LIFECYCLE_READ_SCOPE
from ouroboros.plugin.manifest import PluginManifestError, _build_hook, load_manifest

# Re-use the canonical reference manifest from the existing manifest
# test module so the schema-compliant shape stays a single source of
# truth across the two test files.
from tests.unit.plugin.test_manifest import REFERENCE_MANIFEST


def _hook_manifest() -> dict:
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


def _write(tmp_path: Path, payload: dict | str) -> Path:
    target = tmp_path / "ouroboros.plugin.json"
    if isinstance(payload, str):
        target.write_text(payload)
    else:
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
        "permissions": [
            HOOK_LIFECYCLE_POLICY_SCOPE
            if failure_policy == "fail_closed"
            else HOOK_LIFECYCLE_READ_SCOPE
        ],
        "failure_policy": failure_policy,
        "timeout_seconds": 5,
    }


class TestV1HookVocabulary:
    """Manifest validator must enforce the v1 ``HookKind`` set."""

    def test_v1_hook_name_accepted(self, tmp_path: Path) -> None:
        payload = _hook_manifest()
        payload["hooks"] = [_valid_hook(name="before_invocation")]
        manifest = load_manifest(_write(tmp_path, payload))
        assert manifest.hooks[0].name == "before_invocation"

    def test_after_invocation_accepted(self, tmp_path: Path) -> None:
        payload = _hook_manifest()
        payload["hooks"] = [_valid_hook(name="after_invocation", failure_policy="fail_open")]
        manifest = load_manifest(_write(tmp_path, payload))
        assert manifest.hooks[0].name == "after_invocation"

    def test_deferred_hook_name_rejected(self, tmp_path: Path) -> None:
        payload = _hook_manifest()
        payload["hooks"] = [_valid_hook(name="before_tool_call")]
        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write(tmp_path, payload))
        err = exc_info.value
        assert err.json_pointer == "/hooks/0/name"

    def test_excluded_hook_name_rejected(self, tmp_path: Path) -> None:
        payload = _hook_manifest()
        payload["hooks"] = [_valid_hook(name="before_runtime_start")]
        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write(tmp_path, payload))
        err = exc_info.value
        assert err.json_pointer == "/hooks/0/name"

    def test_unknown_hook_name_rejected(self, tmp_path: Path) -> None:
        payload = _hook_manifest()
        payload["hooks"] = [_valid_hook(name="made_up_hook")]
        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write(tmp_path, payload))
        err = exc_info.value
        assert err.json_pointer == "/hooks/0/name"

    def test_empty_hook_name_rejected(self, tmp_path: Path) -> None:
        payload = _hook_manifest()
        payload["hooks"] = [_valid_hook(name="")]
        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write(tmp_path, payload))
        err = exc_info.value
        assert err.json_pointer == "/hooks/0/name"


class TestHookFailurePolicy:
    """Manifest validator must enforce the v1 failure-policy vocabulary."""

    def test_fail_open_accepted(self, tmp_path: Path) -> None:
        payload = _hook_manifest()
        payload["hooks"] = [_valid_hook(failure_policy="fail_open")]
        manifest = load_manifest(_write(tmp_path, payload))
        assert manifest.hooks[0].failure_policy == "fail_open"

    def test_fail_closed_accepted(self, tmp_path: Path) -> None:
        payload = _hook_manifest()
        payload["hooks"] = [_valid_hook(name="before_invocation", failure_policy="fail_closed")]
        manifest = load_manifest(_write(tmp_path, payload))
        assert manifest.hooks[0].failure_policy == "fail_closed"

    def test_after_invocation_fail_closed_rejected(self, tmp_path: Path) -> None:
        payload = _hook_manifest()
        payload["hooks"] = [_valid_hook(name="after_invocation", failure_policy="fail_closed")]

        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write(tmp_path, payload))

        err = exc_info.value
        assert err.json_pointer == "/hooks/0/failure_policy"
        assert "fail_open" in err.expected

    def test_after_invocation_fail_closed_rejected_by_python_validator(self) -> None:
        with pytest.raises(PluginManifestError) as exc_info:
            _build_hook(
                _valid_hook(name="after_invocation", failure_policy="fail_closed"),
                declared_permission_scopes=frozenset(
                    {HOOK_LIFECYCLE_READ_SCOPE, HOOK_LIFECYCLE_POLICY_SCOPE}
                ),
                declared_required_permission_scopes=frozenset(
                    {HOOK_LIFECYCLE_READ_SCOPE, HOOK_LIFECYCLE_POLICY_SCOPE}
                ),
                hook_index=0,
                manifest_path="ouroboros.plugin.json",
                schema_version="0.3",
            )

        err = exc_info.value
        assert err.json_pointer == "/hooks/0/failure_policy"
        assert "after_invocation" in err.args[0]

    def test_unknown_failure_policy_rejected(self, tmp_path: Path) -> None:
        payload = _hook_manifest()
        payload["hooks"] = [_valid_hook(failure_policy="retry")]
        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write(tmp_path, payload))
        err = exc_info.value
        assert err.json_pointer == "/hooks/0/failure_policy"


class TestHookLifecyclePermission:
    """v0.3 hooks must opt into the v1 lifecycle permission boundary."""

    def test_missing_lifecycle_permission_rejected(self, tmp_path: Path) -> None:
        payload = _hook_manifest()
        payload["hooks"] = [_valid_hook()]
        payload["hooks"][0]["permissions"] = []

        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write(tmp_path, payload))

        err = exc_info.value
        assert err.json_pointer == "/hooks/0/permissions"
        assert HOOK_LIFECYCLE_POLICY_SCOPE in err.expected

    def test_read_only_fail_closed_rejected(self, tmp_path: Path) -> None:
        payload = _hook_manifest()
        payload["hooks"] = [_valid_hook(failure_policy="fail_closed")]
        payload["hooks"][0]["permissions"] = [HOOK_LIFECYCLE_READ_SCOPE]

        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write(tmp_path, payload))

        err = exc_info.value
        assert err.json_pointer == "/hooks/0/permissions"
        assert HOOK_LIFECYCLE_POLICY_SCOPE in err.expected

    def test_lifecycle_permission_must_still_be_declared_top_level(self, tmp_path: Path) -> None:
        payload = _hook_manifest()
        payload["permissions"] = [
            permission
            for permission in payload["permissions"]
            if permission["scope"] != HOOK_LIFECYCLE_POLICY_SCOPE
        ]
        payload["hooks"] = [_valid_hook()]

        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write(tmp_path, payload))

        err = exc_info.value
        assert err.json_pointer == "/hooks/0/permissions/0"
        assert "top-level permissions" in err.args[0]

    def test_fail_closed_policy_permission_must_be_required_top_level(self, tmp_path: Path) -> None:
        payload = _hook_manifest()
        for permission in payload["permissions"]:
            if permission["scope"] == HOOK_LIFECYCLE_POLICY_SCOPE:
                permission["required"] = False
        payload["hooks"] = [_valid_hook(failure_policy="fail_closed")]

        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write(tmp_path, payload))

        err = exc_info.value
        assert err.json_pointer == "/permissions"
        assert "required=true" in err.expected
        assert HOOK_LIFECYCLE_POLICY_SCOPE in err.expected

    def test_fail_open_lifecycle_permission_must_be_required_top_level(
        self, tmp_path: Path
    ) -> None:
        payload = _hook_manifest()
        for permission in payload["permissions"]:
            if permission["scope"] == HOOK_LIFECYCLE_READ_SCOPE:
                permission["required"] = False
        payload["hooks"] = [_valid_hook(failure_policy="fail_open")]

        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write(tmp_path, payload))

        err = exc_info.value
        assert err.json_pointer == "/permissions"
        assert "required=true" in err.expected
        assert HOOK_LIFECYCLE_READ_SCOPE in err.expected
