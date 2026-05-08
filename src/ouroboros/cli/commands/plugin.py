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
import os
from pathlib import Path
import secrets
import shutil
import subprocess
from typing import Annotated

import typer

from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import (
    print_error,
    print_info,
    print_success,
)
from ouroboros.cli.formatters.tables import create_table, print_table
from ouroboros.plugin.digest import (
    canonical_tree_hash,
    normalize_local_path,
    normalize_repo_url,
)
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


def _record_applies_to_subject(
    record,
    *,
    manifest: PluginManifest,
    entry: LockEntry,
) -> bool:
    """True iff the trust record matches the install subject the
    firewall would key on. Mirrors ``firewall._record_matches_subject``
    so the CLI displays scopes only when invocation would honor them.

    Empty fields on the record are tolerated (legacy / pre-RFC trust
    files) so existing callers don't lose their grant display, but any
    populated field that disagrees with the lockfile entry voids the
    application.
    """
    if record is None:
        return False
    if record.version != manifest.version:
        return False
    if record.source_type and record.source_type != manifest.source.type:
        return False
    if record.source_identity and entry.source_identity:
        if record.source_identity != entry.source_identity:
            return False
    if record.artifact_digest and entry.artifact_digest:
        if record.artifact_digest != entry.artifact_digest:
            return False
    return True


def _subject_drift_reason(
    record,
    *,
    manifest: PluginManifest,
    entry: LockEntry,
) -> str:
    """Return a short human-readable reason explaining why a trust record
    no longer applies to the current install subject. Used by `inspect`
    to surface WHY stale grants are not displayed.
    """
    if record is None:
        return "no record"
    if record.version != manifest.version:
        return f"version drift: record={record.version!r} installed={manifest.version!r}"
    if record.source_type and record.source_type != manifest.source.type:
        return (
            f"source.type drift: record={record.source_type!r} installed={manifest.source.type!r}"
        )
    if (
        record.source_identity
        and entry.source_identity
        and record.source_identity != entry.source_identity
    ):
        return "source_identity drift (different install source)"
    if (
        record.artifact_digest
        and entry.artifact_digest
        and record.artifact_digest != entry.artifact_digest
    ):
        return "artifact_digest drift (installed bytes changed since grant)"
    return "subject changed"


def _describe_trust_state(
    manifest: PluginManifest,
    trust_store: TrustStore,
    *,
    expected_source_identity: str | None = None,
    expected_artifact_digest: str | None = None,
) -> str:
    """Compute the displayed trust state for a manifest.

    Note on naming: ``firewall.py`` defines a sibling helper that takes
    a ``TrustRecord``. This CLI helper deliberately takes the
    ``TrustStore`` itself because ``inspect``/``list`` need the
    name-only ``is_disabled`` fallback, which only the store can
    answer. Distinct names prevent reviewers from inferring the wrong
    signature from the call site.

    Per the locked RFC ("Trust identity"), the full install subject is
    ``(version, source.type, source_identity, artifact_digest)``;
    ``"trusted"`` is reserved for the state in which the firewall will
    not block invocation on the trust check. When the caller passes the
    lockfile-recorded ``expected_*`` values, this label agrees with the
    firewall's ``_record_matches_subject`` predicate exactly: a record
    bound to a stale digest reads as ``"installed"`` here just as the
    firewall would refuse it. A disabled subject reads as ``"disabled"``.
    """
    if manifest.source.type == "first_party":
        return "first_party"
    # Disable records are keyed by (name, source.type, source_identity)
    # per the RFC: a stale disable from a previous install at source A
    # MUST NOT carry over to a fresh install from source B. When the
    # caller plumbs the lockfile-recorded identity, use the
    # subject-scoped predicate; otherwise fall back to the name-only
    # check (defensive default for legacy callers).
    if expected_source_identity is not None:
        if trust_store.is_disabled_for_subject(
            manifest.name,
            source_type=manifest.source.type,
            source_identity=expected_source_identity,
        ):
            return "disabled"
    elif trust_store.is_disabled(manifest.name):
        return "disabled"
    record = trust_store.read(manifest.name)
    if record is None or record.version != manifest.version:
        return "installed"
    if record.source_type and record.source_type != manifest.source.type:
        return "installed"
    if expected_source_identity is not None and record.source_identity:
        if record.source_identity != expected_source_identity:
            return "installed"
    if expected_artifact_digest is not None and record.artifact_digest:
        if record.artifact_digest != expected_artifact_digest:
            return "installed"
    granted = {g.scope for g in record.granted_scopes}
    if not granted:
        return "installed"
    required = {p.scope for p in manifest.permissions if p.required}
    if required - granted:
        return "installed"
    return "trusted"


