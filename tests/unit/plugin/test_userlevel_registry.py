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


def test_replace_with_different_namespace_releases_old(tmp_path: Path) -> None:
    """Regression: replace=True must release the previously owned namespace.

    Before the fix at userlevel_registry.py:register, a plugin re-registering
    under a *different* namespace left a stale entry in `_namespace_owner`,
    so `get_by_namespace(old_ns)` returned the wrong program and other
    plugins could never claim the old namespace again.
    """
    registry = UserLevelProgramRegistry()

    old = _load_ref(tmp_path / "old")  # name="github-pr-ops", namespace="github-pr"
    registry.register(old)

    # Re-register under a brand new namespace; same plugin name.
    new_payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    new_payload["commands"][0]["namespace"] = "pr-tools"
    new_payload["commands"][0]["usage"] = "ooo pr-tools review <pull-request-url>"
    new = load_manifest(_write_manifest(tmp_path / "new", new_payload))
    registry.register(new, replace=True)

    # Old namespace must be fully released.
    assert registry.get_by_namespace("github-pr") is None
    # New namespace owns the plugin.
    program = registry.get_by_namespace("pr-tools")
    assert program is not None
    assert program.namespace == "pr-tools"
    # And another plugin can now claim the old namespace. Give it a
    # distinct command name — the global command-name uniqueness rule means
    # we cannot reuse "review" while github-pr-ops still owns it.
    other_payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    other_payload["name"] = "plugin-other"
    other_payload["commands"][0]["name"] = "scan"
    other_payload["commands"][0]["usage"] = "ooo github-pr scan <url>"
    other = load_manifest(_write_manifest(tmp_path / "other", other_payload))
    registry.register(other)
    other_program = registry.get_by_namespace("github-pr")
    assert other_program is not None
    assert other_program.name == "plugin-other"

    # unregister still cleans up the *current* namespace.
    assert registry.unregister("github-pr-ops") is True
    assert registry.get_by_namespace("pr-tools") is None
    # And old plugin's previous (already released) namespace remains owned by
    # the third plugin we registered above.
    assert registry.get_by_namespace("github-pr") is not None


def test_all_programs(tmp_path: Path) -> None:
    """Test 5: all_programs returns every registered program."""
    registry = UserLevelProgramRegistry()
    a = _load_ref(tmp_path / "a", name="plugin-a")
    # Distinct namespace AND command name for b — both must be unique
    # globally now that command-name collisions are rejected.
    b_payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    b_payload["name"] = "plugin-b"
    b_payload["commands"][0]["namespace"] = "other-ns"
    b_payload["commands"][0]["name"] = "audit"
    b_payload["commands"][0]["usage"] = "ooo other-ns audit"
    b = load_manifest(_write_manifest(tmp_path / "b", b_payload))
    registry.register(a)
    registry.register(b)
    names = {p.name for p in registry.all_programs()}
    assert names == {"plugin-a", "plugin-b"}


def test_within_manifest_duplicate_command_rejected(tmp_path: Path) -> None:
    """A single manifest declaring the same command name twice (even within
    the same namespace) is rejected. The vendored 0.1 schema doesn't
    enforce this, but the registry treats command names as a primary key
    so the duplicate would otherwise hide the second spec from
    `find_command()` and `get_by_command()`.
    """
    registry = UserLevelProgramRegistry()
    payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    payload["commands"] = [
        payload["commands"][0],
        {
            "namespace": "github-pr",
            "name": "review",  # duplicate
            "summary": "A second `review` definition.",
            "usage": "ooo github-pr review --shadow",
            "risk": "read_only",
        },
    ]
    manifest = load_manifest(_write_manifest(tmp_path / "dup", payload))
    with pytest.raises(RegistryError, match="duplicate command name"):
        registry.register(manifest)


def test_command_name_collision_rejected(tmp_path: Path) -> None:
    """Two plugins claiming the same bare command name → error.

    Without this rule, `lookup_command("review")` would resolve to
    whichever plugin happened to register first, producing nondeterministic
    dispatch.
    """
    registry = UserLevelProgramRegistry()
    a = _load_ref(tmp_path / "a", name="plugin-a")  # command "review" / ns "github-pr"
    b_payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    b_payload["name"] = "plugin-b"
    b_payload["commands"][0]["namespace"] = "other-ns"
    # Same command name "review" — collision on a different axis from namespace.
    b = load_manifest(_write_manifest(tmp_path / "b", b_payload))
    registry.register(a)
    with pytest.raises(RegistryError, match="shadow command already owned"):
        registry.register(b)


