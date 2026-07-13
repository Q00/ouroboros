"""Schema and audit contract tests for the v0.6 on_rewind hook."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

from ouroboros.plugin.hooks import HOOK_EVENT_TYPES, HOOK_REWIND_OBSERVE_SCOPE
from ouroboros.plugin.manifest import (
    SUPPORTED_SCHEMA_VERSIONS,
    AuditSpec,
    PluginManifestError,
    load_manifest,
)
from tests.unit.plugin.test_manifest import REFERENCE_MANIFEST


def _v06_manifest() -> dict:
    payload = deepcopy(REFERENCE_MANIFEST)
    payload["schema_version"] = "0.6"
    payload["permissions"].append(
        {
            "scope": HOOK_REWIND_OBSERVE_SCOPE,
            "risk": "read_only",
            "required": True,
            "reason": "Observe committed lineage rewinds.",
        }
    )
    return payload


def _rewind_hook() -> dict:
    return {
        "name": "on_rewind",
        "description": "Observe a committed rewind.",
        "entrypoint": {"type": "command", "command": "python -m hook rewind"},
        "permissions": [HOOK_REWIND_OBSERVE_SCOPE],
        "failure_policy": "fail_open",
        "timeout_seconds": 5,
    }


def _write(tmp_path: Path, payload: dict) -> Path:
    target = tmp_path / "ouroboros.plugin.json"
    target.write_text(json.dumps(payload))
    return target


def _audit_schema() -> dict:
    path = (
        Path(__file__).resolve().parents[3]
        / "src/ouroboros/plugin/schemas/0.6/audit-event.schema.json"
    )
    return json.loads(path.read_text())


def _audit_event(*, observation: bool) -> dict:
    event = {
        "schema_version": "0.6",
        "event_type": "plugin.hook.invoked" if observation else "plugin.invoked",
        "occurred_at": "2026-07-13T06:00:00Z",
        "plugin": {
            "name": "rewind-observer",
            "version": "1.0.0",
            "source_type": "plugin_home",
        },
        "trust_state": "trusted",
        "capabilities_used": [],
        "permissions_used": [HOOK_REWIND_OBSERVE_SCOPE] if observation else [],
        "result": {"status": "success"},
    }
    if observation:
        event["observation"] = {
            "kind": "rewind",
            "id": "event-1",
            "aggregate_type": "lineage",
            "aggregate_id": "lin-1",
        }
        event["provenance"] = {
            "correlation_id": "event-1",
            "hook_name": "on_rewind",
            "failure_policy": "fail_open",
        }
    else:
        event["command"] = {"namespace": "rewind-observer", "name": "status"}
    return event


class TestV06Manifest:
    def test_support_window_reserves_0_5_and_accepts_0_6(self) -> None:
        assert "0.5" not in SUPPORTED_SCHEMA_VERSIONS
        assert "0.6" in SUPPORTED_SCHEMA_VERSIONS

    def test_on_rewind_fail_open_is_accepted(self, tmp_path: Path) -> None:
        payload = _v06_manifest()
        payload["hooks"] = [_rewind_hook()]

        manifest = load_manifest(_write(tmp_path, payload))

        assert manifest.schema_version == "0.6"
        assert manifest.hooks[0].name == "on_rewind"
        assert manifest.hooks[0].permissions == (HOOK_REWIND_OBSERVE_SCOPE,)

    def test_fail_closed_is_rejected(self, tmp_path: Path) -> None:
        payload = _v06_manifest()
        payload["hooks"] = [_rewind_hook() | {"failure_policy": "fail_closed"}]

        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write(tmp_path, payload))

        assert exc_info.value.json_pointer == "/hooks/0/failure_policy"

    def test_hook_permission_is_required(self, tmp_path: Path) -> None:
        payload = _v06_manifest()
        payload["hooks"] = [_rewind_hook() | {"permissions": []}]

        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write(tmp_path, payload))

        assert exc_info.value.json_pointer == "/hooks/0/permissions"

    @pytest.mark.parametrize(
        "extra_scope",
        ["plugin:lifecycle:read", "plugin:tool:observe"],
    )
    def test_unrelated_hook_permission_is_rejected(self, tmp_path: Path, extra_scope: str) -> None:
        payload = _v06_manifest()
        payload["permissions"].append(
            {
                "scope": extra_scope,
                "risk": "read_only",
                "required": True,
            }
        )
        payload["hooks"] = [
            _rewind_hook() | {"permissions": [HOOK_REWIND_OBSERVE_SCOPE, extra_scope]}
        ]

        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write(tmp_path, payload))

        assert exc_info.value.json_pointer == "/hooks/0/permissions"

    def test_top_level_permission_must_be_required(self, tmp_path: Path) -> None:
        payload = _v06_manifest()
        payload["permissions"][-1]["required"] = False
        payload["hooks"] = [_rewind_hook()]

        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write(tmp_path, payload))

        assert exc_info.value.json_pointer == "/permissions"

    def test_v0_4_still_rejects_on_rewind(self, tmp_path: Path) -> None:
        payload = _v06_manifest()
        payload["schema_version"] = "0.4"
        payload["hooks"] = [_rewind_hook()]

        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write(tmp_path, payload))

        assert exc_info.value.json_pointer == "/hooks/0/name"

    def test_explicit_audit_requires_generic_hook_events(self, tmp_path: Path) -> None:
        payload = _v06_manifest()
        payload["hooks"] = [_rewind_hook()]
        payload["audit"] = {"events": list(AuditSpec.standard_four_events().events)}

        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write(tmp_path, payload))

        assert exc_info.value.json_pointer == "/audit/events"

    def test_default_audit_contains_generic_hook_events(self) -> None:
        assert set(AuditSpec.standard_events_for_schema("0.6").events) >= HOOK_EVENT_TYPES


class TestV06AuditSubject:
    def test_command_subject_remains_valid(self) -> None:
        assert not list(
            Draft202012Validator(_audit_schema()).iter_errors(_audit_event(observation=False))
        )

    def test_rewind_observation_subject_is_valid(self) -> None:
        assert not list(
            Draft202012Validator(_audit_schema()).iter_errors(_audit_event(observation=True))
        )

    def test_neither_subject_is_rejected(self) -> None:
        event = _audit_event(observation=False)
        event.pop("command")
        assert list(Draft202012Validator(_audit_schema()).iter_errors(event))

    def test_both_subjects_are_rejected(self) -> None:
        event = _audit_event(observation=True)
        event["command"] = {"namespace": "rewind-observer", "name": "status"}
        assert list(Draft202012Validator(_audit_schema()).iter_errors(event))

    def test_rewind_provenance_rejects_unbounded_keys(self) -> None:
        event = _audit_event(observation=True)
        event["provenance"]["stdout"] = "raw output"
        assert list(Draft202012Validator(_audit_schema()).iter_errors(event))