def _atomic_replace_dir(src: Path, dest: Path) -> None:
    """Copy `src` over `dest` atomically-as-possible.

    Strategy:
      1. Copy `src` into a sibling staging directory. If this fails, no
         change to `dest`.
      2. If `dest` already exists, rename it to a sibling `.bak-<rand>` dir
         (atomic on the same filesystem).
      3. Rename the staging dir into `dest` (atomic on the same filesystem).
      4. On any error after step 2, restore the backup and surface the
         original exception to the caller.
      5. Best-effort cleanup of the backup once the swap succeeds.

    This satisfies the locked contract that a failed `ooo plugin add` /
    `install` MUST NOT erase a previously-installed plugin home (data-loss
    avoidance).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    suffix = secrets.token_hex(6)
    staging = dest.with_name(f"{dest.name}.staging-{suffix}")
    backup = dest.with_name(f"{dest.name}.bak-{suffix}")

    # Step 1: stage copy. Pass symlinks=True so symlinks are copied as
    # links rather than dereferenced into the trusted artifact:
    #   - Security: a manifest tree with `evil → /etc/passwd` would
    #     otherwise smuggle host-file contents into plugin_home and
    #     fold them into artifact_digest as if the plugin authored them.
    #   - Digest contract: canonical_tree_hash hashes symlink targets
    #     as part of artifact identity, which only works when the
    #     install actually preserves the link.
    try:
        shutil.copytree(src, staging, symlinks=True)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise

    backup_used = False
    try:
        # Step 2: move existing dest aside.
        if dest.exists():
            os.rename(dest, backup)
            backup_used = True
        # Step 3: promote staging into place.
        os.rename(staging, dest)
    except Exception:
        # Rollback: restore backup, drop staging.
        if backup_used and not dest.exists() and backup.exists():
            try:
                os.rename(backup, dest)
            except OSError:
                pass
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise

    # Step 4: cleanup backup. Failure here is non-fatal — the new install
    # is already in place.
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)


def _maybe_invalidate_trust_for_subject_change(
    *,
    name: str,
    new_version: str,
    new_source_type: str,
    new_source_identity: str,
    new_artifact_digest: str,
    trust: TrustStore,
) -> None:
    """Invalidate the trust file when the install subject changes.

    Per the locked RFC ("Trust identity"), the trust subject is the tuple
    ``(version, source.type, source_identity, artifact_digest)``. ANY
    field changing voids prior grants — that closes both the same-name
    reinstall path and the code-substitution path.

    We compare against the trust record's own subject (not the lockfile's
    prior entry) because callers run this AFTER `_install_one` has
    updated the lockfile. The trust file remains the authoritative
    pointer to the subject that was last consented to.
    """
    record = trust.read(name)
    if record is None:
        return
    if (
        record.version == new_version
        # Treat empty record fields as "legacy / unbound" — match if the
        # new value matches OR the record is silent on that field.
        and (not record.source_type or record.source_type == new_source_type)
        and (not record.source_identity or record.source_identity == new_source_identity)
        and (not record.artifact_digest or record.artifact_digest == new_artifact_digest)
    ):
        return
    trust.reset_for_subject_change(
        name,
        new_version=new_version,
        new_source_type=new_source_type,
        new_source_identity=new_source_identity,
        new_artifact_digest=new_artifact_digest,
    )


# Retained for callers that only know the version (no install-subject
# context). New code should prefer the subject-aware variant.
def _maybe_invalidate_trust_for_version_bump(
    *,
    name: str,
    new_version: str,
    trust: TrustStore,
) -> None:
    record = trust.read(name)
    if record is None or record.version == new_version:
        return
    trust.reset_for_version_bump(name, new_version)


# ---------------------------------------------------------------------------
# Known-catalog registry — backs `ooo plugin install <name>` resolution.
# ---------------------------------------------------------------------------


DEFAULT_CATALOG_STATE_PATH = Path.home() / ".ouroboros" / "plugin-catalogs.json"


class CatalogRegistry:
    """Persistent record of catalogs the user has interacted with.

    Per the locked RFC ("How sources enter the known catalog"), v0 has
    exactly two registration paths:

    - ``plugin_home`` sources are registered by ``ooo plugin add <repo>``.
      The repo URL becomes a known catalog at that moment, regardless of
      whether the user proceeds to install anything from the selection
      prompt. Subsequent ``install``s can address that ``name`` without
      re-fetching.
    - ``local_path`` sources are registered the first time the user runs
      ``ooo plugin install <name> --from <local-path>`` against an
      absolute path.

    The registry stores one entry per ``(source_type, source_identity)``
    keying directly on the canonical identity, so reinstalls from the
    same source are idempotent.
    """

    def __init__(
        self,
        *,
        state_path: Path | None = None,
        catalog_root: Path | None = None,
    ) -> None:
        if state_path is not None:
            self.state_path = state_path
        elif catalog_root is not None:
            self.state_path = catalog_root / "plugin-catalogs.json"
        else:
            self.state_path = DEFAULT_CATALOG_STATE_PATH

    def _load(self) -> dict:
        if not self.state_path.is_file():
            return {"schema_version": "0.1", "catalogs": []}
        # Surface parse / IO failures as a typed ``ValueError`` that
        # names the path. The CLI wrappers (``add``/``install``) catch
        # this and translate to a friendly recovery hint instead of
        # propagating a raw traceback for a state file the user is
        # expected to be able to repair.
        try:
            with self.state_path.open(encoding="utf-8") as handle:
                payload = json.load(handle)
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(
                f"plugin catalog state at {self.state_path} is unreadable: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError(f"plugin catalog state at {self.state_path} is not a JSON object")
        return payload

    def _save(self, payload: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write via temp + replace so a crash mid-update never
        # leaves the catalog half-written.
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.state_path)

    def register(
        self,
        *,
        source_type: str,
        source_identity: str,
        plugin_name: str,
    ) -> None:
        """Idempotently record a (source, plugin) pair."""
        data = self._load()
        catalogs: list[dict] = data.setdefault("catalogs", [])
        for entry in catalogs:
            if (
                entry.get("source_type") == source_type
                and entry.get("source_identity") == source_identity
            ):
                names = set(entry.get("plugins", []))
                names.add(plugin_name)
                entry["plugins"] = sorted(names)
                self._save(data)
                return
        catalogs.append(
            {
                "source_type": source_type,
                "source_identity": source_identity,
                "plugins": [plugin_name],
            }
        )
        self._save(data)

    def find_sources_for(self, plugin_name: str) -> list[dict]:
        """Return every catalog entry that exposes ``plugin_name``."""
        data = self._load()
        return [
            entry for entry in data.get("catalogs", []) if plugin_name in entry.get("plugins", [])
        ]

    def find_by_identity(
        self,
        *,
        source_type: str,
        source_identity: str,
    ) -> dict | None:
        data = self._load()
        for entry in data.get("catalogs", []):
            if (
                entry.get("source_type") == source_type
                and entry.get("source_identity") == source_identity
            ):
                return entry
        return None


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
        # First-party manifests bypass the user-facing trust prompt
        # (RFC: "First-party trust semantics"), so the "must be
        # trusted" hint is misleading for them — those scopes are
        # already implicitly trusted at boot.
        if manifest.source.type == "first_party":
            console.print("  required scopes (implicitly trusted for first-party):")
        else:
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
        typer.Option("--trust-root", help="Override the trust root (default: ~/.ouroboros/trust)."),
    ] = None,
) -> None:
    """Show installed plugin metadata + trust state.

    Unlike `discover`, this reads the lockfile and trust store. It still
    does not mutate any state.
    """
    lock = Lockfile(lockfile_path or DEFAULT_LOCKFILE_PATH)
    trust = TrustStore(root=trust_root or DEFAULT_TRUST_ROOT)

    # Treat malformed local state as a first-class diagnostic condition,
    # not a stack trace. `inspect` is precisely the command operators
    # reach for when something is wrong; crashing here defeats its
    # purpose. Lockfile.read() raises ValueError on schema violations
    # and OSError on filesystem issues — both surface a friendly hint
    # pointing the user at the offending file path.
    try:
        entries = lock.read()
    except (ValueError, OSError) as exc:
        print_error(
            f"lockfile is unreadable ({lock.path}): {exc}. "
            f"Inspect or replace the file, or pass --lockfile to point "
            f"at a known-good copy."
        )
        raise typer.Exit(code=1) from exc
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

    # First-party programs bypass the user-facing trust flow at the
    # firewall (per the RFC's "First-party trust semantics"); a stale
    # or corrupt trust file MUST NOT block their inspection. We also
    # protect every other source.type from raw decode/IO errors so the
    # operator sees a hint rather than a traceback.
    if manifest.source.type == "first_party":
        record = None
    else:
        try:
            record = trust.read(name)
        except (ValueError, OSError) as exc:
            print_error(
                f"trust store is unreadable for {name!r}: {exc}. "
                f"Pass --trust-root to point at a known-good copy, or "
                f"remove the offending file."
            )
            raise typer.Exit(code=1) from exc
    # A trust record applies to the displayed scopes only when its full
    # install subject matches the lockfile entry: version,
    # source.type, source_identity, and artifact_digest. Otherwise the
    # firewall would refuse the grant at invocation time, and showing
    # the stale scopes would mislead the user about what is actually
    # honored. ``_record_applies_to_subject`` mirrors the firewall's
    # ``_record_matches_subject`` predicate.
    applies = _record_applies_to_subject(record, manifest=manifest, entry=entry)
    granted = [g.scope for g in record.granted_scopes] if applies and record else []

    print_info(f"{manifest.name} {manifest.version} ({entry.source_kind})")
    console.print(f"  installed_at:   {entry.installed_at}")
    console.print(f"  plugin_home:    {entry.plugin_home}")
    if entry.repository:
        console.print(f"  repository:     {entry.repository}")
    if entry.git_sha:
        console.print(f"  git_sha:        {entry.git_sha}")
    console.print(
        f"  trust_state:    {_describe_trust_state(manifest, trust, expected_source_identity=entry.source_identity or None, expected_artifact_digest=entry.artifact_digest or None)}"
    )
    console.print(f"  granted_scopes: {', '.join(granted) if granted else '(none)'}")
    if record is not None and not applies:
        # Surface why the grants don't apply, naming the field that drifted.
        reason = _subject_drift_reason(record, manifest=manifest, entry=entry)
        console.print(f"  trust note:     stored grants are stale ({reason}); re-grant required")
    required_perms = [p.scope for p in manifest.permissions if p.required]
    missing = [s for s in required_perms if s not in granted]
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
        typer.Option("--trust-root", help="Override the trust root (default: ~/.ouroboros/trust)."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON for piping; suppresses table formatting."),
    ] = False,
) -> None:
    """List installed plugins with their trust state."""
    lock = Lockfile(lockfile_path or DEFAULT_LOCKFILE_PATH)
    trust = TrustStore(root=trust_root or DEFAULT_TRUST_ROOT)

    # Same operator-friendly handling as `inspect` (see above): malformed
    # local state must not crash the diagnostic command meant to help
    # the user notice and recover from it.
    try:
        entries = lock.read()
    except (ValueError, OSError) as exc:
        print_error(
            f"lockfile is unreadable ({lock.path}): {exc}. "
            f"Inspect or replace the file, or pass --lockfile to point "
            f"at a known-good copy."
        )
        raise typer.Exit(code=1) from exc
    if not entries:
        if json_output:
            # Plain stdout (no Rich highlighting) so consumers can pipe to jq.
            typer.echo(json.dumps([]))
        else:
            print_info("no plugins installed")
        return

    rows = []
    for entry in sorted(entries.values(), key=lambda e: e.name):
        # Per the locked RFC, a record only applies to the install
        # subject if every field of (version, source.type,
        # source_identity, artifact_digest) matches. Same-version
        # source/digest drift makes the firewall refuse the grant, so
        # the displayed scopes must reflect that — otherwise the
        # state label and the scope list contradict each other.
        # Compute the displayed trust state through the same predicate
        # the firewall uses, so list/inspect/firewall agree on the
        # invariant: "trusted" iff invocation will not be blocked on
        # the trust check (record current + grants cover required).
        manifest_path = Path(entry.plugin_home).expanduser() / "ouroboros.plugin.json"
        try:
            manifest = load_manifest(manifest_path)
        except PluginManifestError:
            # Manifest unreadable post-install (e.g. external mutation):
            # show conservatively as "installed", no granted scopes.
            rows.append(
                {
                    "name": entry.name,
                    "version": entry.version,
                    "source_kind": entry.source_kind,
                    "trust_state": "installed",
                    "granted_scopes": [],
                }
            )
            continue
        # First-party programs are implicitly trusted by the firewall
        # (RFC: "First-party trust semantics"), so skip the trust read
        # entirely. Use the ``first_party`` label so machine-readable
        # state agrees across ``list``, ``inspect``, ``_describe_trust_state``,
        # and the firewall audit events — they all use the same string
        # for this case.
        if manifest.source.type == "first_party":
            rows.append(
                {
                    "name": entry.name,
                    "version": entry.version,
                    "source_kind": entry.source_kind,
                    "trust_state": "first_party",
                    "granted_scopes": [p.scope for p in manifest.permissions if p.required],
                }
            )
            continue
        # For non-first-party entries, treat malformed trust state as
        # "trust unreadable" rather than crashing the listing — the
        # operator should still see every other plugin's state.
        try:
            record = trust.read(entry.name)
            trust_state = _describe_trust_state(
                manifest,
                trust,
                expected_source_identity=entry.source_identity or None,
                expected_artifact_digest=entry.artifact_digest or None,
            )
            applies = _record_applies_to_subject(record, manifest=manifest, entry=entry)
            scopes = [g.scope for g in record.granted_scopes] if applies and record else []
        except (ValueError, OSError):
            trust_state = "trust_unreadable"
            scopes = []
        rows.append(
            {
                "name": entry.name,
                "version": entry.version,
                "source_kind": entry.source_kind,
                "trust_state": trust_state,
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


# Names that the top-level `ooo` CLI reserves for first-party programs
# and built-in subcommands. A third-party plugin manifest declaring any
# of these as ``name`` would silently shadow the built-in dispatch (or
# produce ambiguous resolution at boot), so we refuse the install.
#
# Per the locked RFC ("UX / Plugin name → command-namespace mapping"),
# the install MUST refuse a new install whose manifest ``name``
# collides with any name already occupying the top-level ``ooo``
# command namespace. The reserved set is the union of:
#   - first-party UserLevel programs (`auto`, `run`, `pm`, `plugin`,
#     `init`, `cancel`, `codex`, `config`, `detect`, `mcp`, `setup`,
#     `status`, `tui`, `resume`, `uninstall`),
#   - top-level `ooo` built-ins / aliases that are not first-party
#     programs (`help`, `version`, `monitor`).
#
# Same-name third-party reinstall checking happens at the lockfile
# layer (``Lockfile.add`` overwrites the entry by name) AND at the
# UserLevel registry (`get`/`get_by_namespace` collision detection).
# This set covers the third boundary the RFC names: collision with
# names the core release artifact owns.
_RESERVED_TOP_LEVEL_NAMES: frozenset[str] = frozenset(
    {
        # First-party UserLevel programs
        "auto",
        "init",
        "run",
        "config",
        "status",
        "cancel",
        "codex",
        "mcp",
        "setup",
        "detect",
        "tui",
        "pm",
        "plugin",
        "resume",
        "uninstall",
        # Built-in CLI surface
        "help",
        "version",
        "monitor",
    }
)


def _refuse_reserved_name(name: str) -> None:
    """Refuse to install a plugin whose name collides with a reserved
    top-level command. Per the RFC ("UX / Plugin name →
    command-namespace mapping"), name collisions MUST produce an
    explicit error rather than silently shadow the built-in dispatch.
    """
    if name in _RESERVED_TOP_LEVEL_NAMES:
        print_error(
            f"refusing to install plugin {name!r}: that name is reserved "
            "by a first-party `ooo` command or a built-in subcommand. "
            "Rename the plugin's manifest `name` field to avoid silent "
            "dispatch shadowing."
        )
        raise typer.Exit(code=1)


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
    """True if `target` is a clone URL we should pass through `git clone`.

    Mirrors the prefixes that `_normalize_clone_url` knows how to strip,
    so any `git+...` form `_normalize_clone_url` accepts is also routed
    through this URL detector. Without this symmetry the install path
    would fall into the local-path branch for documented forms (notably
    ``git+ssh://...``) and fail with "not a directory" instead of
    cloning.
    """
    return target.startswith(
        (
            "http://",
            "https://",
            "git+http://",
            "git+https://",
            "git+ssh://",
            "ssh://",
            "git@",
        )
    )


def _normalize_clone_url(target: str) -> str:
    """Strip the Python-style `git+` prefix that pip/uv accept but Git itself
    does not understand.

    `_looks_like_url()` accepts `git+https://...` / `git+http://...` / `git+ssh://`
    forms because users routinely paste them from Python packaging tooling. The
    underlying `git clone` rejects that prefix though — we normalize at the
    transport boundary so the prefix is purely a CLI convenience.
    """
    for prefix in ("git+https://", "git+http://", "git+ssh://", "git+"):
        if target.startswith(prefix):
            return target[len("git+") :]
    return target


def _shallow_clone(repo_url: str, dest: Path) -> str:
    """Run `git clone --depth 1` into `dest`. Returns the resolved git SHA."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth", "1", _normalize_clone_url(repo_url), str(dest)],
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
    """Read every `plugins/<name>/ouroboros.plugin.json` from a checked-out repo.

    Invalid sibling manifests must NOT block installing valid plugins from
    a mixed-quality repo. Each parse error is surfaced as a yellow `skip:`
    warning so the user sees what was bypassed; the function only fails if
    nothing at all parsed.
    """
    plugins_dir = repo_root / "plugins"
    if not plugins_dir.is_dir():
        print_error(f"no `plugins/` directory in {repo_root}")
        raise typer.Exit(code=1)
    manifests: list[PluginManifest] = []
    skipped: list[tuple[str, str]] = []
    for entry in sorted(plugins_dir.iterdir()):
        manifest_path = entry / "ouroboros.plugin.json"
        if not manifest_path.is_file():
            continue
        try:
            manifests.append(load_manifest(manifest_path))
        except PluginManifestError as exc:
            loc = exc.json_pointer or "(root)"
            msg = exc.args[0] if exc.args else "invalid manifest"
            skipped.append((entry.name, f"{loc}: {msg}"))
    for dir_name, reason in skipped:
        console.print(f"  [yellow]skip[/]: {dir_name}: invalid manifest ({reason})")
    if not manifests:
        print_error(f"no valid manifests found under {plugins_dir}")
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
    source_type: str,
    source_identity: str,
    artifact_digest: str,
) -> LockEntry:
    """Register one plugin in the lockfile. No trust granted here.

    Per the locked RFC ("Trust identity"), the lockfile entry carries the
    full ``(source.type, source_identity, artifact_digest)`` triple so
    the firewall can detect code substitution and same-name reinstalls
    from a different source.

    Refuses the install if the manifest's ``name`` collides with a
    reserved top-level command — that check happens BEFORE the lockfile
    is touched so a rejected install never produces a half-applied state.
    """
    _refuse_reserved_name(manifest.name)
    entry = LockEntry(
        name=manifest.name,
        version=manifest.version,
        source_kind=source_kind,
        repository=repository,
        git_sha=git_sha,
        manifest_checksum=_manifest_checksum(plugin_home),
        installed_at=datetime.datetime.now(tz=datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        plugin_home=str(plugin_home),
        source_type=source_type,
        source_identity=source_identity,
        artifact_digest=artifact_digest,
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
            help="Non-interactive: name of a plugin in the repo catalog. Repeatable.",
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
    trust_root: Annotated[
        Path | None,
        typer.Option(
            "--trust-root",
            help="Override the trust root (default: ~/.ouroboros/trust). "
            "Used to invalidate prior grants on a version bump.",
        ),
    ] = None,
    catalog_state_path: Annotated[
        Path | None,
        typer.Option(
            "--catalog-state",
            help=(
                "Override the known-catalog state path "
                "(default: ~/.ouroboros/plugin-catalogs.json)."
            ),
        ),
    ] = None,
) -> None:
    """Install one or more plugins from a repo URL or local path.

    Anti-pattern install strings (e.g. `#plugins/<name>`) are rejected.
    """
    _reject_subdirectory_form(target)

    cache_root = cache_root or Path.home() / ".ouroboros" / "cache"
    plugin_home_root = plugin_home_root or Path.home() / ".ouroboros" / "plugins"
    lock = Lockfile(lockfile_path or DEFAULT_LOCKFILE_PATH)
    trust = TrustStore(root=trust_root or DEFAULT_TRUST_ROOT)

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
        source_identity = normalize_repo_url(target)
    else:
        # Local path source.
        repo_root = Path(target).expanduser().resolve()
        if not repo_root.is_dir():
            print_error(f"local path not a directory: {repo_root}")
            raise typer.Exit(code=1)
        git_sha = None
        source_kind = "local"
        repository = None
        # Each catalog plugin gets its own canonical source_identity
        # (its absolute path on disk), recorded per-plugin below.
        source_identity = ""  # set per-plugin below

    catalog = _enumerate_catalog(repo_root)
    selected = _select_plugins(catalog, plugin_names)

    # Per the RFC, `add` registers each catalog source as a known catalog
    # so future `ooo plugin install <name>` invocations can resolve it
    # without re-fetching. The catalog file is keyed by source_identity.
    catalog_state = CatalogRegistry(
        state_path=catalog_state_path,
        catalog_root=(
            plugin_home_root.parent if catalog_state_path is None and plugin_home_root else None
        ),
    )
    # Probe the catalog state once up-front so a corrupted
    # ``plugin-catalogs.json`` produces a friendly recovery hint
    # instead of crashing the install loop with a partial result.
    try:
        catalog_state._load()  # noqa: SLF001 — intentional pre-flight probe
    except ValueError as exc:
        print_error(
            f"{exc} "
            f"Inspect or delete the file (it will be regenerated on the "
            f"next successful add/install), or pass --catalog-state to "
            f"point at a known-good copy."
        )
        raise typer.Exit(code=1) from exc

    installed: list[str] = []
    for manifest in selected:
        plugin_home = plugin_home_root / manifest.name
        # Per-plugin source_identity for local catalogs.
        if source_kind == "local":
            plugin_source_identity = str((repo_root / "plugins" / manifest.name).resolve())
        else:
            plugin_source_identity = source_identity
        # Per the RFC, the persisted ``source_type`` is the manifest's
        # declared value, not an inference from the install transport.
        # Same plugin can travel through different transports (URL
        # clone vs. local checkout) but the trust subject is keyed by
        # what the manifest says, so the firewall keeps a single
        # consistent identity for it.
        manifest_source_type = manifest.source.type
        # Atomic install: prior plugin home survives any copy failure.
        # We DO NOT invalidate trust before this step — if the copy
        # fails, the user should still own the unchanged install with
        # its prior grants intact.
        _atomic_replace_dir(repo_root / "plugins" / manifest.name, plugin_home)
        # Compute the canonical tree hash of the freshly-installed bytes.
        # Per the RFC, this is the input to the trust subject's
        # ``artifact_digest`` field; the firewall recomputes it before
        # every invocation and fails closed on drift.
        artifact_digest = canonical_tree_hash(plugin_home)
        _install_one(
            manifest=manifest,
            plugin_home=plugin_home,
            lock=lock,
            source_kind=source_kind,
            repository=repository,
            git_sha=git_sha,
            source_type=manifest_source_type,
            source_identity=plugin_source_identity,
            artifact_digest=artifact_digest,
        )
        # Catalog registration: record the source so `install <name>`
        # can find it later. The recorded ``source_identity`` MUST match
        # the lockfile's per-plugin ``source_identity``; otherwise a
        # later ``install <name>`` resolved through the catalog would
        # appear to come from a different source than the original
        # ``add`` and force an unnecessary trust reset on the user's
        # already-trusted plugin (RFC: trust subject is keyed by
        # ``source_identity``). For git URLs the per-plugin and
        # repo-root identities are the same normalized URL; for local
        # paths we record the per-plugin path so the catalog and the
        # lockfile agree.
        catalog_state.register(
            source_type=manifest_source_type,
            source_identity=plugin_source_identity,
            plugin_name=manifest.name,
        )
        # Now that the new version is on disk and recorded in the
        # lockfile, invalidate prior grants if ANY field of the install
        # subject changed (RFC: subject = (version, source.type,
        # source_identity, artifact_digest)). The firewall additionally
        # enforces subject-mismatch invalidation as defense-in-depth, so
        # a crash in the narrow window between _install_one and this
        # call still keeps the plugin gated until the user re-grants.
        _maybe_invalidate_trust_for_subject_change(
            name=manifest.name,
            new_version=manifest.version,
            new_source_type=manifest_source_type,
            new_source_identity=plugin_source_identity,
            new_artifact_digest=artifact_digest,
            trust=trust,
        )
        # An install at any digest also clears the disable record for
        # this subject (per the RFC: "remove ALSO deletes any disable
        # record" and re-trust is the re-enable path). Keep the disable
        # signal for `disable` and the subject-stable
        # `(name, source.type, source_identity)` keying — meaning a
        # vanilla `add`/`install` does NOT auto-clear disable. Only
        # `trust` and `remove` clear it (RFC: "Re-enabling is performed
        # by re-running ooo plugin trust").
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
    target: Annotated[
        str,
        typer.Argument(
            help=(
                "Either: a plugin name (resolves via the known-catalog registry — "
                "ambiguous names require --from), OR a local plugin directory "
                "containing ouroboros.plugin.json (legacy form)."
            ),
        ),
    ],
    from_source: Annotated[
        str | None,
        typer.Option(
            "--from",
            help=(
                "Qualify which source to install <name> from: a repo URL "
                "(plugin_home) or an absolute local path (local_path, "
                "register-on-first-use)."
            ),
        ),
    ] = None,
    plugin_home_root: Annotated[
        Path | None,
        typer.Option("--plugin-home-root"),
    ] = None,
    lockfile_path: Annotated[
        Path | None,
        typer.Option("--lockfile"),
    ] = None,
    trust_root: Annotated[
        Path | None,
        typer.Option(
            "--trust-root",
            help="Override the trust root (default: ~/.ouroboros/trust). "
            "Used to invalidate prior grants on a subject change.",
        ),
    ] = None,
    cache_root: Annotated[
        Path | None,
        typer.Option(
            "--cache-root",
            help="Where to clone repo URLs for --from (default: ~/.ouroboros/cache).",
        ),
    ] = None,
    catalog_state_path: Annotated[
        Path | None,
        typer.Option(
            "--catalog-state",
            help="Override the known-catalog state path (default: ~/.ouroboros/plugin-catalogs.json).",
        ),
    ] = None,
) -> None:
    """Install one plugin.

    The RFC ("UX / `add` vs `install`") defines `install` as the
    non-interactive primitive, with three resolution paths:

    - **Default form** — ``ooo plugin install <name>`` — succeeds only
      if exactly one known catalog exposes that name. Multi-source
      ambiguity raises an explicit error listing the candidates.
    - **Qualified form** — ``ooo plugin install <name> --from <url|path>``
      — selects an explicit source. For ``--from <local-path>`` this is
      ALSO the register-on-first-use verb for `local_path` sources.
    - **Legacy direct-directory form** — ``ooo plugin install
      <plugin-dir>`` — kept for ergonomic parity with
      ``ooo plugin discover`` / pre-RFC scripts. The argument must be
      an existing directory containing ``ouroboros.plugin.json``.
    """
    plugin_home_root = plugin_home_root or Path.home() / ".ouroboros" / "plugins"
    cache_root = cache_root or Path.home() / ".ouroboros" / "cache"
    lock = Lockfile(lockfile_path or DEFAULT_LOCKFILE_PATH)
    trust = TrustStore(root=trust_root or DEFAULT_TRUST_ROOT)
    catalog_state = CatalogRegistry(
        state_path=catalog_state_path,
        catalog_root=plugin_home_root.parent if catalog_state_path is None else None,
    )
    # See ``add_command`` — same up-front probe so a corrupted
    # catalog file produces a friendly recovery hint rather than a
    # raw traceback the operator can't easily diagnose.
    try:
        catalog_state._load()  # noqa: SLF001 — intentional pre-flight probe
    except ValueError as exc:
        print_error(
            f"{exc} "
            f"Inspect or delete the file (it will be regenerated on the "
            f"next successful add/install), or pass --catalog-state to "
            f"point at a known-good copy."
        )
        raise typer.Exit(code=1) from exc

    candidate_path = Path(target).expanduser()

    # --- Form A: legacy direct-directory form ---------------------------
    # If the target is an existing directory containing a manifest, treat
    # it as the historical "install <plugin-dir>" form. This keeps the
    # existing test surface working unchanged while the new RFC contract
    # is layered above.
    if (
        from_source is None
        and candidate_path.is_dir()
        and (candidate_path / "ouroboros.plugin.json").is_file()
    ):
        _install_from_local_directory(
            src=candidate_path.resolve(),
            plugin_home_root=plugin_home_root,
            lock=lock,
            trust=trust,
            catalog_state=catalog_state,
        )
        return

    # --- Form B: qualified form (`install <name> --from <...>`) ---------
    if from_source is not None:
        if _looks_like_url(from_source):
            _install_named_from_url(
                name=target,
                repo_url=from_source,
                cache_root=cache_root,
                plugin_home_root=plugin_home_root,
                lock=lock,
                trust=trust,
                catalog_state=catalog_state,
            )
            return
        from_path = Path(from_source).expanduser()
        if not from_path.is_absolute():
            print_error(f"--from <local-path> must be an absolute path, got: {from_source}")
            raise typer.Exit(code=1)
        if not from_path.is_dir():
            print_error(f"--from path is not a directory: {from_path}")
            raise typer.Exit(code=1)
        _install_named_from_local_path(
            name=target,
            from_path=from_path,
            plugin_home_root=plugin_home_root,
            lock=lock,
            trust=trust,
            catalog_state=catalog_state,
        )
        return

    # --- Form C: default form (`install <name>`) ------------------------
    sources = catalog_state.find_sources_for(target)
    if not sources:
        print_error(
            f"plugin {target!r} is not in any known catalog. "
            "Either run `ooo plugin add <repo-url>` first, or re-run with "
            "the qualified form `ooo plugin install <name> --from <local-path>`."
        )
        raise typer.Exit(code=1)
    if len(sources) > 1:
        listing = "\n  ".join(f"- {s['source_type']}: {s['source_identity']}" for s in sources)
        print_error(
            f"plugin name {target!r} is ambiguous across {len(sources)} known "
            f"catalogs:\n  {listing}\nRe-run with --from <repo-url|local-path> "
            "to qualify which source to install from."
        )
        raise typer.Exit(code=1)
    only = sources[0]
    # Route by transport (URL vs local path), NOT by manifest source.type.
    # The persisted ``source_type`` field is the manifest's declared
    # value (per RFC), so a `source.type="plugin_home"` manifest can
    # legitimately be registered with a filesystem ``source_identity``
    # when the user added it via a local checkout. Picking the URL
    # path on `source_type=="plugin_home"` would shell out to
    # ``git clone`` against an absolute filesystem path.
    if _looks_like_url(only["source_identity"]):
        _install_named_from_url(
            name=target,
            repo_url=only["source_identity"],
            cache_root=cache_root,
            plugin_home_root=plugin_home_root,
            lock=lock,
            trust=trust,
            catalog_state=catalog_state,
        )
    else:
        _install_named_from_local_path(
            name=target,
            from_path=Path(only["source_identity"]),
            plugin_home_root=plugin_home_root,
            lock=lock,
            trust=trust,
            catalog_state=catalog_state,
        )


