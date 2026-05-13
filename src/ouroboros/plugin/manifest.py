"""Plugin manifest loader.

Loads UserLevel plugin manifests (`ouroboros.plugin.json`) and validates them
against the vendored JSON Schema under `src/ouroboros/plugin/schemas/<major>/`.

The locked spec (Q00/ouroboros#728) requires:

  - `PluginManifest` is a frozen dataclass with 8 required + optional fields
    selected by the manifest schema version.
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
from importlib import resources
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
# v0.1 is the archived base manifest contract; v0.2 is the local extension
# that adds optional hook declarations.
SUPPORTED_SCHEMA_VERSIONS: tuple[str, ...] = ("0.1", "0.2")

# Source types whose `path` must be a sandboxed relative slug — no absolute
# paths and no parent-directory traversal. `local_path` resolves relative to
# the manifest's directory; `plugin_home` resolves relative to the user's
# plugin home. Either one becoming an absolute or escaping path is a real
# trust-boundary leak, not a cosmetic issue.
_PATH_SANDBOXED_SOURCE_TYPES: frozenset[str] = frozenset({"local_path", "plugin_home"})


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
class HookSpec:
    name: str
    entrypoint: Entrypoint
    failure_policy: str
    timeout_seconds: int | None = None
    permissions: tuple[str, ...] = ()
    description: str = ""


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

    Field shape matches the selected plugin manifest schema. v0.1 manifests
    do not accept hooks; v0.2 manifests may declare optional hooks.
    """

    schema_version: str
    name: str
    version: str
    source: SourceSpec
    commands: tuple[CommandSpec, ...]
    # Capabilities and permissions are stored as ordered tuples so iteration
    # order matches the manifest's declaration. ``frozenset`` would
    # reintroduce nondeterministic order in CLI output and emitted
    # ``plugin.permission_used`` events for multi-entry manifests.
    capabilities: tuple[Capability, ...]
    permissions: tuple[Permission, ...]
    entrypoint: Entrypoint
    description: str = ""
    audit: AuditSpec = field(default_factory=AuditSpec.standard_four_events)
    hooks: tuple[HookSpec, ...] = ()


def _load_schema(schema_version: str, *, manifest_path: str | Path) -> dict[str, Any]:
    """Load the vendored schema for `schema_version`.

    Resolved via `importlib.resources` so the lookup works whether the
    package is installed as a wheel, an editable install, or a zipapp —
    the same pattern `ouroboros.opencode.plugin` uses for its bridge
    assets. Reuses `manifest_path` as the `path` field on any raised
    error so the caller's structured-error contract still points back at
    the manifest that triggered the lookup, not at an internal vendored
    file the user cannot fix.
    """
    try:
        schema_resource = (
            resources.files("ouroboros.plugin.schemas")
            .joinpath(schema_version)
            .joinpath("plugin.schema.json")
        )
        if not schema_resource.is_file():
            raise PluginManifestError(
                f"vendored schema for version {schema_version!r} not found",
                path=str(manifest_path),
                json_pointer="/schema_version",
                expected=f"one of {list(SUPPORTED_SCHEMA_VERSIONS)}",
                got=f"{schema_version!r} (no vendored schema in installed package)",
            )
        raw = schema_resource.read_text(encoding="utf-8")
    except (ModuleNotFoundError, ImportError) as exc:
        # `importlib.resources.files("ouroboros.plugin.schemas")` raises
        # ModuleNotFoundError if the namespace package is missing from the
        # installed wheel (force-include misconfigured) or if the parent
        # package fails to import. Surface it through the same structured
        # error as every other loader failure.
        raise PluginManifestError(
            f"vendored schema package is not importable: {exc}",
            path=str(manifest_path),
            json_pointer="/schema_version",
            expected="ouroboros.plugin.schemas package on the import path",
            got=f"{type(exc).__name__}: {exc}",
        ) from exc
    except FileNotFoundError as exc:
        # Raised by importlib.resources when the package itself is missing
        # the asset directory entirely (e.g. wheel built without
        # force-include). This is exactly the failure mode the bot's
        # follow-up flagged.
        raise PluginManifestError(
            f"vendored schema directory missing from installed package: {exc}",
            path=str(manifest_path),
            json_pointer="/schema_version",
            expected="schema directory packaged with ouroboros.plugin",
            got=f"FileNotFoundError on ouroboros.plugin.schemas/{schema_version}",
        ) from exc
    except OSError as exc:
        raise PluginManifestError(
            f"vendored schema is unreadable: {exc.strerror or exc}",
            path=str(manifest_path),
            json_pointer="/schema_version",
            expected="readable vendored schema file",
            got=f"{type(exc).__name__}: {exc.strerror or exc}",
        ) from exc
    except UnicodeDecodeError as exc:
        raise PluginManifestError(
            "vendored schema is not valid UTF-8",
            path=str(manifest_path),
            json_pointer="/schema_version",
            expected="UTF-8 encoded JSON file",
            got=f"UnicodeDecodeError: {exc.reason}",
        ) from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PluginManifestError(
            f"vendored schema is not valid JSON: {exc.msg}",
            path=str(manifest_path),
            json_pointer="/schema_version",
            expected="valid JSON object",
            got=f"JSON decode error at line {exc.lineno}, col {exc.colno}",
        ) from exc


