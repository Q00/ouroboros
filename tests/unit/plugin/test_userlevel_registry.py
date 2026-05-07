"""Tests for the UserLevel program registry (Q00/ouroboros#730)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ouroboros.plugin.manifest import load_manifest
from ouroboros.plugin.userlevel_registry import (
    RegisteredProgram,
    RegistryError,
    UserLevelProgramRegistry,
    get_userlevel_registry,
    lookup_command,
    reset_userlevel_registry,
)

REFERENCE_MANIFEST: dict = {
    "schema_version": "0.1",
    "name": "github-pr-ops",
    "version": "0.1.0",
    "source": {"type": "local_path", "path": "plugins/github-pr-ops"},
    "commands": [
        {
            "namespace": "github-pr",
            "name": "review",
            "summary": "Review a pull request and summarize readiness.",
            "usage": "ooo github-pr review <pull-request-url>",
            "risk": "read_only",
        }
    ],
    "capabilities": [
        {"name": "ledger", "access": "write"},
    ],
    "permissions": [
        {"scope": "github:read", "risk": "read_only", "required": True},
    ],
    "entrypoint": {"type": "command", "command": "python -m github_pr_ops"},
}


@pytest.fixture(autouse=True)
def _reset_global_registry():
    """Each test starts with a clean global registry."""
    reset_userlevel_registry()
    yield
    reset_userlevel_registry()


def _write_manifest(tmp_path: Path, payload: dict) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "ouroboros.plugin.json"
    target.write_text(json.dumps(payload))
    return target


def _load_ref(tmp_path: Path, **overrides) -> RegisteredProgram:
    payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    payload.update(overrides)
    return load_manifest(_write_manifest(tmp_path, payload))


def test_register_and_get(tmp_path: Path) -> None:
    """Test 1: register a manifest, look it up by name and namespace."""
    registry = UserLevelProgramRegistry()
    manifest = _load_ref(tmp_path)
    program = registry.register(manifest)

    assert isinstance(program, RegisteredProgram)
    assert registry.get("github-pr-ops") == program
    assert registry.get_by_namespace("github-pr") == program
    assert registry.get("nonexistent") is None
    assert registry.get_by_namespace("nonexistent") is None


def test_namespace_collision_rejected(tmp_path: Path) -> None:
    """Test 2: two plugins claiming the same namespace → error."""
    registry = UserLevelProgramRegistry()
    a = _load_ref(tmp_path / "a", name="plugin-a")
    b = _load_ref(tmp_path / "b", name="plugin-b")  # same namespace "github-pr"

    registry.register(a)
    with pytest.raises(RegistryError, match="already owned"):
        registry.register(b)


def test_duplicate_name_requires_replace(tmp_path: Path) -> None:
    """Test 3: re-registering the same name without replace=True → error."""
    registry = UserLevelProgramRegistry()
    manifest = _load_ref(tmp_path)
    registry.register(manifest)
    with pytest.raises(RegistryError, match="already registered"):
        registry.register(manifest)
    # With replace=True, succeeds.
    registry.register(manifest, replace=True)


def test_unregister(tmp_path: Path) -> None:
    """Test 4: unregister releases name AND namespace ownership."""
    registry = UserLevelProgramRegistry()
    manifest = _load_ref(tmp_path)
    registry.register(manifest)
    assert registry.unregister("github-pr-ops") is True
    assert registry.unregister("github-pr-ops") is False
    # After unregistering, the namespace can be claimed by a different plugin.
    other = _load_ref(tmp_path / "other", name="plugin-other")
    registry.register(other)  # must not raise


def test_all_programs(tmp_path: Path) -> None:
    """Test 5: all_programs returns every registered program."""
    registry = UserLevelProgramRegistry()
    a = _load_ref(tmp_path / "a", name="plugin-a")
    # Different namespace for b
    b_payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    b_payload["name"] = "plugin-b"
    b_payload["commands"][0]["namespace"] = "other-ns"
    b = load_manifest(_write_manifest(tmp_path / "b", b_payload))
    registry.register(a)
    registry.register(b)
    names = {p.name for p in registry.all_programs()}
    assert names == {"plugin-a", "plugin-b"}


def test_lookup_command_finds_userlevel_by_namespace(tmp_path: Path) -> None:
    """Test 6: lookup_command finds via namespace first."""
    manifest = _load_ref(tmp_path)
    get_userlevel_registry().register(manifest)
    result = lookup_command("github-pr")
    assert result.found
    assert result.kind == "userlevel"
    assert result.userlevel_program is not None
    assert result.userlevel_program.name == "github-pr-ops"


def test_lookup_command_finds_userlevel_by_name(tmp_path: Path) -> None:
    """Test 7: lookup_command also finds via plugin name."""
    manifest = _load_ref(tmp_path)
    get_userlevel_registry().register(manifest)
    result = lookup_command("github-pr-ops")
    assert result.found
    assert result.kind == "userlevel"


def test_lookup_command_returns_none_for_unknown(tmp_path: Path) -> None:
    """Test 8: lookup_command returns kind='none' for unknown names."""
    result = lookup_command("totally-unknown-plugin")
    assert not result.found
    assert result.kind == "none"


def test_global_singleton_persists_within_session(tmp_path: Path) -> None:
    """Test 9: get_userlevel_registry returns the same instance."""
    a = get_userlevel_registry()
    b = get_userlevel_registry()
    assert a is b


def test_replace_with_changed_namespace_releases_old_namespace(tmp_path: Path) -> None:
    """`register(replace=True)` on a plugin that changed namespace must
    free the old namespace mapping.

    Pre-fix, `_namespace_owner` retained both the stale old namespace and
    the new one pointing at the same plugin name. A later
    `unregister(name)` only popped the *new* namespace, leaving the old
    one permanently shadowed and rejecting any other plugin that tried to
    claim it.
    """
    registry = UserLevelProgramRegistry()

    v1 = _load_ref(tmp_path / "v1")  # namespace="github-pr"
    registry.register(v1)

    # Re-register the same plugin name with a NEW namespace, replace=True.
    v2_payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    v2_payload["version"] = "0.2.0"
    v2_payload["commands"][0]["namespace"] = "github-pr-v2"
    v2 = load_manifest(_write_manifest(tmp_path / "v2", v2_payload))
    registry.register(v2, replace=True)

    # Old namespace must be free again.
    assert registry.get_by_namespace("github-pr") is None
    # New namespace owns the program.
    found = registry.get_by_namespace("github-pr-v2")
    assert found is not None
    assert found.manifest.version == "0.2.0"

    # And a different plugin can now claim the old namespace.
    other_payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    other_payload["name"] = "plugin-other"
    other = load_manifest(_write_manifest(tmp_path / "other", other_payload))
    registry.register(other)  # must not raise — namespace "github-pr" is free

    # Unregistering the replaced plugin must drop only its current ns.
    assert registry.unregister("github-pr-ops") is True
    assert registry.get_by_namespace("github-pr-v2") is None
    # The other plugin's ownership of "github-pr" is unaffected.
    other_found = registry.get_by_namespace("github-pr")
    assert other_found is not None
    assert other_found.name == "plugin-other"


def test_skill_registry_unaffected(tmp_path: Path) -> None:
    """Test 10: existing skill registry tests still work — no state shared.

    This test loads the skill registry module to confirm we can co-exist
    with it (no import-time conflicts), without depending on its discovery.
    """
    from ouroboros.plugin.skills.registry import get_registry as _get_skill_registry

    skill_registry = _get_skill_registry()
    # The skill registry is independent from our UserLevel registry.
    ul = get_userlevel_registry()
    manifest = _load_ref(tmp_path)
    ul.register(manifest)
    # Skill registry must remain unchanged by our registration.
    assert skill_registry.get_skill("github-pr-ops") is None