def _install_from_local_directory(
    *,
    src: Path,
    plugin_home_root: Path,
    lock: Lockfile,
    trust: TrustStore,
    catalog_state: CatalogRegistry,
) -> None:
    """Legacy `install <plugin-dir>` path (kept for back-compat)."""
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
    # Atomic install: a failed copy must leave the prior install (and
    # its trust grants) untouched. Trust is only invalidated AFTER the
    # new subject is committed to disk and the lockfile.
    _atomic_replace_dir(src, plugin_home)
    artifact_digest = canonical_tree_hash(plugin_home)
    source_identity = str(src)
    # Per the RFC ("Trust identity"), the persisted ``source_type`` is
    # the manifest's declared semantic source, not the install
    # transport. The firewall keys subject-match on
    # ``manifest.source.type``, so persisting `"local_path"` for a
    # manifest that declared `plugin_home` would leave the freshly
    # installed plugin permanently stuck in the `installed` state with
    # invocation blocked.
    manifest_source_type = manifest.source.type

    _install_one(
        manifest=manifest,
        plugin_home=plugin_home,
        lock=lock,
        source_kind="local",
        repository=None,
        git_sha=None,
        source_type=manifest_source_type,
        source_identity=source_identity,
        artifact_digest=artifact_digest,
    )
    catalog_state.register(
        source_type=manifest_source_type,
        source_identity=source_identity,
        plugin_name=manifest.name,
    )
    _maybe_invalidate_trust_for_subject_change(
        name=manifest.name,
        new_version=manifest.version,
        new_source_type=manifest_source_type,
        new_source_identity=source_identity,
        new_artifact_digest=artifact_digest,
        trust=trust,
    )
    print_success(f"Installed: {manifest.name} {manifest.version}")


