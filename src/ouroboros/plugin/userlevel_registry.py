"""UserLevel program registry.

Tracks installed UserLevel plugins (and first-party programs) by their
manifest. Sits alongside the existing `skills/registry.py` SkillRegistry —
both are queryable via the top-level `lookup_command(...)` helper at the
bottom of this module.

Per the locked Q00/ouroboros#730 spec:
  - One in-memory registry shared across the process.
  - Namespace ownership: the first plugin to register `namespace=foo`
    owns it; subsequent registrations for the same namespace are
    rejected with a clear error.
  - Names ARE used as primary keys (one program per name); re-registering
    the same name without explicit replace is rejected.
  - The registry is decoupled from discovery — callers (CLI, firewall,
    integration tests) build a registry from already-loaded `PluginManifest`
    instances.

This module deliberately does NOT subsume or modify the SkillRegistry. The
two cover different artifact shapes (JSON manifest vs SKILL.md frontmatter)
and have different lifecycle semantics (install/trust vs hot-reload). They
share only the cross-registry lookup helper at the bottom.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from ouroboros.plugin.manifest import CommandSpec, PluginManifest


class RegistryError(Exception):
    """Raised on namespace collision, duplicate registration, etc."""


@dataclass(frozen=True)
class RegisteredProgram:
    """One entry in the UserLevel program registry.

    The registry stores manifest references rather than copies — the
    manifest is already frozen and value-equal.
    """

    manifest: PluginManifest

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def namespace(self) -> str:
        # All commands of one plugin share one namespace per the schema's
        # pattern + the manager's collision check; pick the first.
        return self.manifest.commands[0].namespace

    def find_command(self, name: str) -> CommandSpec | None:
        for command in self.manifest.commands:
            if command.name == name:
                return command
        return None


class UserLevelProgramRegistry:
    """In-memory registry of installed UserLevel programs."""

    def __init__(self) -> None:
        self._by_name: dict[str, RegisteredProgram] = {}
        self._namespace_owner: dict[str, str] = {}  # namespace -> plugin name
        self._lock = RLock()

    def register(self, manifest: PluginManifest, *, replace: bool = False) -> RegisteredProgram:
        """Register a program from its loaded manifest.

        Args:
            manifest: The validated `PluginManifest` (from `load_manifest`).
            replace: If True, replace an existing entry with the same name.
                Default False — duplicate registrations raise.

        Returns:
            The registered program.

        Raises:
            RegistryError: on namespace collision or duplicate name without
                `replace=True`.
        """
        if not manifest.commands:
            raise RegistryError(f"{manifest.name}: manifest has no commands")

        # All commands must share the same namespace per the schema's pattern.
        namespaces = {c.namespace for c in manifest.commands}
        if len(namespaces) != 1:
            raise RegistryError(
                f"{manifest.name}: commands declare multiple namespaces "
                f"{sorted(namespaces)}; one plugin must own one namespace"
            )
        namespace = namespaces.pop()

        with self._lock:
            existing = self._by_name.get(manifest.name)
            if existing is not None and not replace:
                raise RegistryError(
                    f"{manifest.name} is already registered "
                    f"(version {existing.manifest.version}); pass replace=True to update"
                )

            owner = self._namespace_owner.get(namespace)
            if owner is not None and owner != manifest.name:
                raise RegistryError(
                    f"namespace {namespace!r} already owned by {owner!r}; "
                    f"refusing to register {manifest.name!r}"
                )

            # When `replace=True` is used to update a plugin that previously
            # claimed a different namespace, drop the stale mapping so the
            # old namespace becomes free again. Without this, the old entry
            # in `_namespace_owner` keeps pointing at this plugin and a
            # subsequent `unregister(name)` only frees the *new* namespace,
            # leaving the old one permanently shadowed.
            if existing is not None:
                old_namespace = existing.namespace
                if old_namespace != namespace and (
                    self._namespace_owner.get(old_namespace) == manifest.name
                ):
                    self._namespace_owner.pop(old_namespace, None)

            program = RegisteredProgram(manifest=manifest)
            self._by_name[manifest.name] = program
            self._namespace_owner[namespace] = manifest.name
            return program

    def unregister(self, name: str) -> bool:
        """Remove a program by name. Returns True if removed."""
        with self._lock:
            program = self._by_name.pop(name, None)
            if program is None:
                return False
            ns = program.namespace
            if self._namespace_owner.get(ns) == name:
                self._namespace_owner.pop(ns)
            return True

    def get(self, name: str) -> RegisteredProgram | None:
        with self._lock:
            return self._by_name.get(name)

    def get_by_namespace(self, namespace: str) -> RegisteredProgram | None:
        with self._lock:
            owner = self._namespace_owner.get(namespace)
            if owner is None:
                return None
            return self._by_name.get(owner)

    def all_programs(self) -> list[RegisteredProgram]:
        with self._lock:
            return list(self._by_name.values())


# Global singleton — modeled after skills/registry.py's pattern.
_global: UserLevelProgramRegistry | None = None
_global_lock = RLock()


def get_userlevel_registry() -> UserLevelProgramRegistry:
    """Return the process-wide UserLevel program registry singleton."""
    global _global
    with _global_lock:
        if _global is None:
            _global = UserLevelProgramRegistry()
        return _global


def reset_userlevel_registry() -> None:
    """Reset the singleton. Tests use this to isolate state."""
    global _global
    with _global_lock:
        _global = None


# ---------------------------------------------------------------------------
# Cross-registry lookup
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LookupResult:
    """Result of looking up a name across both registries.

    Exactly one of `userlevel_program` or `skill_metadata` is non-None for
    a successful lookup.
    """

    kind: str  # "userlevel" | "skill" | "none"
    userlevel_program: RegisteredProgram | None = None
    skill_metadata: object | None = None  # Avoid hard import on skill module.

    @property
    def found(self) -> bool:
        return self.kind != "none"


def lookup_command(name: str) -> LookupResult:
    """Look up a name across both the UserLevel registry and the bundled
    skills registry. Returns the first match.

    Order:
      1. UserLevel namespaces (e.g. "github-pr") — fast, in-memory.
      2. UserLevel plugin names (e.g. "github-pr-ops").
      3. Bundled skill names (loaded from .claude-plugin/skills/).

    Args:
        name: The command name, namespace, or plugin name to look up.

    Returns:
        `LookupResult` indicating which registry matched.
    """
    ul = get_userlevel_registry()

    program = ul.get_by_namespace(name)
    if program is not None:
        return LookupResult(kind="userlevel", userlevel_program=program)

    program = ul.get(name)
    if program is not None:
        return LookupResult(kind="userlevel", userlevel_program=program)

    # Skills lookup: import lazily so this module doesn't pull in watchdog
    # and yaml at import time.
    try:
        from ouroboros.plugin.skills.registry import get_registry as _get_skill_registry
    except ImportError:  # pragma: no cover - defensive
        return LookupResult(kind="none")

    skill_registry = _get_skill_registry()
    skill = skill_registry.get_skill(name)
    if skill is not None:
        return LookupResult(kind="skill", skill_metadata=skill.metadata)

    return LookupResult(kind="none")


__all__ = [
    "LookupResult",
    "RegisteredProgram",
    "RegistryError",
    "UserLevelProgramRegistry",
    "get_userlevel_registry",
    "lookup_command",
    "reset_userlevel_registry",
]