def test_cross_axis_collision_rejected(tmp_path: Path) -> None:
    """A name reserved on one axis (plugin name, namespace, command name)
    cannot be reused on a different axis by a different plugin.

    Without this guard, plugin-A could register `name="review"` and
    plugin-B could register `command name="review"` in some other
    namespace, after which `lookup_command("review")` would always
    resolve to plugin-A by the higher-priority "plugin name" arm and
    plugin-B's `review` command would be unreachable. The same axis
    asymmetry holds for namespace vs command name and plugin name vs
    namespace, so the registry rejects any cross-axis shadowing.
    """
    registry = UserLevelProgramRegistry()

    # Case 1: plugin name vs another plugin's command name.
    a_payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    a_payload["name"] = "review"  # plugin name == "review"
    a_payload["commands"][0]["namespace"] = "namespace-a"
    a_payload["commands"][0]["name"] = "run"
    a_payload["commands"][0]["usage"] = "ooo namespace-a run"
    a = load_manifest(_write_manifest(tmp_path / "a", a_payload))
    registry.register(a)

    b_payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    b_payload["name"] = "plugin-b"
    b_payload["commands"][0]["namespace"] = "namespace-b"
    b_payload["commands"][0]["name"] = "review"  # collides with plugin-A's plugin name
    b_payload["commands"][0]["usage"] = "ooo namespace-b review"
    b = load_manifest(_write_manifest(tmp_path / "b", b_payload))
    with pytest.raises(RegistryError, match="shadow existing plugin name"):
        registry.register(b)

    # Case 2: namespace vs another plugin's plugin name.
    c_payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    c_payload["name"] = "plugin-c"
    c_payload["commands"][0]["namespace"] = "review"  # collides with plugin-A's plugin name
    c_payload["commands"][0]["name"] = "scan"
    c_payload["commands"][0]["usage"] = "ooo review scan"
    c = load_manifest(_write_manifest(tmp_path / "c", c_payload))
    with pytest.raises(RegistryError, match="shadow existing plugin name"):
        registry.register(c)

    # Case 3: namespace vs another plugin's command name.
    d_payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    d_payload["name"] = "plugin-d"
    d_payload["commands"][0]["namespace"] = "run"  # collides with plugin-A's command "run"
    d_payload["commands"][0]["name"] = "trace"
    d_payload["commands"][0]["usage"] = "ooo run trace"
    d = load_manifest(_write_manifest(tmp_path / "d", d_payload))
    with pytest.raises(RegistryError, match="shadow command already owned"):
        registry.register(d)


def test_replace_with_different_commands_releases_old(tmp_path: Path) -> None:
    """Regression: replace=True must release command names the previous
    incarnation owned but the new manifest no longer claims, and another
    plugin must then be allowed to register the released command."""
    registry = UserLevelProgramRegistry()

    # Old manifest exposes two commands.
    old_payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    old_payload["commands"] = [
        old_payload["commands"][0],
        {
            "namespace": "github-pr",
            "name": "audit",
            "summary": "Audit a pull request.",
            "usage": "ooo github-pr audit <url>",
            "risk": "read_only",
        },
    ]
    old = load_manifest(_write_manifest(tmp_path / "old", old_payload))
    registry.register(old)

    # New manifest drops "audit"; same plugin name, replace=True.
    new_payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    new = load_manifest(_write_manifest(tmp_path / "new", new_payload))
    registry.register(new, replace=True)

    # "audit" is no longer claimed → another plugin can take it.
    other_payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    other_payload["name"] = "auditor"
    other_payload["commands"][0]["namespace"] = "auditor"
    other_payload["commands"][0]["name"] = "audit"
    other_payload["commands"][0]["usage"] = "ooo auditor audit"
    other = load_manifest(_write_manifest(tmp_path / "other", other_payload))
    registry.register(other)
    by_command = registry.get_by_command("audit")
    assert by_command is not None
    assert by_command.name == "auditor"
    # And "review" still routes to the original plugin.
    review_owner = registry.get_by_command("review")
    assert review_owner is not None
    assert review_owner.name == "github-pr-ops"


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


def test_lookup_command_finds_userlevel_by_command_name(tmp_path: Path) -> None:
    """lookup_command resolves bare command names (e.g. `review`) against
    the registered programs' command lists, matching the docstring contract
    of name | namespace | plugin name."""
    manifest = _load_ref(tmp_path)  # has a "review" command in the github-pr namespace
    get_userlevel_registry().register(manifest)
    result = lookup_command("review")
    assert result.found
    assert result.kind == "userlevel"
    assert result.userlevel_program is not None
    assert result.userlevel_program.name == "github-pr-ops"


def test_lookup_command_finds_bundled_skill_after_discover(tmp_path: Path) -> None:
    """lookup_command returns kind='skill' once the bundled SkillRegistry
    has been driven through discover_all(). The lookup deliberately does
    not trigger discovery itself; this test exercises the post-discovery
    success path the bot flagged as previously uncovered."""
    import asyncio

    from ouroboros.plugin.skills import registry as skill_registry_module
    from ouroboros.plugin.skills.registry import SkillRegistry

    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    skill = skill_root / "demo-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\n"
        "description: A demo skill for the lookup test.\n"
        "triggers:\n"
        "  - demo\n"
        "---\n\n"
        "# Demo Skill\n\nBody.\n"
    )

    fresh = SkillRegistry(skill_dir=skill_root)
    asyncio.run(fresh.discover_all())

    # Substitute the global skill registry singleton so lookup_command's
    # lazy `get_registry()` returns our preloaded one.
    original = skill_registry_module._global_registry
    skill_registry_module._global_registry = fresh
    try:
        result = lookup_command("demo-skill")
        assert result.found
        assert result.kind == "skill"
        assert result.skill_metadata is not None
    finally:
        skill_registry_module._global_registry = original


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