def _install_named_from_local_path(
    *,
    name: str,
    from_path: Path,
    plugin_home_root: Path,
    lock: Lockfile,
    trust: TrustStore,
    catalog_state: CatalogRegistry,
) -> None:
    """`install <name> --from <local-absolute-path>` register-on-first-use."""
    src = normalize_local_path(from_path)
    src_path = Path(src)
    # Two layouts are accepted:
    #  - a single-plugin directory (`<src>/ouroboros.plugin.json`)
    #  - a catalog directory (`<src>/plugins/<name>/ouroboros.plugin.json`)
    direct = src_path / "ouroboros.plugin.json"
    nested = src_path / "plugins" / name / "ouroboros.plugin.json"
    if direct.is_file():
        candidate_root = src_path
        manifest_path = direct
    elif nested.is_file():
        candidate_root = src_path / "plugins" / name
        manifest_path = nested
    else:
        print_error(
            f"no plugin {name!r} found at {src} (looked for "
            f"`ouroboros.plugin.json` and `plugins/{name}/ouroboros.plugin.json`)"
        )
        raise typer.Exit(code=1)

    try:
        manifest = load_manifest(manifest_path)
    except PluginManifestError as exc:
        print_error(
            f"manifest invalid at {exc.path}: "
            f"{exc.json_pointer or '(root)'}: {exc.args[0] if exc.args else ''}"
        )
        raise typer.Exit(code=1) from exc
    if manifest.name != name:
        print_error(
            f"manifest at {manifest_path} declares name {manifest.name!r}, "
            f"but the install command was given {name!r}; refusing to install "
            "to avoid silent name aliasing."
        )
        raise typer.Exit(code=1)

    plugin_home = plugin_home_root / manifest.name
    _atomic_replace_dir(candidate_root, plugin_home)
    artifact_digest = canonical_tree_hash(plugin_home)
    source_identity = str(candidate_root.resolve())
    # See `_install_from_local_directory` — persist manifest's source
    # type, not transport.
    manifest_source_type = manifest.source.type
    _install_one(
        manifest=manifest,
        plugin_home=plugin_home,
        lock=lock,
        source_kind="local",
        repository=None,
        git_sha=None,
        source_type=manifest_source_type,
        source_identity=source_identity,
        artifact_digest=artifact_digest,
    )
    catalog_state.register(
        source_type=manifest_source_type,
        source_identity=source_identity,
        plugin_name=manifest.name,
    )
    _maybe_invalidate_trust_for_subject_change(
        name=manifest.name,
        new_version=manifest.version,
        new_source_type=manifest_source_type,
        new_source_identity=source_identity,
        new_artifact_digest=artifact_digest,
        trust=trust,
    )
    print_success(f"Installed: {manifest.name} {manifest.version}")


