"""`ooo plugin` command group.

UserLevel plugin manager CLI. Implements Q00/ouroboros#731 (locked spec).

Read-only subcommands: `discover`, `inspect`, `list`.
State-mutating subcommands: `add`, `install`, `trust`, `disable`, `remove`.

Anti-patterns explicitly rejected:
  - subdirectory-leaking install strings such as
    `git+https://.../foo.git#plugins/<name>` — these couple the install
    URL to internal repo layout and are forbidden by the locked spec.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import shutil
import subprocess
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
from ouroboros.plugin.ledger_adapter import wrap_plugin_event
from ouroboros.plugin.lockfile import DEFAULT_LOCKFILE_PATH, LockEntry, Lockfile
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


# ---------------------------------------------------------------------------
# State-mutating subcommands
# ---------------------------------------------------------------------------


# The anti-pattern install string explicitly forbidden by the locked spec.
# Examples: git+https://.../foo.git#plugins/github-pr-ops
_REJECTED_FRAGMENT_PREFIX = "#plugins/"


def _reject_subdirectory_form(target: str) -> None:
    if _REJECTED_FRAGMENT_PREFIX in target:
        print_error(
            "subdirectory-form install strings (#plugins/...) are not "
            "supported. Use `ooo plugin add <repo-url> --plugin <name>` "
            "instead."
        )
        raise typer.Exit(code=1)


def _looks_like_url(target: str) -> bool:
    return target.startswith(("http://", "https://", "git+http://", "git+https://", "git@"))


def _shallow_clone(repo_url: str, dest: Path) -> str:
    """Run `git clone --depth 1` into `dest`. Returns the resolved git SHA."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth", "1", repo_url, str(dest)],
        check=True,
        capture_output=True,
        text=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(dest),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return sha


def _enumerate_catalog(repo_root: Path) -> list[PluginManifest]:
    """Read every `plugins/<name>/ouroboros.plugin.json` from a checked-out repo."""
    plugins_dir = repo_root / "plugins"
    if not plugins_dir.is_dir():
        print_error(f"no `plugins/` directory in {repo_root}")
        raise typer.Exit(code=1)
    manifests: list[PluginManifest] = []
    for entry in sorted(plugins_dir.iterdir()):
        manifest_path = entry / "ouroboros.plugin.json"
        if not manifest_path.is_file():
            continue
        manifests.append(load_manifest(manifest_path))
    if not manifests:
        print_error(f"no manifests found under {plugins_dir}")
        raise typer.Exit(code=1)
    return manifests


def _select_plugins(
    catalog: list[PluginManifest],
    requested: list[str] | None,
) -> list[PluginManifest]:
    """Return manifests matching `requested`, or prompt interactively."""
    by_name = {m.name: m for m in catalog}

    if requested:
        unknown = [r for r in requested if r not in by_name]
        if unknown:
            print_error(
                f"plugin(s) not in repository catalog: {sorted(unknown)} "
                f"(available: {sorted(by_name)})"
            )
            raise typer.Exit(code=1)
        return [by_name[r] for r in requested]

    # Interactive multi-select via questionary (optional import — fall back
    # to a clear error if missing so contributors know how to install).
    try:
        import questionary
    except ImportError:
        print_error(
            "interactive multi-select requires `questionary`; install it or "
            "pass `--plugin <name>` for non-interactive selection. "
            f"(catalog has: {sorted(by_name)})"
        )
        raise typer.Exit(code=1)

    choices = [
        questionary.Choice(
            title=f"{m.name:<25} {m.version}  {m.description or ''}",
            value=m.name,
        )
        for m in catalog
    ]
    answers = questionary.checkbox(
        "Select plugins to install:",
        choices=choices,
    ).ask()
    if not answers:
        print_info("no plugins selected; aborting")
        raise typer.Exit(code=0)
    return [by_name[a] for a in answers]


