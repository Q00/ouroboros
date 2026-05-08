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
    """In-memory registry of installed UserLevel programs.

    Maintains three indexes that must stay in lockstep with `_by_name`:

    - `_namespace_owner` — `namespace → plugin name` (one namespace per
      plugin).
    - `_command_owner` — `command name → plugin name` (command names are
      globally unique because `lookup_command(name)` resolves bare command
      names; without uniqueness the same name could dispatch to different
      plugins depending on registration order).

    Every mutation (`register`, `unregister`) is performed under `_lock`
    and updates all relevant indexes atomically; tests that manipulate
    these indexes from the outside are expected to use the public API.
    """

    def __init__(self) -> None:
        self._by_name: dict[str, RegisteredProgram] = {}
        self._namespace_owner: dict[str, str] = {}  # namespace -> plugin name
        self._command_owner: dict[str, str] = {}  # command name -> plugin name
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
            RegistryError: on namespace collision, command-name collision
                across plugins, or duplicate plugin name without
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

        # Within-manifest command-name uniqueness. The vendored 0.1 schema
        # does not enforce uniqueness on `commands[*].name`, but the
        # registry treats command names as a primary lookup key — without
        # this guard a manifest with two commands named `review` would
        # accept silently and `find_command()` / `get_by_command()` would
        # always return the first array entry, hiding the second spec.
        seen: set[str] = set()
        duplicates: list[str] = []
        for c in manifest.commands:
            if c.name in seen and c.name not in duplicates:
                duplicates.append(c.name)
            seen.add(c.name)
        if duplicates:
            raise RegistryError(
                f"{manifest.name}: duplicate command name(s) within manifest: {sorted(duplicates)}"
            )
        new_command_names = seen

        with self._lock:
            existing = self._by_name.get(manifest.name)
            if existing is not None and not replace:
                raise RegistryError(
                    f"{manifest.name} is already registered "
                    f"(version {existing.manifest.version}); pass replace=True to update"
                )

            # Cross-axis collision check.
            # `lookup_command()` resolves one string across three axes in
            # priority order (namespace → plugin name → command name). If
            # the same string is reserved on a higher-priority axis by
            # another plugin, the lower-priority match becomes unreachable.
            # Treat the union of (incoming namespace, plugin name, command
            # names) as a single identifier space; any collision with an
            # axis owned by *another* plugin is rejected. Self-owned
            # identifiers are exempt because the replace branch below
            # releases stale ones before the new ones are installed.
            new_identifiers = {manifest.name, namespace} | new_command_names
            other_namespaces = {
                ns: o for ns, o in self._namespace_owner.items() if o != manifest.name
            }
            other_plugin_names = {n for n in self._by_name if n != manifest.name}
            other_commands = {c: o for c, o in self._command_owner.items() if o != manifest.name}
            for ident in new_identifiers:
                if ident in other_namespaces:
                    raise RegistryError(
                        f"identifier {ident!r} would shadow namespace "
                        f"already owned by {other_namespaces[ident]!r}; "
                        f"refusing to register {manifest.name!r}"
                    )
                if ident in other_plugin_names:
                    raise RegistryError(
                        f"identifier {ident!r} would shadow existing plugin name; "
                        f"refusing to register {manifest.name!r}"
                    )
                if ident in other_commands:
                    raise RegistryError(
                        f"identifier {ident!r} would shadow command "
                        f"already owned by plugin {other_commands[ident]!r}; "
                        f"refusing to register {manifest.name!r}"
                    )

            # Replace must be transactional across all indexes: release any
            # namespace and command-name slots the previous incarnation owned
            # but the new manifest no longer claims. Without this, stale
            # entries linger and either block future registrations or skew
            # the lookup result for a name that should be free. Main's
            # transactional release strictly supersedes the namespace-only
            # release the PR previously shipped, so we adopt it wholesale.
            if existing is not None:
                if existing.namespace != namespace and (
                    self._namespace_owner.get(existing.namespace) == manifest.name
                ):
                    self._namespace_owner.pop(existing.namespace)
                old_command_names = {c.name for c in existing.manifest.commands}
                for stale in old_command_names - new_command_names:
                    if self._command_owner.get(stale) == manifest.name:
                        self._command_owner.pop(stale)

            program = RegisteredProgram(manifest=manifest)
            self._by_name[manifest.name] = program
            self._namespace_owner[namespace] = manifest.name
            for cmd_name in new_command_names:
                self._command_owner[cmd_name] = manifest.name
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
            for cmd in program.manifest.commands:
                if self._command_owner.get(cmd.name) == name:
                    self._command_owner.pop(cmd.name)
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

    def get_by_command(self, command_name: str) -> RegisteredProgram | None:
        """Return the program whose manifest declares `command_name`, or None.

        Backed by `_command_owner` and therefore O(1); register() enforces
        global uniqueness so this never has to disambiguate across plugins.
        """
        with self._lock:
            owner = self._command_owner.get(command_name)
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

    Resolution order:
      1. UserLevel namespace (e.g. "github-pr") — fast, in-memory.
      2. UserLevel plugin name (e.g. "github-pr-ops").
      3. UserLevel command name (e.g. "review") — first plugin whose
         command list contains it. Ambiguity is prevented at registration
         time by namespace + name uniqueness.
      4. Bundled skill name (e.g. "review-pr").

    Skills branch precondition:
        The bundled `SkillRegistry` is discovered lazily via
        `await get_registry().discover_all()`. The caller (CLI bootstrap,
        firewall, integration tests) is responsible for driving discovery
        before relying on the skill branch — this helper deliberately does
        not trigger discovery itself, because doing so would either block
        on async-from-sync (fragile under an active event loop) or hide
        an I/O cost in a pure lookup. Callers that have not discovered
        skills will still get correct UserLevel results; they will simply
        miss skill matches and receive `kind="none"` instead.

    Args:
        name: a command name, namespace, or plugin name to look up.

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

    # Resolve via command name (e.g. "review" → plugin owning the command).
    # `register()` enforces global command-name uniqueness so this lookup is
    # unambiguous and O(1) via the dedicated index.
    program = ul.get_by_command(name)
    if program is not None:
        return LookupResult(kind="userlevel", userlevel_program=program)

    # Skills lookup: import lazily so this module doesn't pull in watchdog
    # and yaml at import time. Returns kind="none" if discovery has not run
    # (see precondition note above) — callers that need authoritative skill
    # results must drive `discover_all()` themselves.
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