def _validate_sandboxed_path(raw_path: str, *, source_type: str, manifest_path: str | Path) -> None:
    """Reject absolute paths and parent-directory traversal in `path`.

    The locked spec says `local_path` resolves relative to the manifest's
    directory and `plugin_home` resolves relative to the user's plugin
    home. Either one accepting `/etc/passwd`, `C:/Windows/System32`,
    `..\\escape`, or any other absolute / traversal form would let a
    plugin escape its sandbox the moment a downstream consumer joined the
    path naively. Validation is platform-agnostic — even when the loader
    runs on Linux it must reject Windows escape forms, because the
    consumer of the manifest may run on Windows.

    Catch it at load time, with the same JSON-pointer contract the rest
    of the loader uses.
    """
    pointer = "/source/path"

    # Backslash is never legal in a manifest source.path: these are POSIX
    # slugs, and accepting `..\\foo` on a POSIX host would let a Windows
    # consumer's `ntpath.join` treat it as parent traversal.
    if "\\" in raw_path:
        raise PluginManifestError(
            f"source.path for {source_type!r} must use forward slashes only",
            path=str(manifest_path),
            json_pointer=pointer,
            expected="POSIX-style relative path with no '\\\\' separators",
            got=raw_path,
        )

    # Windows drive prefix: `C:/foo`, `c:foo`, etc.
    if len(raw_path) >= 2 and raw_path[1] == ":" and raw_path[0].isalpha():
        raise PluginManifestError(
            f"source.path for {source_type!r} must not be drive-qualified",
            path=str(manifest_path),
            json_pointer=pointer,
            expected="relative path with no Windows drive prefix",
            got=raw_path,
        )

    # POSIX absolute or UNC-style leading separator.
    if raw_path.startswith("/"):
        raise PluginManifestError(
            f"source.path for {source_type!r} must be relative, not absolute",
            path=str(manifest_path),
            json_pointer=pointer,
            expected="relative path under the source root",
            got=raw_path,
        )

    # Reject any `..` segment, including ones embedded mid-path like
    # `a/../b`. Splitting on '/' is sufficient because backslashes were
    # already rejected above.
    if any(part == ".." for part in raw_path.split("/")):
        raise PluginManifestError(
            f"source.path for {source_type!r} must not contain '..' segments",
            path=str(manifest_path),
            json_pointer=pointer,
            expected="path with no parent-directory traversal",
            got=raw_path,
        )


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


def _build_hook(
    raw: dict[str, Any],
    *,
    declared_permission_scopes: frozenset[str],
    hook_index: int,
    manifest_path: str | Path,
) -> HookSpec:
    for permission_index, scope in enumerate(raw.get("permissions", ())):
        if scope not in declared_permission_scopes:
            raise PluginManifestError(
                "hook permission must be declared in top-level permissions",
                path=str(manifest_path),
                json_pointer=f"/hooks/{hook_index}/permissions/{permission_index}",
                expected=(
                    "permission scope declared in top-level permissions[].scope "
                    f"({sorted(declared_permission_scopes)!r})"
                ),
                got=scope,
            )

    entrypoint_raw = raw["entrypoint"]
    return HookSpec(
        name=raw["name"],
        entrypoint=Entrypoint(
            type=entrypoint_raw["type"],
            command=entrypoint_raw["command"],
        ),
        failure_policy=raw["failure_policy"],
        timeout_seconds=raw.get("timeout_seconds"),
        permissions=tuple(raw.get("permissions", ())),
        description=raw.get("description", ""),
    )