def _install_named_from_url(
    *,
    name: str,
    repo_url: str,
    cache_root: Path,
    plugin_home_root: Path,
    lock: Lockfile,
    trust: TrustStore,
    catalog_state: CatalogRegistry,
) -> None:
    """`install <name> --from <repo-url>` qualified form for plugin_home sources."""
    _reject_subdirectory_form(repo_url)
    sanitized = (
        repo_url.replace("https://", "")
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
        git_sha = _shallow_clone(repo_url, clone_dest)
    except subprocess.CalledProcessError as exc:
        print_error(f"git clone failed: {exc.stderr.strip() if exc.stderr else exc}")
        raise typer.Exit(code=1) from exc

    catalog = _enumerate_catalog(clone_dest)
    by_name = {m.name: m for m in catalog}
    if name not in by_name:
        print_error(
            f"plugin {name!r} not found in catalog at {repo_url} (available: {sorted(by_name)})"
        )
        raise typer.Exit(code=1)
    manifest = by_name[name]
    plugin_home = plugin_home_root / manifest.name
    _atomic_replace_dir(clone_dest / "plugins" / manifest.name, plugin_home)
    artifact_digest = canonical_tree_hash(plugin_home)
    source_identity = normalize_repo_url(repo_url)
    # See `_install_from_local_directory` — persist manifest's source
    # type, not transport. Cloning from a URL does not by itself imply
    # ``source.type == plugin_home``; the manifest's declared value is
    # what the firewall keys against.
    manifest_source_type = manifest.source.type
    _install_one(
        manifest=manifest,
        plugin_home=plugin_home,
        lock=lock,
        source_kind="git",
        repository=repo_url,
        git_sha=git_sha,
        source_type=manifest_source_type,
        source_identity=source_identity,
        artifact_digest=artifact_digest,
    )
    catalog_state.register(
        source_type=manifest_source_type,
        source_identity=source_identity,
        plugin_name=manifest.name,
    )
    _maybe_invalidate_trust_for_subject_change(
        name=manifest.name,
        new_version=manifest.version,
        new_source_type=manifest_source_type,
        new_source_identity=source_identity,
        new_artifact_digest=artifact_digest,
        trust=trust,
    )
    print_success(f"Installed: {manifest.name} {manifest.version}")


@app.command("trust")
def trust_command(
    name: Annotated[str, typer.Argument(help="Installed plugin name.")],
    scopes: Annotated[
        list[str] | None,
        typer.Option(
            "--scope",
            help=(
                "Permission scope to grant. Repeatable. Exact-string match. "
                "Optional: omit to re-enable a disabled zero-permission "
                "plugin without granting any new scope."
            ),
        ),
    ] = None,
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

    Per the locked RFC ("Disable records / Re-enabling"), `trust` is also
    the re-enable path: it deletes any disable record bound to the
    install subject. Plugins whose manifest declares no permissions
    therefore accept an empty `--scope` set so they can be re-enabled
    after `disable`. Plugins with declared permissions still require at
    least one `--scope` argument so the user has to make an explicit
    permission decision.
    """
    # Typer passes ``None`` when ``--scope`` is omitted entirely; coerce
    # to an empty list so the rest of this function only has to handle
    # one shape.
    scopes = list(scopes) if scopes else []

    lock = Lockfile(lockfile_path or DEFAULT_LOCKFILE_PATH)
    trust = TrustStore(root=trust_root or DEFAULT_TRUST_ROOT)

    entries = lock.read()
    entry = entries.get(name)
    if entry is None:
        print_error(f"{name!r} is not installed; nothing to trust")
        raise typer.Exit(code=1)

    # Validate the requested scopes against the installed manifest's
    # declared permissions BEFORE persisting anything. A typo or an
    # undeclared scope would otherwise produce a misleading
    # "Granted: <scope>" + plugin.trusted event while the firewall still
    # blocked invocation because the real required scope was never
    # granted — a silent false-success at the trust boundary.
    manifest_path = Path(entry.plugin_home).expanduser() / "ouroboros.plugin.json"
    try:
        manifest = load_manifest(manifest_path)
    except PluginManifestError as exc:
        print_error(
            f"installed manifest is unreadable; refusing to grant trust: "
            f"{exc.path}: {exc.json_pointer or '(root)'}: "
            f"{exc.args[0] if exc.args else ''}"
        )
        raise typer.Exit(code=1) from exc
    declared = {p.scope for p in manifest.permissions}
    required = {p.scope for p in manifest.permissions if p.required}
    if not scopes:
        # Bare `ooo plugin trust <name>` — no new grants, but we can
        # still clear the disable record. This path exists so a
        # disabled plugin whose firewall block has nothing to do with
        # missing scopes (zero-permission OR all-optional permissions)
        # can actually be re-enabled. The firewall only refuses
        # invocation on missing *required* scopes, so a plugin with
        # only ``required: false`` permissions is firewall-equivalent
        # to a zero-permission plugin: re-enabling it with no grant
        # is correct. For plugins that DO declare required scopes,
        # refuse so the user is forced to make an explicit grant
        # rather than silently re-enabling without trust.
        if required:
            print_error(
                f"plugin {name!r} declares required permissions {sorted(required)!r}; "
                "pass --scope to grant at least one before re-enabling."
            )
            raise typer.Exit(code=1)
    else:
        undeclared = sorted(s for s in scopes if s not in declared)
        if undeclared:
            print_error(
                f"scope(s) {undeclared!r} are not declared by {name!r}'s manifest "
                f"(declared: {sorted(declared) if declared else '(none)'}); "
                "refusing to grant. Trust may only be granted for scopes the "
                "plugin actually requests — typos must not silently persist as "
                "phantom grants."
            )
            raise typer.Exit(code=1)

    # Audit events should record the install subject (source.type) the
    # firewall actually keys trust by. The pre-RFC implementation
    # hardcoded ``plugin_home`` here, which mis-labelled local_path
    # plugins in the audit trail. Source it from the manifest (or the
    # lockfile entry as a fallback), not a hardcoded literal.
    event_source_type = manifest.source.type or entry.source_type or "plugin_home"

    # Trust is bound to the install subject recorded in the lockfile.
    # Re-trusting also clears the disable record (per the RFC: "Re-enabling
    # is performed by re-running ooo plugin trust …").
    #
    # Ordering matters for transactional integrity: the disable record
    # is cleared LAST, after every fallible step (audit-log open and
    # each grant write) has succeeded. The previous order cleared
    # disable up front, so a later grant or audit-log failure left the
    # plugin re-enabled with a partial / missing grant set — a real
    # state-corruption path at the trust boundary. With the new
    # order, any failure leaves the plugin still disabled and the
    # user can simply re-run after fixing the cause.
    was_disabled = trust.is_disabled(name)

    audit_handle = audit_log_path.open("a", encoding="utf-8") if audit_log_path else None
    try:
        for scope in scopes:
            record = trust.grant(
                plugin=name,
                version=entry.version,
                scope=scope,
                granted_by=granted_by,
                source_type=entry.source_type,
                source_identity=entry.source_identity,
                artifact_digest=entry.artifact_digest,
            )
            print_success(f"Granted: {scope} ({len(record.granted_scopes)} total scope(s))")

            # Emit plugin.trusted via the ledger adapter shape.
            audit_event = {
                "schema_version": "0.1",
                "event_type": "plugin.trusted",
                "occurred_at": datetime.datetime.now(tz=datetime.UTC).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "plugin": {
                    "name": name,
                    "version": entry.version,
                    "source_type": event_source_type,
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

    # Clear the disable record only after all fallible writes above
    # have succeeded. ``clear_disable`` is idempotent and the
    # *last* state-changing step the command makes.
    trust.clear_disable(name)
    if not scopes and was_disabled:
        # Bare `ooo plugin trust <zero-perm-plugin>` (or all-optional)
        # against a disabled subject — the only effective change is
        # the just-cleared disable record. Surface that explicitly
        # so the user sees something happened.
        print_success(
            f"Re-enabled {name} ({manifest.version}) "
            "(no scopes to grant — manifest declares no required permissions)"
        )


@app.command("disable")
def disable_command(
    name: Annotated[str, typer.Argument(help="Installed plugin name.")],
    lockfile_path: Annotated[
        Path | None,
        typer.Option("--lockfile"),
    ] = None,
    trust_root: Annotated[
        Path | None,
        typer.Option(
            "--trust-root",
            help="Override the trust root (default: ~/.ouroboros/trust). "
            "Required when the plugin was trusted under a non-default root.",
        ),
    ] = None,
) -> None:
    """Disable an installed plugin.

    Per the locked RFC ("Disable records"), `disable` writes a record
    keyed by ``(name, source.type, source_identity)`` (no
    ``artifact_digest``) so the disable signal survives every digest
    change, including upgrades. The trust file is wiped at the same
    time. The lockfile entry remains so the user can re-enable with
    ``ooo plugin trust …``, which is the re-enable path.
    """
    lock = Lockfile(lockfile_path or DEFAULT_LOCKFILE_PATH)
    entries = lock.read()
    if name not in entries:
        print_error(f"{name!r} is not installed")
        raise typer.Exit(code=1)
    entry = entries[name]
    # Honor the explicit `--trust-root` override so that grants made
    # under a non-default root are actually removed (not silently left
    # behind).
    trust = TrustStore(root=trust_root or DEFAULT_TRUST_ROOT)
    trust.write_disable(
        name,
        source_type=entry.source_type
        or ("plugin_home" if entry.source_kind == "git" else "local_path"),
        source_identity=entry.source_identity or (entry.repository or entry.plugin_home),
    )
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

    # Prefer the explicit `--plugin-home-root` override when provided so
    # that callers (notably tests) can target a non-default install
    # location even if the lockfile points elsewhere.
    if plugin_home_root is not None:
        plugin_home = plugin_home_root.expanduser() / name
    else:
        plugin_home = Path(entry.plugin_home).expanduser()
    # Order matters: mutate the durable bookkeeping state (lockfile +
    # trust + disable records) BEFORE touching the on-disk plugin
    # bytes. The lockfile is the source of truth for "is this plugin
    # installed?", so once `lock.remove()` succeeds the firewall
    # treats the plugin as gone — which means leftover bytes (if the
    # subsequent `rmtree` were to fail) cannot be invoked. The
    # opposite order ran the bytes-removal first, leaving a window
    # where a `wipe_subject` / `lock.remove` failure produced a
    # split-brain: bytes gone but lockfile still claimed installed.
    #
    # `wipe_subject` removes both the trust file and the disable
    # record (per the RFC: "remove ALSO deletes any disable record
    # for the plugin's install subject").
    trust.wipe_subject(name)
    lock.remove(name)

    plugin_home_status = "plugin home"
    if plugin_home.is_dir():
        try:
            shutil.rmtree(plugin_home)
        except OSError as exc:
            # Bookkeeping state is already consistent (lockfile +
            # trust both say uninstalled), so the plugin cannot be
            # invoked. Surface the cleanup failure so the user can
            # remove the leftover directory manually, but don't fail
            # the command.
            plugin_home_status = (
                f"plugin home (BYTES NOT REMOVED: {plugin_home} — "
                f"{type(exc).__name__}: {exc}; remove manually)"
            )

    print_success(
        f"Removed {name} (lockfile entry + trust file + disable record + {plugin_home_status})"
    )


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
