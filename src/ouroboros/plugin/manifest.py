"""Plugin manifest loader.

Loads UserLevel plugin manifests (`ouroboros.plugin.json`) and validates them
against the vendored JSON Schema under `src/ouroboros/plugin/schemas/<major>/`.

The locked spec (Q00/ouroboros#728) requires:

  - `PluginManifest` is a frozen dataclass with 8 required + 2 optional fields
    (per Q00/ouroboros-plugins#6 lock).
  - `load_manifest(path)` returns a frozen, validated manifest.
  - On any schema violation, raise `PluginManifestError` with structured
    fields: `path`, `json_pointer`, `expected`, `got`. A reviewer can match
    on `json_pointer` rather than parsing message text.
  - `source.type=first_party` is a real branch; the loader does not require
    `source.path`/`source.repository` for first-party manifests.
  - Manifest's `schema_version` selects the matching archived schema.
    Unsupported versions raise with a clear message naming the support
    window.

This module is intentionally narrow. It does not load remote URLs (that is
the manager's job in #731), does not cache (premature), and does not perform
runtime side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import shlex
from typing import Any

try:
    from jsonschema import Draft202012Validator
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "jsonschema>=4.21 is required. Install it via `pip install jsonschema`."
    ) from exc


# Support window per Q00/ouroboros-plugins#11 lock: current MAJOR + previous MAJOR.
# Today we only ship 0.1; once 1.0 ships, both "0.1" and "1.0" are accepted, "0.x"
# below the latest is unsupported.
SUPPORTED_SCHEMA_VERSIONS: tuple[str, ...] = ("0.1",)

_SCHEMAS_ROOT = Path(__file__).resolve().parent / "schemas"


class PluginManifestError(Exception):
    """Raised when a manifest fails to load or validate.

    Attributes:
        path: Filesystem path of the manifest that failed.
        json_pointer: JSON Pointer (RFC 6901) to the failing field, or None
            for whole-file failures.
        expected: Human-readable description of what was expected.
        got: Human-readable description of what was actually present.
    """

    def __init__(
        self,
        message: str,
        *,
        path: str | Path,
        json_pointer: str | None = None,
        expected: str = "",
        got: str = "",
    ) -> None:
        super().__init__(message)
        self.path = str(path)
        self.json_pointer = json_pointer
        self.expected = expected
        self.got = got

    def __str__(self) -> str:  # pragma: no cover - convenience
        loc = self.json_pointer if self.json_pointer is not None else "(root)"
        return f"{self.path}: {loc}: {self.args[0] if self.args else ''}"


@dataclass(frozen=True)
class CommandArgument:
    name: str
    type: str
    required: bool
    description: str = ""


@dataclass(frozen=True)
class CommandSpec:
    namespace: str
    name: str
    summary: str
    usage: str
    risk: str
    requires_confirmation: bool = False
    arguments: tuple[CommandArgument, ...] = ()


@dataclass(frozen=True)
class Capability:
    name: str
    access: str
    reason: str = ""


@dataclass(frozen=True)
class Permission:
    scope: str
    risk: str
    required: bool
    reason: str = ""


@dataclass(frozen=True)
class SourceSpec:
    type: str
    path: str | None = None
    repository: str | None = None


@dataclass(frozen=True)
class Entrypoint:
    type: str
    command: str


@dataclass(frozen=True)
class AuditSpec:
    events: tuple[str, ...]

    @staticmethod
    def standard_four_events() -> AuditSpec:
        return AuditSpec(
            events=(
                "plugin.invoked",
                "plugin.permission_used",
                "plugin.completed",
                "plugin.failed",
            )
        )


# Events the invocation firewall may emit during a single call. A manifest
# whose `audit.events` omits any of these is asserting a contract the
# runtime cannot honor — `invoke_plugin` would still emit them. The
# loader enforces that all four are present so the manifest's declared
# contract matches the firewall's actual behavior.
FIREWALL_REQUIRED_EVENTS: frozenset[str] = frozenset(
    {
        "plugin.invoked",
        "plugin.permission_used",
        "plugin.completed",
        "plugin.failed",
    }
)


@dataclass(frozen=True)
class PluginManifest:
    """Frozen representation of a validated plugin manifest.

    Field shape matches Q00/ouroboros-plugins/schemas/0.1/plugin.schema.json
    after the locked Q00/ouroboros-plugins#6 (8 required + 2 optional)
    decision is applied.
    """

    schema_version: str
    name: str
    version: str
    source: SourceSpec
    commands: tuple[CommandSpec, ...]
    # Capabilities and permissions preserve manifest declaration order.
    # The firewall's `permission_used` emission and blocked-scope error
    # message both walk these in order, so the persisted audit trail and
    # user-facing UX are deterministic. Using `tuple` (instead of
    # `frozenset`) makes that explicit at the type level.
    capabilities: tuple[Capability, ...]
    permissions: tuple[Permission, ...]
    entrypoint: Entrypoint
    description: str = ""
    audit: AuditSpec = field(default_factory=AuditSpec.standard_four_events)


def _load_schema(schema_version: str) -> dict[str, Any]:
    schema_path = _SCHEMAS_ROOT / schema_version / "plugin.schema.json"
    if not schema_path.is_file():
        raise PluginManifestError(
            f"vendored schema for version {schema_version!r} not found",
            path=str(schema_path),
            json_pointer="/schema_version",
            expected=f"one of {list(SUPPORTED_SCHEMA_VERSIONS)}",
            got=f"{schema_version!r} (no vendored schema)",
        )
    with schema_path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _build_command(raw: dict[str, Any]) -> CommandSpec:
    args = tuple(
        CommandArgument(
            name=a["name"],
            type=a["type"],
            required=a["required"],
            description=a.get("description", ""),
        )
        for a in raw.get("arguments", [])
    )
    return CommandSpec(
        namespace=raw["namespace"],
        name=raw["name"],
        summary=raw["summary"],
        usage=raw["usage"],
        risk=raw["risk"],
        requires_confirmation=raw.get("requires_confirmation", False),
        arguments=args,
    )


def load_manifest(path: str | Path) -> PluginManifest:
    """Load and validate a plugin manifest from `path`.

    Args:
        path: Filesystem path to an `ouroboros.plugin.json` file.

    Returns:
        A frozen, validated `PluginManifest`.

    Raises:
        PluginManifestError: on JSON decode failure, schema violation, or
            unsupported `schema_version`.
    """
    manifest_path = Path(path)
    if not manifest_path.is_file():
        raise PluginManifestError(
            f"manifest file not found: {manifest_path}",
            path=str(manifest_path),
            json_pointer=None,
            expected="readable file",
            got="missing",
        )

    try:
        with manifest_path.open(encoding="utf-8") as handle:
            raw = json.load(handle)
    except json.JSONDecodeError as exc:
        raise PluginManifestError(
            f"manifest is not valid JSON: {exc.msg}",
            path=str(manifest_path),
            json_pointer=None,
            expected="valid JSON object",
            got=f"JSON decode error at line {exc.lineno}, col {exc.colno}",
        ) from exc

    if not isinstance(raw, dict):
        raise PluginManifestError(
            "manifest must be a JSON object",
            path=str(manifest_path),
            json_pointer="",
            expected="object",
            got=type(raw).__name__,
        )

    schema_version = raw.get("schema_version")
    if not isinstance(schema_version, str):
        raise PluginManifestError(
            "manifest is missing `schema_version`",
            path=str(manifest_path),
            json_pointer="/schema_version",
            expected="string (e.g. '0.1')",
            got=type(schema_version).__name__,
        )

    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise PluginManifestError(
            f"schema_version {schema_version!r} is not in the support window",
            path=str(manifest_path),
            json_pointer="/schema_version",
            expected=f"schema_version in supported window {list(SUPPORTED_SCHEMA_VERSIONS)}",
            got=schema_version,
        )

    schema = _load_schema(schema_version)
    validator = Draft202012Validator(schema)
    errors = sorted(
        validator.iter_errors(raw),
        key=lambda e: list(e.absolute_path),
    )
    if errors:
        err = errors[0]
        pointer = "/" + "/".join(str(p) for p in err.absolute_path) if err.absolute_path else ""
        raise PluginManifestError(
            err.message,
            path=str(manifest_path),
            json_pointer=pointer,
            expected=str(err.schema),
            got=str(err.instance)[:200],
        )

    source_raw = raw["source"]
    source = SourceSpec(
        type=source_raw["type"],
        path=source_raw.get("path"),
        repository=source_raw.get("repository"),
    )

    commands = tuple(_build_command(c) for c in raw["commands"])
    # Reject duplicate command names within a plugin. The JSON Schema
    # enforces shape, but does not enforce uniqueness across the
    # `commands` array. `RegisteredProgram.find_command()` returns the
    # first match by name, so a duplicate would silently shadow a
    # different command's risk level, confirmation policy, or arguments.
    seen_command_names: set[str] = set()
    for cmd in commands:
        if cmd.name in seen_command_names:
            idx = next(i for i, c in enumerate(commands) if c.name == cmd.name and c is cmd)
            raise PluginManifestError(
                f"duplicate command name {cmd.name!r}",
                path=str(manifest_path),
                json_pointer=f"/commands/{idx}/name",
                expected="unique command name within the plugin",
                got=f"second occurrence of {cmd.name!r}",
            )
        seen_command_names.add(cmd.name)
    capabilities = tuple(
        Capability(name=c["name"], access=c["access"], reason=c.get("reason", ""))
        for c in raw["capabilities"]
    )
    # Reject duplicate capability names. Downstream code keys capability
    # decisions on `name`, so a duplicate would silently shadow another
    # entry's `access` / `reason`.
    seen_capability_names: set[str] = set()
    for cap in capabilities:
        if cap.name in seen_capability_names:
            idx = next(i for i, c in enumerate(capabilities) if c.name == cap.name and c is cap)
            raise PluginManifestError(
                f"duplicate capability name {cap.name!r}",
                path=str(manifest_path),
                json_pointer=f"/capabilities/{idx}/name",
                expected="unique capability name within the plugin",
                got=f"second occurrence of {cap.name!r}",
            )
        seen_capability_names.add(cap.name)

    permissions = tuple(
        Permission(
            scope=p["scope"],
            risk=p["risk"],
            required=p["required"],
            reason=p.get("reason", ""),
        )
        for p in raw["permissions"]
    )
    # Reject duplicate permission scopes. The firewall treats `scope` as
    # the unique permission identifier: `_required_permissions()` would
    # emit one `plugin.permission_used` event per duplicate, and
    # `_scope_risk_index()` would silently let the last entry win for
    # the blocked-message risk label. Two `github:read` entries with
    # different `risk` or `required` values therefore produce
    # inconsistent UX and audit data instead of being rejected.
    seen_scopes: set[str] = set()
    for perm in permissions:
        if perm.scope in seen_scopes:
            idx = next(i for i, p in enumerate(permissions) if p.scope == perm.scope and p is perm)
            raise PluginManifestError(
                f"duplicate permission scope {perm.scope!r}",
                path=str(manifest_path),
                json_pointer=f"/permissions/{idx}/scope",
                expected="unique permission scope within the plugin",
                got=f"second occurrence of {perm.scope!r}",
            )
        seen_scopes.add(perm.scope)
    entrypoint_command = raw["entrypoint"]["command"]
    # The JSON Schema only enforces `minLength: 1`, so a manifest with
    # `"   "` (or any whitespace string) would pass shape validation but
    # `shlex.split()` returns `[]` at invocation time. The firewall
    # would then call `subprocess.run([])` which raises `IndexError`
    # AFTER `plugin.invoked` has been emitted, leaving an incomplete
    # audit trail. Reject blank or shlex-empty commands at load.
    try:
        _entrypoint_argv = shlex.split(entrypoint_command)
    except ValueError as exc:
        raise PluginManifestError(
            f"entrypoint.command is not a valid shell-like token sequence: {exc}",
            path=str(manifest_path),
            json_pointer="/entrypoint/command",
            expected="non-empty shell-tokenizable command",
            got=repr(entrypoint_command)[:200],
        ) from exc
    if not _entrypoint_argv:
        raise PluginManifestError(
            "entrypoint.command must be a non-empty command",
            path=str(manifest_path),
            json_pointer="/entrypoint/command",
            expected="non-empty command (at least one shlex token)",
            got=repr(entrypoint_command)[:200],
        )
    entrypoint = Entrypoint(
        type=raw["entrypoint"]["type"],
        command=entrypoint_command,
    )

    audit_raw = raw.get("audit")
    if audit_raw is None:
        audit = AuditSpec.standard_four_events()
    else:
        audit = AuditSpec(events=tuple(audit_raw["events"]))
        # Enforce that the manifest's declared `audit.events` include every
        # event the firewall may emit. Otherwise a manifest could opt out
        # of (e.g.) `plugin.permission_used` while the runtime still emits
        # it, breaking any consumer that treats the manifest as
        # authoritative for the event set.
        declared = frozenset(audit.events)
        missing = sorted(FIREWALL_REQUIRED_EVENTS - declared)
        if missing:
            raise PluginManifestError(
                f"audit.events must include the firewall-emitted events; missing: {missing}",
                path=str(manifest_path),
                json_pointer="/audit/events",
                expected=(
                    f"array containing all firewall events {sorted(FIREWALL_REQUIRED_EVENTS)}"
                ),
                got=f"declared: {sorted(declared)}",
            )

    return PluginManifest(
        schema_version=schema_version,
        name=raw["name"],
        version=raw["version"],
        source=source,
        commands=commands,
        capabilities=capabilities,
        permissions=permissions,
        entrypoint=entrypoint,
        description=raw.get("description", ""),
        audit=audit,
    )


__all__ = [
    "AuditSpec",
    "Capability",
    "CommandArgument",
    "CommandSpec",
    "Entrypoint",
    "FIREWALL_REQUIRED_EVENTS",
    "Permission",
    "PluginManifest",
    "PluginManifestError",
    "SUPPORTED_SCHEMA_VERSIONS",
    "SourceSpec",
    "load_manifest",
]