def _escape_json_pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _validation_error_pointer(error: Any) -> str:
    """Return the most useful manifest pointer for a jsonschema error."""
    if error.validator == "additionalProperties" and isinstance(error.instance, dict):
        allowed = set(error.schema.get("properties", ()))
        unexpected = [key for key in error.instance if key not in allowed]
        if unexpected:
            path = [*error.absolute_path, unexpected[0]]
            return "/" + "/".join(_escape_json_pointer_token(str(p)) for p in path)

    return (
        "/" + "/".join(_escape_json_pointer_token(str(p)) for p in error.absolute_path)
        if error.absolute_path
        else ""
    )


def load_manifest(path: str | Path) -> PluginManifest:
    """Load and validate a plugin manifest from `path`.

    Args:
        path: Filesystem path to an `ouroboros.plugin.json` file.

    Returns:
        A frozen, validated `PluginManifest`.

    Raises:
        PluginManifestError: on missing file, unreadable file (permission
            denied, non-UTF-8 bytes), JSON decode failure, schema
            violation, or unsupported `schema_version`. All structured
            failures surface through this single exception type so callers
            never need to catch raw `OSError`/`UnicodeDecodeError`.
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
    except UnicodeDecodeError as exc:
        raise PluginManifestError(
            "manifest is not valid UTF-8",
            path=str(manifest_path),
            json_pointer=None,
            expected="UTF-8 encoded JSON file",
            got=f"{exc.reason} at byte {exc.start}",
        ) from exc
    except OSError as exc:
        # Reaches here on permission denied, transient filesystem errors,
        # or a TOCTOU between the is_file() check and the open() call.
        raise PluginManifestError(
            f"manifest is unreadable: {exc.strerror or exc}",
            path=str(manifest_path),
            json_pointer=None,
            expected="readable file",
            got=f"{type(exc).__name__}: {exc.strerror or exc}",
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

    schema = _load_schema(schema_version, manifest_path=manifest_path)
    validator = Draft202012Validator(schema)
    errors = sorted(
        validator.iter_errors(raw),
        key=lambda e: list(e.absolute_path),
    )
    if errors:
        err = errors[0]
        pointer = _validation_error_pointer(err)
        raise PluginManifestError(
            err.message,
            path=str(manifest_path),
            json_pointer=pointer,
            expected=str(err.schema),
            got=str(err.instance)[:200],
        )

    source_raw = raw["source"]
    source_type = source_raw["type"]
    source_path = source_raw.get("path")
    if source_type in _PATH_SANDBOXED_SOURCE_TYPES and isinstance(source_path, str):
        _validate_sandboxed_path(source_path, source_type=source_type, manifest_path=manifest_path)
    source = SourceSpec(
        type=source_type,
        path=source_path,
        repository=source_raw.get("repository"),
    )

    commands = tuple(_build_command(c) for c in raw["commands"])
    capabilities = tuple(
        Capability(name=c["name"], access=c["access"], reason=c.get("reason", ""))
        for c in raw["capabilities"]
    )
    permissions = tuple(
        Permission(
            scope=p["scope"],
            risk=p["risk"],
            required=p["required"],
            reason=p.get("reason", ""),
        )
        for p in raw["permissions"]
    )
    entrypoint = Entrypoint(
        type=raw["entrypoint"]["type"],
        command=raw["entrypoint"]["command"],
    )

    audit_raw = raw.get("audit")
    if audit_raw is None:
        audit = AuditSpec.standard_four_events()
    else:
        audit = AuditSpec(events=tuple(audit_raw["events"]))

    declared_permission_scopes = frozenset(p.scope for p in permissions)
    hooks = tuple(
        _build_hook(
            h,
            declared_permission_scopes=declared_permission_scopes,
            hook_index=index,
            manifest_path=manifest_path,
        )
        for index, h in enumerate(raw.get("hooks", ()))
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
        hooks=hooks,
    )


__all__ = [
    "Capability",
    "CommandArgument",
    "CommandSpec",
    "Entrypoint",
    "HookSpec",
    "Permission",
    "PluginManifest",
    "PluginManifestError",
    "SourceSpec",
    "AuditSpec",
    "load_manifest",
    "SUPPORTED_SCHEMA_VERSIONS",
]