def _manifest_checksum(plugin_home: Path) -> str:
    """sha256 of the manifest file (canonical content, not parsed)."""
    raw = (plugin_home / "ouroboros.plugin.json").read_bytes()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _install_one(
    *,
    manifest: PluginManifest,
    plugin_home: Path,
    lock: Lockfile,
    source_kind: str,
    repository: str | None,
    git_sha: str | None,
) -> LockEntry:
    """Register one plugin in the lockfile. No trust granted here."""
    entry = LockEntry(
        name=manifest.name,
        version=manifest.version,
        source_kind=source_kind,
        repository=repository,
        git_sha=git_sha,
        manifest_checksum=_manifest_checksum(plugin_home),
        installed_at=datetime.datetime.now(tz=datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        plugin_home=str(plugin_home),
    )
    lock.add(entry)
    return entry


@app.command("add")
def add_command(
    target: Annotated[
        str,
        typer.Argument(help="Repository URL or local path."),
    ],
    plugin_names: Annotated[
        list[str] | None,
        typer.Option(
            "--plugin",
            help="Non-interactive: name of a plugin in the repo catalog. "
            "Repeatable.",
        ),
    ] = None,
    cache_root: Annotated[
        Path | None,
        typer.Option(
            "--cache-root",
            help="Where to clone repo URLs (default: ~/.ouroboros/cache).",
        ),
    ] = None,
    plugin_home_root: Annotated[
        Path | None,
        typer.Option(
            "--plugin-home-root",
            help="Where to install plugin homes (default: ~/.ouroboros/plugins).",
        ),
    ] = None,
    lockfile_path: Annotated[
        Path | None,
        typer.Option("--lockfile", help="Override the lockfile path."),
    ] = None,
) -> None:
    """Install one or more plugins from a repo URL or local path.

    Anti-pattern install strings (e.g. `#plugins/<name>`) are rejected.
    """
    _reject_subdirectory_form(target)

    cache_root = cache_root or Path.home() / ".ouroboros" / "cache"
    plugin_home_root = plugin_home_root or Path.home() / ".ouroboros" / "plugins"
    lock = Lockfile(lockfile_path or DEFAULT_LOCKFILE_PATH)

    if _looks_like_url(target):
        # Shallow clone into cache_root/<sanitized-host-path>.
        sanitized = (
            target.replace("https://", "")
            .replace("http://", "")
            .replace("git@", "")
            .replace(":", "_")
            .replace("/", "_")
            .strip("_")
        )
        clone_dest = cache_root / sanitized
        if clone_dest.exists():
            shutil.rmtree(clone_dest)
        try:
            git_sha = _shallow_clone(target, clone_dest)
        except subprocess.CalledProcessError as exc:
            print_error(f"git clone failed: {exc.stderr.strip() if exc.stderr else exc}")
            raise typer.Exit(code=1) from exc
        repo_root = clone_dest
        source_kind = "git"
        repository = target
    else:
        # Local path source.
        repo_root = Path(target).expanduser().resolve()
        if not repo_root.is_dir():
            print_error(f"local path not a directory: {repo_root}")
            raise typer.Exit(code=1)
        git_sha = None
        source_kind = "local"
        repository = None

    catalog = _enumerate_catalog(repo_root)
    selected = _select_plugins(catalog, plugin_names)

    installed: list[str] = []
    for manifest in selected:
        plugin_home = plugin_home_root / manifest.name
        if plugin_home.exists():
            shutil.rmtree(plugin_home)
        shutil.copytree(repo_root / "plugins" / manifest.name, plugin_home)
        _install_one(
            manifest=manifest,
            plugin_home=plugin_home,
            lock=lock,
            source_kind=source_kind,
            repository=repository,
            git_sha=git_sha,
        )
        installed.append(f"{manifest.name} {manifest.version}")
        required = [p.scope for p in manifest.permissions if p.required]
        if required:
            console.print(
                f"  required scopes (run `ooo plugin trust {manifest.name} "
                f"--scope <scope>`): {', '.join(required)}"
            )

    print_success(f"Installed: {'; '.join(installed)}")


@app.command("install")
def install_command(
    path: Annotated[
        Path,
        typer.Argument(help="Local plugin directory containing ouroboros.plugin.json."),
    ],
    plugin_home_root: Annotated[
        Path | None,
        typer.Option("--plugin-home-root"),
    ] = None,
    lockfile_path: Annotated[
        Path | None,
        typer.Option("--lockfile"),
    ] = None,
) -> None:
    """Register a single local plugin directory in the lockfile.

    Equivalent to `ooo plugin add <local-parent-dir> --plugin <name>`,
    but accepts the plugin directory directly so callers don't need the
    catalog convention.
    """
    plugin_home_root = plugin_home_root or Path.home() / ".ouroboros" / "plugins"
    lock = Lockfile(lockfile_path or DEFAULT_LOCKFILE_PATH)

    src = path.expanduser().resolve()
    if not (src / "ouroboros.plugin.json").is_file():
        print_error(f"no ouroboros.plugin.json in {src}")
        raise typer.Exit(code=1)
    try:
        manifest = load_manifest(src / "ouroboros.plugin.json")
    except PluginManifestError as exc:
        print_error(
            f"manifest invalid at {exc.path}: "
            f"{exc.json_pointer or '(root)'}: {exc.args[0] if exc.args else ''}"
        )
        raise typer.Exit(code=1) from exc

    plugin_home = plugin_home_root / manifest.name
    if plugin_home.exists():
        shutil.rmtree(plugin_home)
    shutil.copytree(src, plugin_home)

    _install_one(
        manifest=manifest,
        plugin_home=plugin_home,
        lock=lock,
        source_kind="local",
        repository=None,
        git_sha=None,
    )
    print_success(f"Installed: {manifest.name} {manifest.version}")


@app.command("trust")
def trust_command(
    name: Annotated[str, typer.Argument(help="Installed plugin name.")],
    scopes: Annotated[
        list[str],
        typer.Option(
            "--scope",
            help="Permission scope to grant. Repeatable. Exact-string match.",
        ),
    ],
    granted_by: Annotated[
        str,
        typer.Option(
            "--granted-by",
            help="User identity recorded in the audit trail.",
        ),
    ] = "user:cli",
    lockfile_path: Annotated[
        Path | None,
        typer.Option("--lockfile"),
    ] = None,
    trust_root: Annotated[
        Path | None,
        typer.Option("--trust-root"),
    ] = None,
    audit_log_path: Annotated[
        Path | None,
        typer.Option(
            "--audit-log",
            help="Append plugin.trusted events here as JSON Lines (default: skip).",
        ),
    ] = None,
) -> None:
    """Grant one or more scopes to an installed plugin.

    Per Q00/ouroboros-plugins#9 Q3 lock: scopes are exact strings —
    `--scope github:pull_request` does NOT imply `github:pull_request:write`.
    """
    if not scopes:
        print_error("at least one --scope is required")
        raise typer.Exit(code=1)

    lock = Lockfile(lockfile_path or DEFAULT_LOCKFILE_PATH)
    trust = TrustStore(root=trust_root or DEFAULT_TRUST_ROOT)

    entries = lock.read()
    entry = entries.get(name)
    if entry is None:
        print_error(f"{name!r} is not installed; nothing to trust")
        raise typer.Exit(code=1)

    audit_handle = audit_log_path.open("a", encoding="utf-8") if audit_log_path else None
    try:
        for scope in scopes:
            record = trust.grant(
                plugin=name,
                version=entry.version,
                scope=scope,
                granted_by=granted_by,
            )
            print_success(f"Granted: {scope} ({len(record.granted_scopes)} total scope(s))")

            # Emit plugin.trusted via the ledger adapter shape.
            audit_event = {
                "schema_version": "0.1",
                "event_type": "plugin.trusted",
                "occurred_at": datetime.datetime.now(tz=datetime.timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "plugin": {
                    "name": name,
                    "version": entry.version,
                    "source_type": "plugin_home",
                },
                "command": {
                    "namespace": "trust",
                    "name": "grant",
                    "argv": ["--scope", scope],
                },
                "trust_state": "trusted",
                "capabilities_used": [],
                "permissions_used": [],
                "result": {"status": "success", "message": f"Granted scope {scope}"},
                "provenance": {"granted_by": granted_by, "granted_scope": scope},
            }
            envelope = wrap_plugin_event(
                audit_event,
                correlation_id=f"trust-{name}-{scope}",
            )
            if audit_handle is not None:
                audit_handle.write(json.dumps(envelope) + "\n")
    finally:
        if audit_handle is not None:
            audit_handle.close()


@app.command("disable")
def disable_command(
    name: Annotated[str, typer.Argument(help="Installed plugin name.")],
    lockfile_path: Annotated[
        Path | None,
        typer.Option("--lockfile"),
    ] = None,
) -> None:
    """Disable an installed plugin (its trust file is wiped; lockfile entry stays)."""
    lock = Lockfile(lockfile_path or DEFAULT_LOCKFILE_PATH)
    entries = lock.read()
    if name not in entries:
        print_error(f"{name!r} is not installed")
        raise typer.Exit(code=1)
    # Disabling = removing all trust grants. Lockfile entry remains so the
    # user can re-enable with `ooo plugin trust ...`.
    trust = TrustStore()
    trust.remove(name)
    print_success(f"Disabled {name} (re-grant scopes to re-enable)")


@app.command("remove")
def remove_command(
    name: Annotated[str, typer.Argument(help="Installed plugin name.")],
    lockfile_path: Annotated[
        Path | None,
        typer.Option("--lockfile"),
    ] = None,
    trust_root: Annotated[
        Path | None,
        typer.Option("--trust-root"),
    ] = None,
    plugin_home_root: Annotated[
        Path | None,
        typer.Option("--plugin-home-root"),
    ] = None,
) -> None:
    """Remove an installed plugin (lockfile entry, trust file, plugin home)."""
    lock = Lockfile(lockfile_path or DEFAULT_LOCKFILE_PATH)
    trust = TrustStore(root=trust_root or DEFAULT_TRUST_ROOT)

    entries = lock.read()
    entry = entries.get(name)
    if entry is None:
        print_error(f"{name!r} is not installed")
        raise typer.Exit(code=1)

    plugin_home = Path(entry.plugin_home).expanduser()
    if plugin_home.is_dir():
        shutil.rmtree(plugin_home)

    trust.remove(name)
    lock.remove(name)

    print_success(f"Removed {name} (lockfile entry + trust file + plugin home)")


__all__ = [
    "add_command",
    "app",
    "disable_command",
    "discover_command",
    "inspect_command",
    "install_command",
    "list_command",
    "remove_command",
    "trust_command",
]
