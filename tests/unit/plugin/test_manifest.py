"""Tests for the plugin manifest loader (Q00/ouroboros#728).

Each test asserts BOTH the rejection AND the JSON Pointer to the failing
field, so a future schema change cannot silently relax constraints.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from ouroboros.plugin.manifest import (
    SUPPORTED_SCHEMA_VERSIONS,
    PluginManifest,
    PluginManifestError,
    load_manifest,
)

REFERENCE_MANIFEST: dict = {
    "schema_version": "0.1",
    "name": "github-pr-ops",
    "version": "0.1.0",
    "description": "Reference skeleton for GitHub PR operational workflows.",
    "source": {"type": "local_path", "path": "plugins/github-pr-ops"},
    "commands": [
        {
            "namespace": "github-pr",
            "name": "review",
            "summary": "Review a pull request and summarize readiness without mutating it.",
            "usage": "ooo github-pr review <pull-request-url>",
            "risk": "read_only",
            "requires_confirmation": False,
            "arguments": [
                {
                    "name": "pull_request_url",
                    "type": "url",
                    "required": True,
                    "description": "GitHub pull request URL to inspect.",
                }
            ],
        }
    ],
    "capabilities": [
        {"name": "ledger", "access": "write", "reason": "Record decisions."},
        {"name": "provenance", "access": "write", "reason": "Record context."},
    ],
    "permissions": [
        {
            "scope": "github:read",
            "risk": "read_only",
            "required": True,
            "reason": "Read PR status.",
        }
    ],
    "entrypoint": {"type": "command", "command": "python -m github_pr_ops"},
}


def _write(tmp_path: Path, payload: dict | str) -> Path:
    target = tmp_path / "ouroboros.plugin.json"
    if isinstance(payload, str):
        target.write_text(payload)
    else:
        target.write_text(json.dumps(payload))
    return target


def test_load_reference_manifest(tmp_path: Path) -> None:
    """Test 1: github-pr-ops reference manifest loads cleanly."""
    manifest = load_manifest(_write(tmp_path, REFERENCE_MANIFEST))
    assert isinstance(manifest, PluginManifest)
    assert manifest.name == "github-pr-ops"
    assert manifest.version == "0.1.0"
    assert manifest.schema_version == "0.1"
    assert len(manifest.commands) == 1
    assert manifest.commands[0].name == "review"
    assert manifest.source.type == "local_path"


def test_missing_required_top_level_field(tmp_path: Path) -> None:
    """Test 2: missing `name` raises with empty json_pointer (root-level)."""
    bad = {**REFERENCE_MANIFEST}
    bad.pop("name")
    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(_write(tmp_path, bad))
    err = excinfo.value
    assert err.json_pointer == ""
    assert "name" in err.args[0]


def test_pattern_violation_on_name(tmp_path: Path) -> None:
    """Test 3: pattern violation reports json_pointer=/name."""
    bad = {**REFERENCE_MANIFEST, "name": "Bad Name"}
    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(_write(tmp_path, bad))
    assert excinfo.value.json_pointer == "/name"
    assert "match" in excinfo.value.args[0].lower() or "pattern" in excinfo.value.expected.lower()


def test_unknown_capability(tmp_path: Path) -> None:
    """Test 4: unknown capability name reports nested pointer."""
    bad = json.loads(json.dumps(REFERENCE_MANIFEST))
    bad["capabilities"][0]["name"] = "fake_cap"
    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(_write(tmp_path, bad))
    assert excinfo.value.json_pointer == "/capabilities/0/name"


def test_unknown_source_type(tmp_path: Path) -> None:
    """Test 5: unknown source.type reports /source/type pointer."""
    bad = json.loads(json.dumps(REFERENCE_MANIFEST))
    bad["source"] = {"type": "remote"}
    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(_write(tmp_path, bad))
    assert excinfo.value.json_pointer == "/source/type"


def test_additional_property_rejected(tmp_path: Path) -> None:
    """Test 6: additionalProperties:false catches unknown top-level keys."""
    bad = {**REFERENCE_MANIFEST, "weird_key": 1}
    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(_write(tmp_path, bad))
    assert "weird_key" in excinfo.value.args[0]


def test_unsupported_schema_version(tmp_path: Path) -> None:
    """Test 7: schema_version outside support window is rejected."""
    bad = {**REFERENCE_MANIFEST, "schema_version": "99.0"}
    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(_write(tmp_path, bad))
    assert excinfo.value.json_pointer == "/schema_version"
    assert "99.0" in excinfo.value.got
    assert str(list(SUPPORTED_SCHEMA_VERSIONS)) in excinfo.value.expected


def test_returned_manifest_is_frozen(tmp_path: Path) -> None:
    """Test 8: PluginManifest dataclass is frozen — attribute mutation raises."""
    manifest = load_manifest(_write(tmp_path, REFERENCE_MANIFEST))
    with pytest.raises(dataclasses.FrozenInstanceError):
        manifest.name = "other"  # type: ignore[misc]


def test_optional_fields_omitted(tmp_path: Path) -> None:
    """Test 9: manifest without description and audit loads with defaults
    (per Q00/ouroboros-plugins#6 lock — 8 required + 2 optional)."""
    bare = {k: v for k, v in REFERENCE_MANIFEST.items() if k not in ("description", "audit")}
    manifest = load_manifest(_write(tmp_path, bare))
    assert manifest.description == ""
    assert "plugin.invoked" in manifest.audit.events
    assert "plugin.completed" in manifest.audit.events
    assert "plugin.failed" in manifest.audit.events


def test_first_party_source_branch(tmp_path: Path) -> None:
    """Test 10: source.type=first_party loads without requiring path/repository
    (per Q00/ouroboros-plugins#8 lock)."""
    fp = json.loads(json.dumps(REFERENCE_MANIFEST))
    fp["name"] = "ooo-auto"
    fp["source"] = {"type": "first_party"}
    fp["permissions"] = []
    fp["commands"] = [
        {
            "namespace": "auto",
            "name": "run",
            "summary": "Take a goal, run interview, produce Seed, hand off execution.",
            "usage": "ooo auto <goal-text>",
            "risk": "write",
        }
    ]
    manifest = load_manifest(_write(tmp_path, fp))
    assert manifest.source.type == "first_party"
    assert manifest.source.path is None
    assert manifest.source.repository is None


def test_old_risk_enum_value_rejected(tmp_path: Path) -> None:
    """Test 11: command.risk='writes_state' rejected by 3-value enum
    (per Q00/ouroboros-plugins#10 lock)."""
    bad = json.loads(json.dumps(REFERENCE_MANIFEST))
    bad["commands"][0]["risk"] = "writes_state"
    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(_write(tmp_path, bad))
    assert excinfo.value.json_pointer == "/commands/0/risk"


def test_invalid_json_decodes_to_useful_error(tmp_path: Path) -> None:
    """Bonus: garbage JSON is reported with a useful message."""
    target = tmp_path / "ouroboros.plugin.json"
    target.write_text("{invalid json")
    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(target)
    assert "JSON" in excinfo.value.args[0] or "json" in excinfo.value.args[0].lower()


def test_missing_file_reports_clean_error(tmp_path: Path) -> None:
    """Bonus: missing file path reports a clean error, not a stack trace."""
    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(tmp_path / "does-not-exist.json")
    assert "not found" in excinfo.value.args[0]


def test_local_path_source_requires_path(tmp_path: Path) -> None:
    """source.type='local_path' without a `path` is rejected.

    Regression for ouroboros-agent[bot] BLOCKING finding on PR #749 commit
    39ad604: the schema previously only required `source.type`, so
    `local_path` (and `plugin_home`) manifests with no `path` validated
    fine but downstream install/launch had nothing to resolve. The schema
    now applies an `if/then` constraint to require `path` for those types,
    and the loader surfaces the violation with a JSON pointer to /source.
    """
    bad = json.loads(json.dumps(REFERENCE_MANIFEST))
    bad["source"] = {"type": "local_path"}  # no `path`
    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(_write(tmp_path, bad))
    assert excinfo.value.json_pointer.startswith("/source")
    assert "path" in excinfo.value.args[0].lower()


def test_plugin_home_source_requires_path(tmp_path: Path) -> None:
    """source.type='plugin_home' also requires `path` (parallel to local_path)."""
    bad = json.loads(json.dumps(REFERENCE_MANIFEST))
    bad["source"] = {"type": "plugin_home"}
    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(_write(tmp_path, bad))
    assert excinfo.value.json_pointer.startswith("/source")
    assert "path" in excinfo.value.args[0].lower()


def test_first_party_source_does_not_require_path(tmp_path: Path) -> None:
    """source.type='first_party' is the documented exemption — `path` is
    optional. Belt-and-suspenders check that the `if/then` schema branch
    targets only `local_path` / `plugin_home` and does not regress the
    locked first-party behaviour."""
    fp = json.loads(json.dumps(REFERENCE_MANIFEST))
    fp["name"] = "ooo-auto"
    fp["source"] = {"type": "first_party"}
    fp["permissions"] = []
    fp["commands"] = [
        {
            "namespace": "auto",
            "name": "run",
            "summary": "Take a goal, run interview, produce Seed.",
            "usage": "ooo auto <goal-text>",
            "risk": "write",
        }
    ]
    manifest = load_manifest(_write(tmp_path, fp))
    assert manifest.source.type == "first_party"
    assert manifest.source.path is None


def test_capabilities_and_permissions_preserve_declaration_order(tmp_path: Path) -> None:
    """Capabilities and permissions are exposed in manifest declaration order.

    Regression for ouroboros-agent[bot] design-note finding on PR #749:
    `frozenset` storage made multi-permission iteration order
    nondeterministic, so the firewall's `plugin.permission_used` event
    sequence and "first missing scope" message varied between runs. The
    loader now stores both as ordered tuples.
    """
    payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    payload["capabilities"] = [
        {"name": "ledger", "access": "write"},
        {"name": "provenance", "access": "write"},
        {"name": "runtime", "access": "read"},
    ]
    payload["permissions"] = [
        {"scope": "github:read", "risk": "read_only", "required": True},
        {"scope": "github:write", "risk": "write", "required": True},
        {"scope": "ledger:append", "risk": "write", "required": False},
    ]
    manifest = load_manifest(_write(tmp_path, payload))

    assert isinstance(manifest.capabilities, tuple)
    assert isinstance(manifest.permissions, tuple)
    assert [c.name for c in manifest.capabilities] == ["ledger", "provenance", "runtime"]
    assert [p.scope for p in manifest.permissions] == [
        "github:read",
        "github:write",
        "ledger:append",
    ]


def test_duplicate_permission_scope_rejected(tmp_path: Path) -> None:
    """Two permissions with the same scope but different `required` are rejected.

    JSON Schema's `uniqueItems` only catches *whole-item* duplicates, so
    natural-key collisions (same scope, different risk/required) slip
    past schema validation. The loader rejects them with a structured
    JSON pointer.
    """
    bad = json.loads(json.dumps(REFERENCE_MANIFEST))
    bad["permissions"] = [
        {"scope": "github:read", "risk": "read_only", "required": True},
        {"scope": "github:read", "risk": "write", "required": False},
    ]
    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(_write(tmp_path, bad))
    assert excinfo.value.json_pointer == "/permissions/1/scope"
    assert "duplicate permission scope" in excinfo.value.args[0]


def test_blank_entrypoint_command_rejected(tmp_path: Path) -> None:
    """A whitespace-only `entrypoint.command` is rejected at load time.

    Regression for ouroboros-agent[bot] BLOCKING finding on PR #749 commit
    d86743c: the schema only enforced `minLength: 1`, so a command like
    "   " loaded fine. At invocation time `shlex.split` yields zero
    tokens, the firewall would then prepend the subcommand name and try
    to exec it as the binary — a misleading runtime failure after
    `plugin.invoked` was already emitted. The loader now rejects this
    at the manifest boundary so the runtime path is never reached.
    """
    bad = json.loads(json.dumps(REFERENCE_MANIFEST))
    bad["entrypoint"]["command"] = "   "
    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(_write(tmp_path, bad))
    assert excinfo.value.json_pointer == "/entrypoint/command"
    assert "non-whitespace token" in excinfo.value.args[0]


def test_duplicate_command_name_rejected(tmp_path: Path) -> None:
    """Two commands sharing a name within one manifest are rejected.

    Regression for ouroboros-agent[bot] follow-up finding on PR #749 commit
    e0112d3: `RegisteredProgram.find_command()` returns the first match by
    name, so a second command with the same name is silently unreachable
    and its risk/requires_confirmation settings are ignored. The loader
    now rejects this with a structured pointer, mirroring the
    per-permission and per-capability uniqueness checks.
    """
    bad = json.loads(json.dumps(REFERENCE_MANIFEST))
    bad["commands"] = [
        {
            "namespace": "github-pr",
            "name": "review",
            "summary": "Read-only review.",
            "usage": "ooo github-pr review <url>",
            "risk": "read_only",
        },
        {
            "namespace": "github-pr",
            "name": "review",  # duplicate name with destructive risk
            "summary": "Hidden destructive variant.",
            "usage": "ooo github-pr review <url>",
            "risk": "destructive",
            "requires_confirmation": True,
        },
    ]
    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(_write(tmp_path, bad))
    assert excinfo.value.json_pointer == "/commands/1/name"
    assert "duplicate command name" in excinfo.value.args[0]


def test_duplicate_capability_name_rejected(tmp_path: Path) -> None:
    """Two capabilities sharing a name (with different access) are rejected."""
    bad = json.loads(json.dumps(REFERENCE_MANIFEST))
    bad["capabilities"] = [
        {"name": "ledger", "access": "write"},
        {"name": "ledger", "access": "read"},
    ]
    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(_write(tmp_path, bad))
    assert excinfo.value.json_pointer == "/capabilities/1/name"
    assert "duplicate capability name" in excinfo.value.args[0]
