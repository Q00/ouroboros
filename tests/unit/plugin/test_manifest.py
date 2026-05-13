"""Tests for the plugin manifest loader (Q00/ouroboros#728).

Each test asserts BOTH the rejection AND the JSON Pointer to the failing
field, so a future schema change cannot silently relax constraints.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import sys

import pytest

from ouroboros.plugin.ledger_adapter import AUDIT_EVENT_TYPES
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


def test_audit_events_accept_full_explicit_vocabulary(tmp_path: Path) -> None:
    """Manifests may opt into any event type the audit-event schema emits."""
    raw = json.loads(json.dumps(REFERENCE_MANIFEST))
    raw["audit"] = {"events": list(AUDIT_EVENT_TYPES)}

    manifest = load_manifest(_write(tmp_path, raw))

    assert manifest.audit.events == AUDIT_EVENT_TYPES


def test_audit_events_reject_unknown_names(tmp_path: Path) -> None:
    """Keep the manifest audit contract closed to the explicit vocabulary."""
    raw = json.loads(json.dumps(REFERENCE_MANIFEST))
    raw["audit"] = {"events": ["plugin.hook_started", "plugin.not_real"]}

    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(_write(tmp_path, raw))

    assert excinfo.value.json_pointer == "/audit/events/1"


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


def test_local_path_source_requires_path(tmp_path: Path) -> None:
    """source.type=local_path must reject manifests that omit `path`.

    Without the conditional `required`, a manifest like
    `{"source": {"type": "local_path"}}` would validate and the loader
    would return `SourceSpec(path=None)`, pushing the failure into
    downstream code instead of catching it at load time.
    """
    bad = json.loads(json.dumps(REFERENCE_MANIFEST))
    bad["source"] = {"type": "local_path"}
    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(_write(tmp_path, bad))
    err = excinfo.value
    assert err.json_pointer is not None
    assert err.json_pointer.startswith("/source")
    assert "path" in err.args[0]


def test_plugin_home_source_requires_path(tmp_path: Path) -> None:
    """source.type=plugin_home must also reject manifests that omit `path`.

    plugin_home sources reference a slot under the user's plugin home
    directory; the loader needs the relative path to resolve them.
    """
    bad = json.loads(json.dumps(REFERENCE_MANIFEST))
    bad["source"] = {"type": "plugin_home"}
    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(_write(tmp_path, bad))
    err = excinfo.value
    assert err.json_pointer is not None
    assert err.json_pointer.startswith("/source")
    assert "path" in err.args[0]


def test_local_path_source_with_path_loads(tmp_path: Path) -> None:
    """Positive control: source.type=local_path with `path` loads cleanly."""
    fp = json.loads(json.dumps(REFERENCE_MANIFEST))
    fp["source"] = {"type": "local_path", "path": "plugins/whatever"}
    manifest = load_manifest(_write(tmp_path, fp))
    assert manifest.source.type == "local_path"
    assert manifest.source.path == "plugins/whatever"


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


def test_non_utf8_manifest_reports_structured_error(tmp_path: Path) -> None:
    """Non-UTF-8 manifest bytes must surface as PluginManifestError, not
    a raw UnicodeDecodeError (structured-error contract)."""
    target = tmp_path / "ouroboros.plugin.json"
    target.write_bytes(b"\xff\xfe\x00not utf-8")
    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(target)
    assert "UTF-8" in excinfo.value.args[0] or "utf-8" in excinfo.value.args[0]
    assert excinfo.value.path == str(target)


@pytest.mark.parametrize(
    "bad_path",
    [
        # POSIX absolute / traversal
        "/etc/passwd",
        "/absolute/install",
        "../escape",
        "a/../escape",
        "nested/../../escape",
        # Windows drive prefix — must be rejected even on POSIX hosts because
        # the manifest may be consumed on Windows where ntpath treats these
        # as absolute.
        "C:/Windows/System32",
        "c:foo",
        # Backslash separator — never legal in a POSIX-slug source.path,
        # and accepting it on POSIX would let a Windows consumer's
        # `ntpath.join` interpret it as parent traversal.
        "..\\escape",
        "foo\\..\\bar",
        "C:\\Windows",
    ],
)
@pytest.mark.parametrize("source_type", ["local_path", "plugin_home"])
def test_sandboxed_source_path_rejects_traversal(
    tmp_path: Path, source_type: str, bad_path: str
) -> None:
    """Path-bearing source types must reject absolute paths and `..` segments
    in both POSIX and Windows forms.

    Without this, a `plugin_home` manifest could declare
    `source.path = "C:/Windows/System32"` or `"..\\foo"` and the loader
    would happily return it — turning a naive downstream `os.path.join`
    on the consumer side into a sandbox escape. Validation has to be
    platform-agnostic because the host that loads the manifest may not
    be the host that resolves it.
    """
    bad = json.loads(json.dumps(REFERENCE_MANIFEST))
    bad["source"] = {"type": source_type, "path": bad_path}
    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(_write(tmp_path, bad))
    err = excinfo.value
    assert err.json_pointer == "/source/path"
    assert err.got == bad_path


def test_plugin_home_with_relative_path_loads(tmp_path: Path) -> None:
    """Positive control: a sandboxed relative `plugin_home` path loads."""
    fp = json.loads(json.dumps(REFERENCE_MANIFEST))
    fp["source"] = {"type": "plugin_home", "path": "vendor/ooo-pr-ops"}
    manifest = load_manifest(_write(tmp_path, fp))
    assert manifest.source.type == "plugin_home"
    assert manifest.source.path == "vendor/ooo-pr-ops"


def test_vendored_schemas_are_packaged_resources() -> None:
    """The vendored schema must be reachable through `importlib.resources`,
    not via a filesystem-relative read.

    A wheel built without explicit `force-include` may silently drop the
    `schemas/` directory, and `_load_schema()` would then raise
    `vendored schema directory missing from installed package` for every
    manifest load. Asserting the resource is reachable here gives that
    failure mode a unit-test guard alongside the hatch packaging config.
    """
    from importlib import resources

    schema_pkg = resources.files("ouroboros.plugin.schemas")
    for version in SUPPORTED_SCHEMA_VERSIONS:
        plugin_schema = schema_pkg.joinpath(version).joinpath("plugin.schema.json")
        assert plugin_schema.is_file(), f"plugin.schema.json missing for v{version}"
        # And it must be parseable JSON, not an empty placeholder.
        body = plugin_schema.read_text(encoding="utf-8")
        assert json.loads(body)["title"].startswith("Ouroboros Plugin Manifest")


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="POSIX permission semantics; Windows handles read perms differently.",
)
def test_unreadable_manifest_reports_structured_error(tmp_path: Path) -> None:
    """Permission-denied reads must surface as PluginManifestError, not
    a raw OSError (structured-error contract)."""
    target = _write(tmp_path, REFERENCE_MANIFEST)
    target.chmod(0o000)
    try:
        # Skip if running as root, where chmod 0o000 cannot deny reads.
        if os.geteuid() == 0:
            pytest.skip("root bypasses POSIX read permissions")
        with pytest.raises(PluginManifestError) as excinfo:
            load_manifest(target)
        assert "unreadable" in excinfo.value.args[0]
        assert excinfo.value.path == str(target)
    finally:
        target.chmod(0o644)
