"""`ooo plugin` command group.

UserLevel plugin manager CLI. Implements Q00/ouroboros#731 (locked spec).

This file contains the **read-only** subcommands (`discover`, `inspect`,
`list`). State-mutating subcommands (`add`, `install`, `trust`, `disable`,
`remove`) are added in a follow-up PR; the module structure is laid out
here so the follow-up plugs in cleanly.
"""

from __future__ import annotations

import json
from pathlib import Path
import tomllib
from typing import Annotated

import typer

from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import (
    print_error,
    print_info,
    print_success,
)
from ouroboros.cli.formatters.tables import create_table, print_table
from ouroboros.plugin.lockfile import DEFAULT_LOCKFILE_PATH, Lockfile
from ouroboros.plugin.manifest import (
    PluginManifest,
    PluginManifestError,
    load_manifest,
)
from ouroboros.plugin.trust_store import (
    DEFAULT_TRUST_ROOT,
    TrustRecord,
    TrustStore,
)

app = typer.Typer(
    name="plugin",
    help="Manage UserLevel plugins (#725).",
    no_args_is_help=True,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_manifest_path(target: str) -> Path:
    """Accept either a directory containing ouroboros.plugin.json or the
    file itself; return the file path."""
    p = Path(target).expanduser()
    if p.is_dir():
        return p / "ouroboros.plugin.json"
    return p


def _load_with_friendly_error(target: str) -> PluginManifest:
    """Load a manifest, printing a nicely-formatted error on failure."""
    path = _resolve_manifest_path(target)
    try:
        return load_manifest(path)
    except PluginManifestError as exc:
        loc = exc.json_pointer if exc.json_pointer else "(root)"
        print_error(
            f"manifest invalid:\n  path: {exc.path}\n  at: {loc}\n  expected: {exc.expected}\n  got: {exc.got}"
        )
        raise typer.Exit(code=1) from exc
    except OSError as exc:
        # Mirror the inspect/list error path: `discover` is also a
        # diagnostic command, so an unreadable manifest (broken
        # symlink, wrong permissions, transient I/O) must surface as a
        # clean message instead of bubbling a raw OSError.
        print_error(
            f"manifest is unreadable: {path}: {exc}\n"
            f"  Check the path and file permissions; "
            f"`ooo plugin discover` cannot proceed without it."
        )
        raise typer.Exit(code=1) from exc


_STATE_FILE_ERRORS: tuple[type[Exception], ...] = (
    ValueError,  # schema_version mismatch raised by Lockfile.read / TrustStore.read
    tomllib.TOMLDecodeError,
    json.JSONDecodeError,
    # Lockfile.read / TrustStore.read build dataclasses via unchecked
    # `raw["field"]` lookups. A parseable-but-structurally-corrupt state
    # file (missing `name`, wrong field type) therefore raises KeyError
    # / TypeError. These commands are diagnostic — every shape of
    # damaged state file must surface a friendly error, not a raw
    # traceback.
    KeyError,
    TypeError,
    OSError,  # permission/IO errors on the state files themselves
)


def _read_lockfile_or_exit(lock: Lockfile) -> dict:
    """Read the lockfile, surfacing a friendly error if it's malformed.

    `inspect` and `list` are diagnostic commands — if the very state
    file they introspect is broken, they should print a useful error
    and exit cleanly rather than dump a raw traceback to the operator.
    """
    try:
        return lock.read()
    except _STATE_FILE_ERRORS as exc:
        print_error(
            f"plugins lockfile is unreadable: {lock.path}: {exc}\n"
            f"  Inspect the file by hand or restore from backup; "
            f"`ooo plugin` cannot proceed until it parses cleanly."
        )
        raise typer.Exit(code=1) from exc


def _read_trust_or_exit(trust: TrustStore, plugin: str) -> TrustRecord | None:
    """Read a trust record, surfacing a friendly error if it's malformed."""
    try:
        return trust.read(plugin)
    except _STATE_FILE_ERRORS as exc:
        print_error(
            f"trust file for {plugin!r} is unreadable: {exc}\n"
            f"  Inspect or remove ~/.ouroboros/plugins/{plugin}/trust.json; "
            f"`ooo plugin` cannot proceed until it parses cleanly."
        )
        raise typer.Exit(code=1) from exc


def _required_scopes(manifest: PluginManifest) -> list[str]:
    return [p.scope for p in manifest.permissions if p.required]


def _missing_required(
    manifest: PluginManifest,
    record: TrustRecord | None,
) -> list[str]:
    """Required scopes that are not currently satisfied.

    Mirrors `ouroboros.plugin.firewall._missing_required` so the CLI's
    trust-state report cannot drift away from the firewall's actual
    invocation gate. A version-bumped trust file is treated as if no
    scopes were granted (locked Q4: version bump invalidates trust).
    """
    required = _required_scopes(manifest)
    if not required:
        return []
    if record is None:
        return list(required)
    if record.version != manifest.version:
        return list(required)
    return record.missing(required)


def _trust_state_label(
    manifest: PluginManifest,
    record: TrustRecord | None,
) -> str:
    """Compute the trust state shown in `inspect`/`list`.

    "trusted" must mean "the firewall will let this run without further
    user action" — i.e. the trust file matches the manifest version AND
    every required scope is granted. Anything short of that is reported
    as "installed" so the CLI cannot lie to operators about whether
    invocation will be blocked.
    """
    if manifest.source.type == "first_party":
        return "first_party"
    if record is None:
        return "installed"
    if record.version != manifest.version:
        return "installed"
    if _missing_required(manifest, record):
        return "installed"
    if record.granted_scopes:
        return "trusted"
    return "installed"


# ---------------------------------------------------------------------------
# Read-only subcommands
# ---------------------------------------------------------------------------


@app.command("discover")
def discover_command(
    target: Annotated[
        str,
        typer.Argument(help="Path to a plugin directory or its ouroboros.plugin.json file."),
    ],
) -> None:
    """Inspect a manifest without registering or granting trust.

    `discover` is the safest command in the manager — it neither writes to
    the lockfile nor reads the trust store.
    """
    manifest = _load_with_friendly_error(target)
    print_success(f"manifest valid: {manifest.name} {manifest.version}")
    console.print(f"  schema_version: {manifest.schema_version}")
    console.print(f"  source.type:    {manifest.source.type}")
    console.print(f"  description:    {manifest.description or '(none)'}")
    console.print(
        f"  commands:       {len(manifest.commands)} "
        f"in namespace {manifest.commands[0].namespace!r}"
    )
    console.print(f"  capabilities:   {len(manifest.capabilities)}")
    console.print(f"  permissions:    {len(manifest.permissions)}")
    required_perms = [p for p in manifest.permissions if p.required]
    if required_perms:
        console.print("  required scopes (must be trusted before invocation):")
        for perm in required_perms:
            console.print(f"    - {perm.scope} ({perm.risk})")


@app.command("inspect")
def inspect_command(
    name: Annotated[str, typer.Argument(help="Installed plugin name.")],
    lockfile_path: Annotated[
        Path | None,
        typer.Option(
            "--lockfile", help="Override the lockfile path (default: ~/.ouroboros/plugins.lock)."
        ),
    ] = None,
    trust_root: Annotated[
        Path | None,
        typer.Option(
            "--trust-root", help="Override the trust root (default: ~/.ouroboros/plugins)."
        ),
    ] = None,
) -> None:
    """Show installed plugin metadata + trust state.

    Unlike `discover`, this reads the lockfile and trust store. It still
    does not mutate any state.
    """
    lock = Lockfile(lockfile_path or DEFAULT_LOCKFILE_PATH)
    trust = TrustStore(root=trust_root or DEFAULT_TRUST_ROOT)

    entries = _read_lockfile_or_exit(lock)
    entry = entries.get(name)
    if entry is None:
        print_error(f"{name!r} is not installed (no entry in {lock.path})")
        raise typer.Exit(code=1)

    manifest_path = Path(entry.plugin_home).expanduser() / "ouroboros.plugin.json"
    try:
        manifest = load_manifest(manifest_path)
    except PluginManifestError as exc:
        print_error(
            f"installed manifest is invalid: {exc.path}: "
            f"{exc.json_pointer or '(root)'}: {exc.args[0] if exc.args else ''}"
        )
        raise typer.Exit(code=1) from exc
    except OSError as exc:
        # `load_manifest` opens the file directly; an unreadable manifest
        # (wrong permissions, broken symlink, vanished plugin_home) bubbles
        # raw OSError. Diagnostic CLI must convert that to a clean message.
        print_error(
            f"installed manifest is unreadable: {manifest_path}: {exc}\n"
            f"  Check the plugin_home path and permissions; "
            f"`ooo plugin inspect` cannot proceed without it."
        )
        raise typer.Exit(code=1) from exc

    # First-party plugins ship inside the binary and the firewall
    # ignores their trust file by design (Q00/ouroboros-plugins#8). A
    # leftover/corrupt `trust.json` for such a plugin must not make
    # `inspect` fail — the correct authoritative state for first-party
    # is "first_party with no scopes", read straight from the manifest.
    if manifest.source.type == "first_party":
        record = None
    else:
        record = _read_trust_or_exit(trust, name)
    granted = [g.scope for g in record.granted_scopes] if record else []

    print_info(f"{manifest.name} {manifest.version} ({entry.source_kind})")
    console.print(f"  installed_at:   {entry.installed_at}")
    console.print(f"  plugin_home:    {entry.plugin_home}")
    if entry.repository:
        console.print(f"  repository:     {entry.repository}")
    if entry.git_sha:
        console.print(f"  git_sha:        {entry.git_sha}")
    console.print(f"  trust_state:    {_trust_state_label(manifest, record)}")
    console.print(f"  granted_scopes: {', '.join(granted) if granted else '(none)'}")
    if record is not None and record.version != manifest.version:
        # Loud signal so users understand why "trust_state" flipped back
        # to installed even though the trust file still lists grants.
        console.print(
            f"  trust_version:  recorded {record.version!r} but installed "
            f"{manifest.version!r} — version bump invalidated trust; "
            f"re-grant scopes via `ooo plugin trust`."
        )
    missing = _missing_required(manifest, record)
    if missing:
        console.print(
            f"  missing scopes: {', '.join(missing)} (invocation will be blocked until granted)"
        )


@app.command("list")
def list_command(
    lockfile_path: Annotated[
        Path | None,
        typer.Option(
            "--lockfile", help="Override the lockfile path (default: ~/.ouroboros/plugins.lock)."
        ),
    ] = None,
    trust_root: Annotated[
        Path | None,
        typer.Option(
            "--trust-root", help="Override the trust root (default: ~/.ouroboros/plugins)."
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON for piping; suppresses table formatting."),
    ] = False,
) -> None:
    """List installed plugins with their trust state."""
    lock = Lockfile(lockfile_path or DEFAULT_LOCKFILE_PATH)
    trust = TrustStore(root=trust_root or DEFAULT_TRUST_ROOT)

    entries = _read_lockfile_or_exit(lock)
    if not entries:
        if json_output:
            # Plain stdout (no Rich highlighting) so consumers can pipe to jq.
            typer.echo(json.dumps([]))
        else:
            print_info("no plugins installed")
        return

    rows = []
    for entry in sorted(entries.values(), key=lambda e: e.name):
        # Re-load the manifest so trust_state reflects the same gate the
        # firewall enforces (required-scope set + version match), not
        # just "did the user grant any scope at all". Reading the
        # manifest first also lets us skip the trust.json read for
        # first-party plugins, which the firewall ignores by design —
        # a leftover trust file there must not make `list` fail.
        manifest_path = Path(entry.plugin_home).expanduser() / "ouroboros.plugin.json"
        manifest: PluginManifest | None
        try:
            manifest = load_manifest(manifest_path)
        except (PluginManifestError, OSError):
            manifest = None

        if manifest is not None and manifest.source.type == "first_party":
            record = None
        elif manifest is not None:
            record = _read_trust_or_exit(trust, entry.name)
        else:
            # Manifest unreadable — fall through to the
            # safe-default branch below; reading trust here would still
            # require an exit_or path, but we don't yet know the
            # source.type, so skip and report stale-but-known state.
            record = _read_trust_or_exit(trust, entry.name)

        # Stale-version trust files: the firewall treats them as
        # invalidated, so the JSON view should not echo grants that
        # are no longer effective. Mirror the `inspect` warning by
        # zero-ing out scopes when the record's version no longer
        # matches the manifest, and surface a `trust_version_stale`
        # flag so consumers can branch deterministically.
        stale_version = bool(
            manifest is not None and record is not None and record.version != manifest.version
        )
        if stale_version:
            scopes: list[str] = []
        else:
            scopes = [g.scope for g in record.granted_scopes] if record else []
        try:
            if manifest is None:
                raise PluginManifestError(
                    "manifest unreadable",
                    path=str(manifest_path),
                    json_pointer=None,
                    expected="readable",
                    got="missing",
                )
            trust_state = _trust_state_label(manifest, record)
            missing = _missing_required(manifest, record)
        except (PluginManifestError, OSError):
            # Lockfile entry exists but the on-disk manifest is broken
            # (schema-invalid) or unreadable (vanished/permissions). We
            # cannot prove "trusted" without it, so report "installed"
            # — the safer default — and surface the missing
            # required-scope set as unknown rather than crash the row.
            trust_state = "installed"
            missing = []
        rows.append(
            {
                "name": entry.name,
                "version": entry.version,
                "source_kind": entry.source_kind,
                "trust_state": trust_state,
                "granted_scopes": scopes,
                "missing_required_scopes": missing,
                # Explicit signal so JSON consumers don't have to compare
                # versions themselves to know the grants in `trust.json`
                # were already invalidated by a version bump.
                "trust_version_stale": stale_version,
            }
        )

    if json_output:
        # Plain stdout (no Rich highlighting) so consumers can pipe to jq.
        typer.echo(json.dumps(rows, indent=2))
        return

    table = create_table(title="Installed UserLevel plugins")
    for column in ("name", "version", "source", "trust", "scopes", "missing"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            row["name"],
            row["version"],
            row["source_kind"],
            row["trust_state"],
            ", ".join(row["granted_scopes"]) or "(none)",
            ", ".join(row["missing_required_scopes"]) or "(none)",
        )
    print_table(table)


__all__ = [
    "app",
    "discover_command",
    "inspect_command",
    "list_command",
]
