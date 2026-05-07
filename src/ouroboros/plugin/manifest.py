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
    # Ordered tuples (not frozensets) so the firewall's audit-event/message
    # iteration order matches manifest declaration order. Uniqueness is
    # enforced at load time via `_unique_in_order`.
    capabilities: tuple[Capability, ...]
    permissions: tuple[Permission, ...]
    entrypoint: Entrypoint
    description: str = ""
    audit: AuditSpec = field(default_factory=AuditSpec.standard_four_events)
    # Filesystem path the manifest was loaded from. Used downstream to
    # resolve relative `source.path` values against the manifest
    # directory (not the calling process cwd). May be `None` for
    # synthetic manifests built in tests; production code paths set it
    # via `load_manifest`.
    manifest_path: str | None = None


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

    # Reject duplicate command names within a manifest. `RegisteredProgram
    # .find_command()` returns the first match by name, so a second command
    # with the same name would be silently unreachable and its
    # `risk` / `requires_confirmation` settings would be ignored — an
    # API-contract ambiguity at the manifest boundary that should fail
    # loud at load time, mirroring the per-permission/per-capability
    # uniqueness already enforced below.
    command_names_seen: set[str] = set()
    for index, c in enumerate(raw["commands"]):
        if c["name"] in command_names_seen:
            raise PluginManifestError(
                f"duplicate command name {c['name']!r}",
                path=str(manifest_path),
                json_pointer=f"/commands/{index}/name",
                expected="unique command name across the array",
                got=c["name"],
            )
        command_names_seen.add(c["name"])
    commands = tuple(_build_command(c) for c in raw["commands"])

    # Capabilities and permissions preserve manifest declaration order so the
    # firewall's audit-event emission and "first missing scope" message are
    # deterministic. Duplicate keys are rejected with a structured pointer
    # rather than silently deduped, so manifest authors get a clear error.
    capabilities_seen: set[str] = set()
    capabilities_list: list[Capability] = []
    for index, c in enumerate(raw["capabilities"]):
        if c["name"] in capabilities_seen:
            raise PluginManifestError(
                f"duplicate capability name {c['name']!r}",
                path=str(manifest_path),
                json_pointer=f"/capabilities/{index}/name",
                expected="unique capability name across the array",
                got=c["name"],
            )
        capabilities_seen.add(c["name"])
        capabilities_list.append(
            Capability(name=c["name"], access=c["access"], reason=c.get("reason", ""))
        )
    capabilities = tuple(capabilities_list)

    permissions_seen: set[str] = set()
    permissions_list: list[Permission] = []
    for index, p in enumerate(raw["permissions"]):
        if p["scope"] in permissions_seen:
            raise PluginManifestError(
                f"duplicate permission scope {p['scope']!r}",
                path=str(manifest_path),
                json_pointer=f"/permissions/{index}/scope",
                expected="unique permission scope across the array",
                got=p["scope"],
            )
        permissions_seen.add(p["scope"])
        permissions_list.append(
            Permission(
                scope=p["scope"],
                risk=p["risk"],
                required=p["required"],
                reason=p.get("reason", ""),
            )
        )
    permissions = tuple(permissions_list)
    # The schema only enforces `minLength: 1`, which lets a whitespace-only
    # command like "   " through. At invocation time `shlex.split` of such
    # a command yields zero tokens; the firewall would then prepend the
    # subcommand name and try to exec the literal subcommand as the
    # binary — a misleading runtime failure that escapes after
    # `plugin.invoked` has already been emitted. Reject here at the
    # manifest boundary.
    import shlex

    entrypoint_command_raw = raw["entrypoint"]["command"]
    if not entrypoint_command_raw.strip() or not shlex.split(entrypoint_command_raw):
        raise PluginManifestError(
            "entrypoint.command must contain at least one non-whitespace token",
            path=str(manifest_path),
            json_pointer="/entrypoint/command",
            expected="shell command with >= 1 executable token",
            got=repr(entrypoint_command_raw),
        )
    entrypoint = Entrypoint(
        type=raw["entrypoint"]["type"],
        command=entrypoint_command_raw,
    )

    audit_raw = raw.get("audit")
    if audit_raw is None:
        audit = AuditSpec.standard_four_events()
    else:
        audit = AuditSpec(events=tuple(audit_raw["events"]))

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
        manifest_path=str(manifest_path.resolve()),
    )


__all__ = [
    "Capability",
    "CommandArgument",
    "CommandSpec",
    "Entrypoint",
    "Permission",
    "PluginManifest",
    "PluginManifestError",
    "SourceSpec",
    "AuditSpec",
    "load_manifest",
    "SUPPORTED_SCHEMA_VERSIONS",
]
