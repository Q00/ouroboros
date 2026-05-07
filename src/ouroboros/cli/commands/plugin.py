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
from ouroboros.plugin.trust_store import DEFAULT_TRUST_ROOT, TrustStore

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


def _trust_state_label(
    manifest: PluginManifest,
    trust_store: TrustStore,
) -> str:
    if manifest.source.type == "first_party":
        return "first_party"
    record = trust_store.read(manifest.name)
    if record is not None and record.granted_scopes:
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
        typer.Option("--lockfile", help="Override the lockfile path (default: ~/.ouroboros/plugins.lock)."),
    ] = None,
    trust_root: Annotated[
        Path | None,
        typer.Option("--trust-root", help="Override the trust root (default: ~/.ouroboros/plugins)."),
    ] = None,
) -> None:
    """Show installed plugin metadata + trust state.

    Unlike `discover`, this reads the lockfile and trust store. It still
    does not mutate any state.
    """
    lock = Lockfile(lockfile_path or DEFAULT_LOCKFILE_PATH)
    trust = TrustStore(root=trust_root or DEFAULT_TRUST_ROOT)

    entries = lock.read()
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

    record = trust.read(name)
    granted = [g.scope for g in record.granted_scopes] if record else []

    print_info(f"{manifest.name} {manifest.version} ({entry.source_kind})")
    console.print(f"  installed_at:   {entry.installed_at}")
    console.print(f"  plugin_home:    {entry.plugin_home}")
    if entry.repository:
        console.print(f"  repository:     {entry.repository}")
    if entry.git_sha:
        console.print(f"  git_sha:        {entry.git_sha}")
    console.print(f"  trust_state:    {_trust_state_label(manifest, trust)}")
    console.print(
        f"  granted_scopes: {', '.join(granted) if granted else '(none)'}"
    )
    required_perms = [p.scope for p in manifest.permissions if p.required]
    missing = [s for s in required_perms if s not in granted]
    if missing:
        console.print(
            f"  missing scopes: {', '.join(missing)} "
            f"(invocation will be blocked until granted)"
        )


@app.command("list")
def list_command(
    lockfile_path: Annotated[
        Path | None,
        typer.Option("--lockfile", help="Override the lockfile path (default: ~/.ouroboros/plugins.lock)."),
    ] = None,
    trust_root: Annotated[
        Path | None,
        typer.Option("--trust-root", help="Override the trust root (default: ~/.ouroboros/plugins)."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON for piping; suppresses table formatting."),
    ] = False,
) -> None:
    """List installed plugins with their trust state."""
    lock = Lockfile(lockfile_path or DEFAULT_LOCKFILE_PATH)
    trust = TrustStore(root=trust_root or DEFAULT_TRUST_ROOT)

    entries = lock.read()
    if not entries:
        if json_output:
            # Plain stdout (no Rich highlighting) so consumers can pipe to jq.
            typer.echo(json.dumps([]))
        else:
            print_info("no plugins installed")
        return

    rows = []
    for entry in sorted(entries.values(), key=lambda e: e.name):
        record = trust.read(entry.name)
        scopes = [g.scope for g in record.granted_scopes] if record else []
        rows.append(
            {
                "name": entry.name,
                "version": entry.version,
                "source_kind": entry.source_kind,
                "trust_state": "trusted" if scopes else "installed",
                "granted_scopes": scopes,
            }
        )

    if json_output:
        # Plain stdout (no Rich highlighting) so consumers can pipe to jq.
        typer.echo(json.dumps(rows, indent=2))
        return

    table = create_table(title="Installed UserLevel plugins")
    for column in ("name", "version", "source", "trust", "scopes"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            row["name"],
            row["version"],
            row["source_kind"],
            row["trust_state"],
            ", ".join(row["granted_scopes"]) or "(none)",
        )
    print_table(table)


__all__ = [
    "app",
    "discover_command",
    "inspect_command",
    "list_command",
]
