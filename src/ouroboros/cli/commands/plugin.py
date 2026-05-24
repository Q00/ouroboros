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

from contextlib import contextmanager
from dataclasses import dataclass
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
    Permission,
    PluginManifest,
    PluginManifestError,
    load_manifest,
)
from ouroboros.plugin.trust_store import DEFAULT_TRUST_ROOT, TrustStore
from ouroboros.plugin.userlevel_registry import RegistryError, UserLevelProgramRegistry

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
    """Load a manifest, printing a nicely-formatted error on failure.

    ``load_manifest`` already wraps OS-level errors (permission denied,
    transient I/O, broken symlink, etc.) into ``PluginManifestError``
    with a message starting with ``"manifest is unreadable: ..."``.
    Detect that shape and surface it as a distinct top-level header so
    operators reading ``discover`` output can distinguish "the file is
    structurally invalid" from "the file could not be read at all" —
    the remediation is different.
    """
    path = _resolve_manifest_path(target)
    try:
        return load_manifest(path)
    except PluginManifestError as exc:
        loc = exc.json_pointer if exc.json_pointer else "(root)"
        message = exc.args[0] if exc.args else ""
        if message.startswith("manifest is unreadable"):
            print_error(f"manifest is unreadable:\n  path: {exc.path}\n  reason: {exc.got}")
        else:
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

    Empty fields on the record are tolerated only when the lockfile entry
    has no plumbed subject fields (legacy / pre-RFC callers). Once the
    entry carries a subject, any blank record subject column means the
    record cannot prove it applies to this install.
    """
    if record is None:
        return False
    if record.version != manifest.version:
        return False
    if entry.source_identity or entry.artifact_digest:
        if not record.source_type or not record.source_identity or not record.artifact_digest:
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
    # Legacy/unbound records cannot be reported as "trusted" under the
    # post-RFC subject contract: the firewall refuses them once the
    # dispatcher plumbs the install subject (see
    # ``firewall._record_matches_subject``). Mirror that here so
    # ``inspect`` / ``list`` agree with the actual enforcement path —
    # otherwise operators see "trusted" while invocation is in fact
    # blocked. The check only fires when the caller plumbs an expected
    # subject column (production CLI), so legacy CLI tests that pass
    # ``expected_*=None`` keep their version-only behavior.
    if expected_source_identity is not None or expected_artifact_digest is not None:
        if not record.source_type or not record.source_identity or not record.artifact_digest:
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

    See ``_atomic_install_with_rollback`` for the install-time variant
    that DOES NOT delete the backup until the caller's follow-up work
    (typically the lockfile write) has also succeeded — preventing
    split-brain when the post-replace lockfile commit fails.

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


@contextmanager
def _atomic_install_with_rollback(src: Path, dest: Path):
    """Atomic ``src``→``dest`` replace whose backup survives the caller block.

    The plain ``_atomic_replace_dir`` is fine for read-only setups, but
    the install pipeline has a transactional dependency: after the new
    bytes are in ``dest``, the caller still needs to add a lockfile
    entry. If that lockfile write fails (corrupt file, EROFS, EDQUOT,
    etc.), the previous helper had already deleted the backup —
    leaving a split-brain state where ``dest`` holds the new bytes but
    no lockfile entry references them, and the prior install is
    irrecoverable.

    Used as a context manager: the caller's follow-up work runs INSIDE
    the ``with`` block. If the block raises, we restore the backup
    over ``dest`` and re-raise. If it returns cleanly, we drop the
    backup. ``dest`` carries the new bytes during the block (matching
    the previous behavior so digest verification in the follow-up
    sees the post-replace tree).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    suffix = secrets.token_hex(6)
    staging = dest.with_name(f"{dest.name}.staging-{suffix}")
    backup = dest.with_name(f"{dest.name}.bak-{suffix}")

    try:
        shutil.copytree(src, staging, symlinks=True)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise

    backup_used = False
    try:
        if dest.exists():
            os.rename(dest, backup)
            backup_used = True
        os.rename(staging, dest)
    except Exception:
        if backup_used and not dest.exists() and backup.exists():
            try:
                os.rename(backup, dest)
            except OSError:
                pass
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise

    try:
        yield
    except Exception:
        # Caller's follow-up failed (typically a lockfile write).
        # Roll back to the prior install: drop the new bytes, restore
        # the backup. The original exception propagates so the caller
        # surfaces the friendly recovery hint.
        #
        # Ordering matters: if ``os.rename(backup, dest)`` itself
        # raises (EXDEV across filesystems, an unexpected ``dest``
        # re-appearance, etc.), ``backup`` becomes the *only*
        # remaining copy of the user's prior install. The previous
        # implementation deleted ``backup`` unconditionally in a
        # ``finally`` clause — meaning a failed restore destroyed
        # both the new tree and the backup, the exact data-loss path
        # the rollback is here to prevent. Now ``backup`` is only
        # cleaned up implicitly via the successful ``os.rename`` (a
        # rename consumes the source); a failed rename leaves the
        # backup on disk at its full path so the operator can
        # recover from ``<dest>.bak-<suffix>``.
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        if backup_used and backup.exists():
            try:
                os.rename(backup, dest)
            except OSError:
                # Restore failed; backup preserved for manual recovery.
                # The original exception still propagates below.
                pass
        raise

    # Caller's follow-up succeeded — drop the backup.
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)


def _register_catalog_or_warn(
    catalog_state: CatalogRegistry,
    *,
    source_type: str,
    source_identity: str,
    plugin_name: str,
) -> None:
    """Register the (source, plugin) pair in the catalog, warning on failure.

    Catalog registration runs AFTER ``_install_one`` has committed the
    plugin home + lockfile entry. The catalog is a resolution-cache
    convenience used by ``ooo plugin install <name>`` (no ``--from``)
    — losing the entry does NOT make the plugin uninvokable, just
    requires the user to re-pass ``--from`` until the catalog is
    repaired. So a write failure here is a warning, not a hard
    error: hard-failing AFTER the install was committed would
    contradict the just-printed ``Installed: ...`` and turn a
    successful install into a partial-commit error path.
    """
    try:
        catalog_state.register(
            source_type=source_type,
            source_identity=source_identity,
            plugin_name=plugin_name,
        )
    except (ValueError, OSError) as exc:
        console.print(
            f"  [yellow]warning[/]: install of {plugin_name!r} succeeded but "
            f"catalog registration failed ({exc}); future "
            f"`ooo plugin install {plugin_name}` will need an explicit "
            f"`--from` flag until {catalog_state.state_path} is repaired."
        )


def _read_lock_or_exit(lock: Lockfile) -> dict:
    """Read the lockfile, surfacing parse/IO failures as a controlled exit.

    Mirrors the wrapper used by ``inspect``/``list``: a malformed
    ``plugins.lock`` MUST produce a one-line recovery hint, not a raw
    traceback in the very commands operators use to repair plugin
    state. Returns the lockfile's name → ``LockEntry`` map on success.
    """
    try:
        return lock.read()
    except (ValueError, OSError) as exc:
        print_error(
            f"lockfile is unreadable ({lock.path}): {exc}. "
            f"Inspect or replace the file, or pass --lockfile to point "
            f"at a known-good copy."
        )
        raise typer.Exit(code=1) from exc


def _safe_read_trust(trust: TrustStore, name: str) -> tuple[object | None, str | None]:
    """Read a trust record, returning ``(record, error_message)``.

    The trust file is operator-editable and can drift into a corrupt
    state independently of the lockfile / plugin bytes. ``trust.read``
    raises ``ValueError`` on schema/JSON failures and ``OSError`` on
    filesystem failures; both must NOT surface as raw tracebacks in
    state-mutating commands. Returning the failure as a string lets
    callers decide whether to abort (e.g. ``trust``: refuse to grant
    while the file is unreadable) or continue with a controlled
    warning (e.g. post-install trust invalidation: lockfile is
    already updated, so a "couldn't reset trust automatically" hint
    plus a recovery instruction is the right shape).
    """
    try:
        return trust.read(name), None
    except (ValueError, OSError) as exc:
        return None, str(exc)


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

    A corrupt ``trust.json`` MUST NOT abort the install with a raw
    traceback at this late stage. The lockfile + plugin home are
    already updated; the right shape is a controlled warning that
    names the recovery action (``ooo plugin trust``) and lets the
    install command exit cleanly. The firewall already fails closed
    on subject mismatch as defense-in-depth, so leaving a
    not-yet-reset trust file in place keeps the plugin gated until
    the user re-grants — same end-state the auto-reset would have
    produced.
    """
    record, err = _safe_read_trust(trust, name)
    if err is not None:
        console.print(
            f"  [yellow]warning[/]: trust file for {name!r} is unreadable "
            f"({err}); auto-reset skipped. Re-grant scopes via "
            f"`ooo plugin trust {name} --scope <...>` after fixing the file. "
            f"The firewall blocks invocation until trust is re-granted."
        )
        return
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
    try:
        trust.reset_for_subject_change(
            name,
            new_version=new_version,
            new_source_type=new_source_type,
            new_source_identity=new_source_identity,
            new_artifact_digest=new_artifact_digest,
        )
    except (ValueError, OSError) as exc:
        console.print(
            f"  [yellow]warning[/]: could not reset trust for {name!r} after "
            f"subject change ({exc}); re-grant scopes via "
            f"`ooo plugin trust {name} --scope <...>`. The firewall blocks "
            f"invocation until trust is re-granted."
        )


# Retained for callers that only know the version (no install-subject
# context). New code should prefer the subject-aware variant.
def _maybe_invalidate_trust_for_version_bump(
    *,
    name: str,
    new_version: str,
    trust: TrustStore,
) -> None:
    record, err = _safe_read_trust(trust, name)
    if err is not None:
        console.print(
            f"  [yellow]warning[/]: trust file for {name!r} is unreadable "
            f"({err}); auto-reset skipped. Re-grant scopes via "
            f"`ooo plugin trust {name} --scope <...>`."
        )
        return
    if record is None or record.version == new_version:
        return
    try:
        trust.reset_for_version_bump(name, new_version)
    except (ValueError, OSError) as exc:
        console.print(
            f"  [yellow]warning[/]: could not reset trust for {name!r} after "
            f"version bump ({exc}); re-grant scopes via "
            f"`ooo plugin trust {name} --scope <...>`."
        )


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
        # Validate inner shape too. ``register()`` and
        # ``find_sources_for()`` immediately assume ``catalogs`` is a
        # list of dicts; a parseable-but-structurally-corrupt file
        # like ``{"catalogs": 1}`` would otherwise pass the outer
        # check and crash with TypeError/AttributeError downstream.
        # Surface inner-shape mismatches with the same ``ValueError``
        # shape so the CLI's friendly-error wrapper catches them too.
        catalogs = payload.get("catalogs", [])
        if not isinstance(catalogs, list):
            raise ValueError(
                f"plugin catalog state at {self.state_path} has a non-list "
                f"'catalogs' field (got {type(catalogs).__name__})"
            )
        for index, entry in enumerate(catalogs):
            if not isinstance(entry, dict):
                raise ValueError(
                    f"plugin catalog state at {self.state_path}: catalogs[{index}] "
                    f"is not a JSON object (got {type(entry).__name__})"
                )
            # ``register()`` does ``set(entry.get("plugins", []))`` and
            # ``find_sources_for()`` does ``plugin_name in entry.get("plugins", [])``.
            # If ``plugins`` is anything but a list of strings (e.g.
            # ``"plugins": 1`` or ``"plugins": {"a": 1}``), both paths
            # crash with raw ``TypeError`` / ``AttributeError``. Surface
            # the corruption with the same typed ``ValueError`` shape so
            # the CLI's friendly-error wrapper catches it too.
            if "plugins" in entry:
                plugins_field = entry["plugins"]
                if not isinstance(plugins_field, list):
                    raise ValueError(
                        f"plugin catalog state at {self.state_path}: "
                        f"catalogs[{index}].plugins is not a list "
                        f"(got {type(plugins_field).__name__})"
                    )
                for plugin_index, name in enumerate(plugins_field):
                    if not isinstance(name, str):
                        raise ValueError(
                            f"plugin catalog state at {self.state_path}: "
                            f"catalogs[{index}].plugins[{plugin_index}] is not a string "
                            f"(got {type(name).__name__})"
                        )
            # ``ooo plugin install <name>`` does ``s["source_type"]`` and
            # ``s["source_identity"]`` (see ``install_command`` resolve
            # block) and ``find_sources_for()`` returns these dicts as
            # ``sources`` directly. A catalog file that omits either
            # field — or supplies a non-string — would pass the outer
            # JSON-object check and crash later with ``KeyError`` /
            # ``TypeError`` instead of the friendly recovery hint this
            # path is supposed to provide.
            for required_field in ("source_type", "source_identity"):
                if required_field not in entry:
                    raise ValueError(
                        f"plugin catalog state at {self.state_path}: "
                        f"catalogs[{index}] is missing required field "
                        f"{required_field!r}"
                    )
                value = entry[required_field]
                if not isinstance(value, str):
                    raise ValueError(
                        f"plugin catalog state at {self.state_path}: "
                        f"catalogs[{index}].{required_field} is not a string "
                        f"(got {type(value).__name__})"
                    )
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

    @contextmanager
    def _file_lock(self):
        """Acquire an exclusive flock for concurrent-write safety.

        Mirrors the POSIX flock pattern used by ``Lockfile`` and
        ``TrustStore`` so two concurrent ``ooo plugin add`` /
        ``ooo plugin install <name> --from ...`` calls cannot
        clobber each other's catalog entries via a lost-update
        race. The default ``ooo plugin install <name>`` resolution
        path now depends on ``plugin-catalogs.json``, so persisted
        catalog state must offer the same durability guarantee as
        the lockfile and trust store.

        Falls through gracefully on platforms without ``fcntl``
        (the file is still atomically replaced via ``os.replace``,
        which gives last-writer-wins semantics — acceptable for
        non-concurrent single-user setups, the only case where
        ``fcntl`` is unavailable).
        """
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl
        except ImportError:  # pragma: no cover — non-POSIX platforms
            yield
            return
        lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        with lock_path.open("w") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def register(
        self,
        *,
        source_type: str,
        source_identity: str,
        plugin_name: str,
    ) -> None:
        """Idempotently record a (source, plugin) pair.

        Read-modify-write is wrapped in an exclusive flock so two
        concurrent ``ooo plugin add`` / ``ooo plugin install`` calls
        cannot lose updates. Without the lock both processes would
        ``_load()`` the same prior payload, each merge in their own
        plugin, and the last ``os.replace()`` would win — silently
        dropping the other's entry from the catalog after both
        commands had already reported success.
        """
        with self._file_lock():
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
        # First-party plugins ship inside the binary and the firewall
        # explicitly bypasses trust for them; telling operators that
        # required scopes "must be trusted before invocation" for
        # first-party plugins would directly contradict the gate.
        # Surface the declarations, but be honest that they are
        # advisory.
        if manifest.source.type == "first_party":
            console.print(
                "  declared required scopes (advisory; first-party plugins bypass the trust gate):"
            )
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
    # First-party programs bypass the user-facing trust flow at the
    # firewall (RFC: "First-party trust semantics") — their required
    # permissions are implicitly trusted at boot. Reflect that in the
    # `inspect` output so it agrees with `list` / firewall events:
    # "missing scopes" is not meaningful for them and would falsely
    # imply invocation will be blocked.
    is_first_party = manifest.source.type == "first_party"
    if is_first_party:
        granted = [p.scope for p in manifest.permissions if p.required]
        applies = True
    else:
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
        # Surface why the grants don't apply, naming the field that
        # drifted. The version-bump case gets its own labelled line
        # (rather than a generic "trust note") so consumers reading
        # the output have a stable phrase to grep for — a stale trust
        # file after a version bump is by far the most common shape
        # of drift in practice and the one whose remediation is the
        # most concrete (re-run `ooo plugin trust`).
        if record.version != manifest.version:
            console.print(
                f"  trust_version:  version bump invalidated trust "
                f"(record={record.version!r}, installed={manifest.version!r}); "
                f"re-grant required"
            )
        else:
            reason = _subject_drift_reason(record, manifest=manifest, entry=entry)
            console.print(
                f"  trust note:     stored grants are stale ({reason}); re-grant required"
            )
    if not is_first_party:
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
        except (PluginManifestError, OSError):
            # Manifest unreadable post-install (external mutation,
            # missing file, permission error). Without the manifest we
            # cannot prove ``trusted``; report the row as safely
            # degraded so the operator can still see every other
            # plugin's state. Don't read ``trust.json`` either — a
            # corrupt trust file on top of an unreadable manifest
            # would only stack a second crash path onto the very row
            # the operator is trying to diagnose.
            rows.append(
                {
                    "name": entry.name,
                    "version": entry.version,
                    "source_kind": entry.source_kind,
                    "trust_state": "installed",
                    "granted_scopes": [],
                    "missing_required_scopes": [],
                    "trust_version_stale": False,
                    "trust_read_error": None,
                }
            )
            continue
        # First-party programs are implicitly trusted by the firewall
        # (RFC: "First-party trust semantics"), so skip the trust read
        # entirely. Use the ``first_party`` label so machine-readable
        # state agrees across ``list``, ``inspect``, ``_describe_trust_state``,
        # and the firewall audit events — they all use the same string
        # for this case. Per the upstream contract, first-party rows
        # carry ``missing_required_scopes: []`` rather than the
        # manifest's declared set: the firewall treats first-party
        # plugins as already trusted, so nothing is "missing" from the
        # consumer's view.
        if manifest.source.type == "first_party":
            # First-party plugins are implicitly trusted; the firewall
            # bypasses the trust store entirely. ``granted_scopes`` and
            # ``missing_required_scopes`` are both empty because no
            # explicit grants are persisted and the firewall treats
            # nothing as "missing". The required-scope set is part of
            # the manifest, not the trust grant, and ``inspect`` /
            # ``discover`` are the right surfaces for surfacing it.
            rows.append(
                {
                    "name": entry.name,
                    "version": entry.version,
                    "source_kind": entry.source_kind,
                    "trust_state": "first_party",
                    "granted_scopes": [],
                    "missing_required_scopes": [],
                    "trust_version_stale": False,
                    "trust_read_error": None,
                }
            )
            continue
        # For non-first-party entries, treat malformed trust state as
        # a degraded row rather than crashing the listing. ``inspect``
        # is the right command for surfacing exactly which file is
        # corrupt; ``list`` must keep working so the operator can see
        # what else is installed.
        trust_read_error: str | None = None
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
        except (ValueError, OSError) as exc:
            # Operators using ``--json`` need to distinguish "no trust
            # grant yet" from "trust file is malformed". Capture the
            # raised reason so the row carries a deterministic
            # diagnostic signal rather than collapsing onto the
            # untrusted state. The ``trust_state == "trust_unreadable"``
            # label already exists; ``trust_read_error`` complements it
            # with the underlying message.
            record = None
            trust_state = "trust_unreadable"
            scopes = []
            trust_read_error = str(exc)
        # ``trust_version_stale`` mirrors the firewall's "the recorded
        # grant is bound to a different installed version" predicate.
        # ``_record_applies_to_subject`` already collapses this into
        # the scope list (empty when the record doesn't apply), but
        # JSON consumers want a deterministic boolean rather than
        # having to compare versions themselves, so surface it
        # explicitly.
        stale_version = bool(record is not None and record.version != manifest.version)
        # ``missing_required_scopes`` describes the deficit between the
        # manifest's required scopes and the cumulative grant set the
        # firewall would honor (i.e. when the record actually applies
        # to the install subject). When the trust file is unreadable
        # or no record exists, every required scope is missing.
        required = [p.scope for p in manifest.permissions if p.required]
        granted_set = set(scopes)
        missing = [s for s in required if s not in granted_set]
        rows.append(
            {
                "name": entry.name,
                "version": entry.version,
                "source_kind": entry.source_kind,
                "trust_state": trust_state,
                "granted_scopes": scopes,
                "missing_required_scopes": missing,
                "trust_version_stale": stale_version,
                "trust_read_error": trust_read_error,
            }
        )

    if json_output:
        # Plain stdout (no Rich highlighting) so consumers can pipe to jq.
        typer.echo(json.dumps(rows, indent=2))
        return

    # Operator-facing diagnostic: when any row's trust file failed to
    # parse, the table column flips to ``trust_unreadable`` but does NOT
    # carry the underlying parser message. Surface it on stderr so the
    # operator sees both (a) which plugin's trust file is broken and
    # (b) the literal recovery action, without polluting stdout (which
    # remains the structured table for ``less`` / ``column`` /
    # screenshotting). The ``--json`` path already exposes the same
    # information via the ``trust_read_error`` field, so we only emit
    # the stderr warning on the human surface. ``typer.echo(..., err=True)``
    # resolves ``sys.stderr`` at call time, which is what
    # ``CliRunner(mix_stderr=False)`` needs to capture the warning under
    # test.
    unreadable = [row for row in rows if row.get("trust_read_error") is not None]
    for row in unreadable:
        typer.echo(
            f"warning: trust file for {row['name']!r} is unreadable "
            f"({row['trust_read_error']}); the row is shown with "
            f"trust_state=trust_unreadable and empty scopes. Inspect or "
            f"replace the file, then re-grant scopes via "
            f"`ooo plugin trust {row['name']} --scope <...>`.",
            err=True,
        )

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


@dataclass(frozen=True)
class CatalogEntry:
    """One discovered plugin in a repo's ``plugins/`` directory.

    Pairs the on-disk directory the manifest was loaded from with the
    parsed manifest. The directory is preserved so install paths copy
    bytes from the EXACT subtree that produced the validated manifest,
    even when ``manifest.name`` differs from the directory name (e.g.
    a vendored sibling whose folder is ``foo`` but whose manifest
    declares ``name: bar``). Reconstructing the source path from
    ``manifest.name`` would silently install bytes from
    ``plugins/bar`` instead — breaking the manifest-to-artifact
    binding the trust subject depends on.
    """

    plugin_dir: Path
    manifest: PluginManifest


def _enumerate_catalog(repo_root: Path) -> list[CatalogEntry]:
    """Read every `plugins/<name>/ouroboros.plugin.json` from a checked-out repo.

    Invalid sibling manifests must NOT block installing valid plugins from
    a mixed-quality repo. Each parse error is surfaced as a yellow `skip:`
    warning so the user sees what was bypassed; the function only fails if
    nothing at all parsed.

    Returns ``CatalogEntry`` records that pair the on-disk directory
    with its parsed manifest. Callers MUST copy bytes from
    ``entry.plugin_dir`` (not from ``plugins/<manifest.name>``) so a
    folder-name vs. manifest-name mismatch can never desynchronize the
    validated manifest from the installed bytes.
    """
    plugins_dir = repo_root / "plugins"
    if not plugins_dir.is_dir():
        print_error(f"no `plugins/` directory in {repo_root}")
        raise typer.Exit(code=1)
    entries: list[CatalogEntry] = []
    skipped: list[tuple[str, str]] = []
    for entry in sorted(plugins_dir.iterdir()):
        manifest_path = entry / "ouroboros.plugin.json"
        if not manifest_path.is_file():
            continue
        try:
            candidate = load_manifest(manifest_path)
        except PluginManifestError as exc:
            loc = exc.json_pointer or "(root)"
            msg = exc.args[0] if exc.args else "invalid manifest"
            skipped.append((entry.name, f"{loc}: {msg}"))
            continue
        # Skip ``first_party`` manifests up-front so they neither appear
        # in the interactive multi-select nor flow into ``_install_one``.
        # The trust-bypass semantic the firewall grants ``first_party``
        # is reserved for plugins bundled with ouroboros itself; an
        # external repo claiming that type would let a malicious
        # manifest skip the user-facing trust flow entirely.
        if candidate.source.type == "first_party":
            skipped.append(
                (
                    entry.name,
                    'manifest declares `source.type = "first_party"` (reserved '
                    "for plugins bundled with ouroboros; not installable from "
                    "an external source)",
                )
            )
            continue
        entries.append(CatalogEntry(plugin_dir=entry, manifest=candidate))
    for dir_name, reason in skipped:
        console.print(f"  [yellow]skip[/]: {dir_name}: invalid manifest ({reason})")
    if not entries:
        print_error(f"no valid manifests found under {plugins_dir}")
        raise typer.Exit(code=1)
    return entries


def _select_plugins(
    catalog: list[CatalogEntry],
    requested: list[str] | None,
) -> list[CatalogEntry]:
    """Return catalog entries matching `requested`, or prompt interactively.

    A repository may legitimately host multiple subdirectories whose
    manifests declare the same ``name`` (a refactor in flight, a
    reorganized monorepo, an accidentally-duplicated subtree). Silently
    collapsing those into a single ``name -> entry`` dict would let
    ``ooo plugin add ... --plugin <name>`` install whichever entry
    happened to win the dict overwrite — a wrong-artifact install with
    no ambiguity error. Detect duplicates before any selection runs and
    refuse with a friendly hint that lists the conflicting paths.
    """
    seen: dict[str, list[CatalogEntry]] = {}
    for entry in catalog:
        seen.setdefault(entry.manifest.name, []).append(entry)
    duplicates = {n: ents for n, ents in seen.items() if len(ents) > 1}
    if duplicates:
        # Stable, alphabetised diagnostic so reruns produce the same
        # error text — operators script remediation against this.
        details = []
        for name in sorted(duplicates):
            paths = sorted(str(e.plugin_dir) for e in duplicates[name])
            details.append(f"  {name!r} declared at: {paths}")
        joined = "\n".join(details)
        print_error(
            "catalog has plugins declaring duplicate `name` fields; refusing "
            "to install because the dispatcher would silently pick one "
            "subdirectory and shadow the others:\n"
            f"{joined}\n"
            "Resolve by renaming the duplicate manifests or removing the "
            "stale subtree, then re-run."
        )
        raise typer.Exit(code=1)
    by_name = {entry.manifest.name: entry for entry in catalog}

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
            title=(
                f"{entry.manifest.name:<25} {entry.manifest.version}  "
                f"{entry.manifest.description or ''}"
            ),
            value=entry.manifest.name,
        )
        for entry in catalog
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


def _refuse_first_party_external_install(manifest: PluginManifest) -> None:
    """Reject an external install that claims ``source.type == "first_party"``.

    The firewall skips the trust gate for ``first_party`` manifests
    (`src/ouroboros/plugin/firewall.py`, "First-party trust semantics" —
    bundled programs are implicitly trusted at boot). That semantic only
    holds for plugins shipped with ouroboros itself; allowing arbitrary
    user-installed plugins to declare the same ``source.type`` would let
    a malicious manifest fetched from a repo or local path bypass the
    user-facing trust flow entirely. The CLI install paths
    (``ooo plugin add`` / ``install``) are external installs by
    definition, so a ``first_party`` claim here is always invalid.
    """
    if manifest.source.type == "first_party":
        print_error(
            f"manifest for {manifest.name!r} declares "
            f'`source.type = "first_party"`, which is reserved for plugins '
            f"bundled with ouroboros itself. External installs (`ooo plugin "
            f"add` / `install`) cannot grant first-party trust semantics; "
            f"the manifest must declare `source.type` as `local_path` or "
            f"`plugin_home` to be installable."
        )
        raise typer.Exit(code=1)


def _refuse_userlevel_collision(manifest: PluginManifest, lock: Lockfile) -> None:
    """Reject installs that would collide with an already-installed plugin's
    namespace, plugin name, or command names.

    ``plugin_dispatch._build_registry_from_lockfile()`` builds the
    runtime ``UserLevelProgramRegistry`` from the lockfile and silently
    ``continue``s past entries whose ``register()`` raises
    ``RegistryError`` for a cross-plugin collision (per the locked
    "first registration wins" rule). Without an install-time guard,
    a colliding manifest gets persisted to the lockfile but is
    perpetually unreachable at runtime — the install reports success
    and ``ooo <name>`` reports "no such command", an unrecoverable
    contract gap.

    Mirror the runtime registry's resolution: build a registry from
    every already-installed manifest (skipping any whose on-disk
    manifest is corrupt — dispatch surfaces that separately) and try
    a ``register(... replace=True)`` of the new manifest. The replace
    flag means a same-name reinstall is exempt from collision (its
    prior namespace/command slots are released first), so this guard
    only fires on genuine cross-plugin collisions. Surface the
    failure BEFORE ``lock.add()`` so a rejected install never leaves
    a half-applied lockfile entry.
    """
    registry = UserLevelProgramRegistry()
    for entry in lock.read().values():
        if entry.name == manifest.name:
            # The plugin we are (re)installing — its own slots are
            # released by ``replace=True`` below, so it does not
            # contribute a self-collision.
            continue
        manifest_path = Path(entry.plugin_home).expanduser() / "ouroboros.plugin.json"
        try:
            existing_manifest = load_manifest(manifest_path)
        except (PluginManifestError, OSError):
            # Corrupt or missing on-disk manifest for an installed
            # plugin: dispatch surfaces this separately as a
            # registration skip. Don't let it block unrelated installs.
            continue
        try:
            registry.register(existing_manifest, replace=True)
        except RegistryError:
            # Existing-vs-existing collision (e.g., from a prior
            # buggy install). Not this install's problem to surface
            # here — keep walking so the new manifest's own collisions
            # still get caught against whichever entries did register.
            continue
    try:
        registry.register(manifest, replace=True)
    except RegistryError as exc:
        print_error(
            f"refusing to install {manifest.name!r}: {exc}. "
            f"Pick a different name/namespace/command, or remove the "
            f"colliding plugin first (`ooo plugin remove <name>`)."
        )
        raise typer.Exit(code=1) from exc


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
    reserved top-level command, OR if the manifest claims
    ``source.type == "first_party"`` (privilege escalation guard — see
    ``_refuse_first_party_external_install``), OR if the manifest's
    namespace / plugin name / command names would collide with an
    already-installed plugin (UserLevel registry guard — see
    ``_refuse_userlevel_collision``). All three checks happen BEFORE
    the lockfile is touched so a rejected install never produces a
    half-applied state.
    """
    _refuse_reserved_name(manifest.name)
    # Defense in depth: every external install path also calls the
    # first-party guard right after manifest load (so the rejection
    # happens before we touch the filesystem). Re-checking here means a
    # future caller that forgets the early guard cannot persist a
    # ``first_party`` lockfile entry — the firewall's trust-bypass
    # semantic stays bound to genuinely-bundled programs.
    _refuse_first_party_external_install(manifest)
    # Cross-plugin namespace/command collision guard: aligns the install
    # contract with the runtime dispatch registry so a "successful"
    # install can never be silently unreachable.
    _refuse_userlevel_collision(manifest, lock)
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


def _missing_required_permissions(
    *,
    manifest: PluginManifest,
    entry: LockEntry,
    trust: TrustStore,
) -> list[Permission]:
    required = [p for p in manifest.permissions if p.required]
    if not required:
        return []

    record = trust.read(manifest.name)
    granted = (
        {g.scope for g in record.granted_scopes}
        if record is not None
        and record.matches_subject(
            version=manifest.version,
            source_type=entry.source_type,
            source_identity=entry.source_identity,
            artifact_digest=entry.artifact_digest,
        )
        else set()
    )
    return [p for p in required if p.scope not in granted]


def _print_required_permissions(permissions: list[Permission]) -> None:
    console.print("")
    console.print("Required permissions:")
    width = max(len(p.scope) for p in permissions)
    for permission in permissions:
        reason = permission.reason.strip() if permission.reason else permission.risk
        console.print(f"  {permission.scope:<{width}}  {reason}")


def _maybe_prompt_grant_required_permissions(
    *,
    manifest: PluginManifest,
    entry: LockEntry,
    trust: TrustStore,
) -> None:
    try:
        missing = _missing_required_permissions(manifest=manifest, entry=entry, trust=trust)
    except (ValueError, OSError) as exc:
        console.print(
            f"  [yellow]warning[/]: trust file for {manifest.name!r} is unreadable "
            f"({exc}); required-permission prompt skipped. Re-grant scopes via "
            f"`ooo plugin trust {manifest.name} --scope <...>` after fixing the file. "
            f"The firewall blocks invocation until trust is re-granted."
        )
        return
    if not missing:
        return

    _print_required_permissions(missing)
    destructive = [p.scope for p in missing if p.risk == "destructive"]
    if destructive:
        console.print(
            "  destructive scopes require an explicit trust command: "
            f"`ooo plugin trust {manifest.name} --scope <scope>`",
            markup=False,
            highlight=False,
        )
        return

    try:
        approved = typer.confirm("Grant required permissions now?", default=False, abort=False)
    except (EOFError, typer.Abort):
        approved = False
    if not approved:
        console.print(
            f"  run `ooo plugin trust {manifest.name} --scope <scope>` later",
            markup=False,
            highlight=False,
        )
        return

    scopes = [p.scope for p in missing]
    try:
        was_disabled = trust.is_disabled(manifest.name)
        record = None
        for scope in scopes:
            record = trust.grant(
                plugin=manifest.name,
                version=manifest.version,
                scope=scope,
                granted_by="user:cli",
                source_type=entry.source_type,
                source_identity=entry.source_identity,
                artifact_digest=entry.artifact_digest,
            )
    except (ValueError, OSError) as exc:
        print_error(
            f"could not write trust grant for {manifest.name!r}: {exc}. "
            f"Inspect the trust file at {trust.root / manifest.name}, then "
            f"re-run `ooo plugin trust {manifest.name} --scope <...>`."
        )
        raise typer.Exit(code=1) from exc

    assert record is not None
    console.print(
        f"  granted required scopes: {', '.join(scopes)} "
        f"({len(record.granted_scopes)} total scope(s))"
    )
    if was_disabled:
        console.print(
            "  plugin remains disabled; re-enable explicitly with "
            f"`ooo plugin trust {manifest.name}`",
            markup=False,
            highlight=False,
        )


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

    cache_root = (cache_root or Path.home() / ".ouroboros" / "cache").expanduser().resolve()
    plugin_home_root = (
        (plugin_home_root or Path.home() / ".ouroboros" / "plugins").expanduser().resolve()
    )
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

    # Per the locked RFC ("How sources enter the known catalog"): `add
    # <repo>` makes the repo a known catalog at that moment, regardless
    # of which plugins the user picks (or skips) at the selection
    # prompt. The selection step (``_select_plugins``) can exit with
    # code 0 when the interactive prompt comes back empty/cancelled —
    # so registering AFTER selection would silently fail to publish
    # the repo for that path, breaking later `ooo plugin install <name>`
    # for sibling plugins. Registering here, BEFORE the selection
    # gate, preserves three properties:
    #   - empty / cancelled interactive selections still publish the
    #     repo so a later `install <name>` resolves without re-fetching
    #   - sibling plugins not chosen on this `add` call are addressable
    #     by name on the next `install`
    #   - per-plugin install failures inside the loop don't strand the
    #     rest of the repo from name-only resolution
    # The recorded ``source_identity`` MUST match the lockfile's
    # per-plugin ``source_identity`` so a future `install <name>`
    # resolved through the catalog appears to come from the same source
    # (RFC: trust subject is keyed by ``source_identity``). For URL
    # repos the per-plugin and repo-root identities are the same
    # normalized URL; for local catalogs we record the per-plugin
    # absolute path so catalog and lockfile agree.
    for catalog_entry in catalog:
        catalog_plugin_source_identity = (
            str(catalog_entry.plugin_dir.resolve()) if source_kind == "local" else source_identity
        )
        _register_catalog_or_warn(
            catalog_state,
            source_type=catalog_entry.manifest.source.type,
            source_identity=catalog_plugin_source_identity,
            plugin_name=catalog_entry.manifest.name,
        )

    selected = _select_plugins(catalog, plugin_names)

    # Pre-flight lockfile health check. ``_install_one`` will write
    # to the lockfile after the bytes are swapped into ``plugin_home``;
    # surfacing a corrupt-or-unwritable lockfile here means we abort
    # BEFORE mutating any plugin home, not after. The transactional
    # ``_atomic_install_with_rollback`` below additionally restores
    # the prior install if the post-swap write still fails.
    _read_lock_or_exit(lock)

    installed: list[str] = []
    for entry in selected:
        manifest = entry.manifest
        # Use the plugin's actual on-disk directory (not
        # ``plugins/<manifest.name>``). When folder-name and
        # ``manifest.name`` disagree, the validated manifest still
        # binds to the bytes in ``entry.plugin_dir`` — copying from
        # ``plugins/<manifest.name>`` would silently install bytes
        # from a different subtree.
        source_dir = entry.plugin_dir
        plugin_home = plugin_home_root / manifest.name
        # Per-plugin source_identity for local catalogs.
        if source_kind == "local":
            plugin_source_identity = str(source_dir.resolve())
        else:
            plugin_source_identity = source_identity
        # Per the RFC, the persisted ``source_type`` is the manifest's
        # declared value, not an inference from the install transport.
        # Same plugin can travel through different transports (URL
        # clone vs. local checkout) but the trust subject is keyed by
        # what the manifest says, so the firewall keeps a single
        # consistent identity for it.
        manifest_source_type = manifest.source.type
        # Atomic install with rollback. The whole install transaction
        # — digest computation (which can refuse the source tree),
        # plugin_home swap, and lockfile commit — is wrapped so any
        # failure surfaces a controlled error and the prior install
        # (if any) is restored. ``EscapingSymlinkError`` and
        # ``UnsupportedFileTypeError`` from ``canonical_tree_hash``
        # are ``ValueError`` subclasses, so they are caught here too.
        try:
            # Compute the canonical tree hash of the SOURCE bytes
            # BEFORE mutating ``plugin_home``. content-addressable, so
            # the post-replace tree has the same digest, but a hash
            # failure must abort before the prior install is renamed
            # away — otherwise the user is left in a split-brain
            # state with the old install gone, the new bytes on
            # disk, and no lockfile entry to reflect either.
            artifact_digest = canonical_tree_hash(source_dir)
            with _atomic_install_with_rollback(source_dir, plugin_home):
                installed_entry = _install_one(
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
        except (ValueError, OSError) as exc:
            print_error(
                f"could not commit install for {manifest.name!r}: {exc}. "
                f"The prior install (if any) was restored. Inspect the "
                f"source tree and the lockfile at {lock.path}, then "
                f"re-run the install."
            )
            raise typer.Exit(code=1) from exc
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
        _register_catalog_or_warn(
            catalog_state,
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
        _maybe_prompt_grant_required_permissions(
            manifest=manifest,
            entry=installed_entry,
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
    plugin_home_root = (
        (plugin_home_root or Path.home() / ".ouroboros" / "plugins").expanduser().resolve()
    )
    cache_root = (cache_root or Path.home() / ".ouroboros" / "cache").expanduser().resolve()
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
    # Reject before touching the filesystem.
    _refuse_first_party_external_install(manifest)
    # Pre-flight lockfile health: surface a corrupt lockfile before
    # we mutate plugin_home.
    _read_lock_or_exit(lock)

    plugin_home = plugin_home_root / manifest.name
    source_identity = str(src)
    # Per the RFC ("Trust identity"), the persisted ``source_type`` is
    # the manifest's declared semantic source, not the install
    # transport. The firewall keys subject-match on
    # ``manifest.source.type``, so persisting `"local_path"` for a
    # manifest that declared `plugin_home` would leave the freshly
    # installed plugin permanently stuck in the `installed` state with
    # invocation blocked.
    manifest_source_type = manifest.source.type

    # Atomic install with rollback: digest compute, plugin_home swap,
    # and lockfile commit are wrapped together. Any failure surfaces
    # a controlled error; the prior install is restored.
    try:
        artifact_digest = canonical_tree_hash(src)
        with _atomic_install_with_rollback(src, plugin_home):
            installed_entry = _install_one(
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
    except (ValueError, OSError) as exc:
        print_error(
            f"could not commit install for {manifest.name!r}: {exc}. "
            f"The prior install (if any) was restored. Inspect the "
            f"source tree and the lockfile at {lock.path}, then "
            f"re-run the install."
        )
        raise typer.Exit(code=1) from exc
    _register_catalog_or_warn(
        catalog_state,
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
    _maybe_prompt_grant_required_permissions(
        manifest=manifest,
        entry=installed_entry,
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
    _refuse_first_party_external_install(manifest)
    # Pre-flight lockfile health.
    _read_lock_or_exit(lock)

    plugin_home = plugin_home_root / manifest.name
    source_identity = str(candidate_root.resolve())
    # See `_install_from_local_directory` — persist manifest's source
    # type, not transport.
    manifest_source_type = manifest.source.type
    try:
        # Digest, plugin_home swap, lockfile commit — all wrapped.
        artifact_digest = canonical_tree_hash(candidate_root)
        with _atomic_install_with_rollback(candidate_root, plugin_home):
            installed_entry = _install_one(
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
    except (ValueError, OSError) as exc:
        print_error(
            f"could not commit install for {manifest.name!r}: {exc}. "
            f"The prior install (if any) was restored. Inspect the "
            f"source tree and the lockfile at {lock.path}, then "
            f"re-run the install."
        )
        raise typer.Exit(code=1) from exc
    _register_catalog_or_warn(
        catalog_state,
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
    _maybe_prompt_grant_required_permissions(
        manifest=manifest,
        entry=installed_entry,
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
    # Detect duplicate manifest names BEFORE the dict comprehension
    # collapses them silently. Identical to the guard in
    # ``_select_plugins`` — a remote repo with two subtrees declaring
    # the same name would otherwise install whichever entry happened
    # to win the overwrite, masking the actual artifact behind a
    # name lookup.
    seen: dict[str, list[CatalogEntry]] = {}
    for entry in catalog:
        seen.setdefault(entry.manifest.name, []).append(entry)
    duplicates = {n: ents for n, ents in seen.items() if len(ents) > 1}
    if duplicates and name in duplicates:
        paths = sorted(str(e.plugin_dir) for e in duplicates[name])
        print_error(
            f"repository at {repo_url} declares plugin name {name!r} in "
            f"multiple subtrees: {paths}. Refusing to install because the "
            "dispatcher would silently pick one and shadow the others. "
            "Resolve in the upstream repo, then re-run."
        )
        raise typer.Exit(code=1)
    by_name = {entry.manifest.name: entry for entry in catalog}
    if name not in by_name:
        print_error(
            f"plugin {name!r} not found in catalog at {repo_url} (available: {sorted(by_name)})"
        )
        raise typer.Exit(code=1)
    catalog_entry = by_name[name]
    manifest = catalog_entry.manifest
    # Use the actual on-disk directory, not ``plugins/<manifest.name>``.
    source_dir = catalog_entry.plugin_dir
    # Pre-flight lockfile health.
    _read_lock_or_exit(lock)
    plugin_home = plugin_home_root / manifest.name
    source_identity = normalize_repo_url(repo_url)
    # See `_install_from_local_directory` — persist manifest's source
    # type, not transport. Cloning from a URL does not by itself imply
    # ``source.type == plugin_home``; the manifest's declared value is
    # what the firewall keys against.
    manifest_source_type = manifest.source.type
    try:
        # Digest, plugin_home swap, lockfile commit — all wrapped.
        artifact_digest = canonical_tree_hash(source_dir)
        with _atomic_install_with_rollback(source_dir, plugin_home):
            installed_entry = _install_one(
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
    except (ValueError, OSError) as exc:
        print_error(
            f"could not commit install for {manifest.name!r}: {exc}. "
            f"The prior install (if any) was restored. Inspect the "
            f"source tree and the lockfile at {lock.path}, then "
            f"re-run the install."
        )
        raise typer.Exit(code=1) from exc
    _register_catalog_or_warn(
        catalog_state,
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
    _maybe_prompt_grant_required_permissions(
        manifest=manifest,
        entry=installed_entry,
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

    entries = _read_lock_or_exit(lock)
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
    #
    # Wrap the trust-store reads/writes so a corrupt ``trust.json`` /
    # ``disabled.json`` produces a one-line recovery hint pointing at
    # the offending file rather than a raw traceback. ``trust`` is
    # one of the commands operators run to repair plugin state, so it
    # MUST surface that state's own corruption clearly.
    try:
        was_disabled = trust.is_disabled(name)
        existing_record = trust.read(name) if scopes else None
    except (ValueError, OSError) as exc:
        print_error(
            f"trust state for {name!r} is unreadable: {exc}. "
            f"Inspect or remove the offending file under {trust.root}, "
            f"then re-run `ooo plugin trust {name} --scope <...>`."
        )
        raise typer.Exit(code=1) from exc
    prior_granted_scopes = (
        {g.scope for g in existing_record.granted_scopes}
        if existing_record is not None
        and existing_record.matches_subject(
            version=manifest.version,
            source_type=entry.source_type,
            source_identity=entry.source_identity,
            artifact_digest=entry.artifact_digest,
        )
        else set()
    )

    # ``--audit-log`` is operator-supplied and routinely points at a
    # path the user expects to exist (e.g. inside a pre-created log
    # directory). When the parent directory is missing or the file is
    # unwritable, ``open("a")`` raises ``OSError``; without this guard
    # the whole `trust` command would dump a raw traceback BEFORE any
    # grant was written, which is exactly the failure shape the rest
    # of this command is hardened against (corrupt lockfile, corrupt
    # trust file, etc.). Surface the same controlled-exit shape so the
    # operator can see the path and re-run after fixing it.
    try:
        audit_handle = audit_log_path.open("a", encoding="utf-8") if audit_log_path else None
    except OSError as exc:
        print_error(
            f"could not open audit log at {audit_log_path}: {exc}. "
            f"Ensure the parent directory exists and is writable, then "
            f"re-run `ooo plugin trust {name} --scope <...>`."
        )
        raise typer.Exit(code=1) from exc
    try:
        record = None
        if scopes:
            try:
                record = trust.grant_many_and_clear_disable(
                    plugin=name,
                    version=manifest.version,
                    scopes=scopes,
                    granted_by=granted_by,
                    source_type=entry.source_type,
                    source_identity=entry.source_identity,
                    artifact_digest=entry.artifact_digest,
                )
            except (ValueError, OSError) as exc:
                print_error(
                    f"could not write trust grant for {name!r}: {exc}. "
                    f"Inspect the trust file at {trust.root / name}, then "
                    f"re-run `ooo plugin trust {name} --scope <...>`."
                )
                raise typer.Exit(code=1) from exc
        granted_for_event = set(prior_granted_scopes)
        for scope in scopes:
            assert record is not None
            print_success(f"Granted: {scope} ({len(record.granted_scopes)} total scope(s))")
            granted_for_event.add(scope)

            # Audit `trust_state` must mirror the firewall's invokability
            # view, not the fact that *some* grant was just written. A
            # grant whose cumulative scope set still does not satisfy
            # every required permission leaves the plugin firewall-blocked
            # (`inspect`/`list` show "installed"); hardcoding "trusted"
            # here misstated the permission boundary in the audit stream
            # and broke consumers that key off the event. Concrete
            # regression case: a manifest with one required scope and
            # one optional scope, where the user grants only the
            # optional one. Compute the same predicate the firewall
            # uses post-grant.
            required_scopes = {p.scope for p in manifest.permissions if p.required}
            event_trust_state = (
                "trusted"
                if granted_for_event and not (required_scopes - granted_for_event)
                else "installed"
            )

            # Emit plugin.trusted via the ledger adapter shape.
            audit_event = {
                "schema_version": "0.1",
                "event_type": "plugin.trusted",
                "occurred_at": datetime.datetime.now(tz=datetime.UTC).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "plugin": {
                    "name": name,
                    "version": manifest.version,
                    "source_type": event_source_type,
                },
                "command": {
                    "namespace": "trust",
                    "name": "grant",
                    "argv": ["--scope", scope],
                },
                "trust_state": event_trust_state,
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
                # Audit-log writes can fail mid-stream (disk full, broken
                # pipe, NFS error). Without a guard, an OSError after the
                # grant is already persisted leaves the command in a
                # partial-commit state: the trust file IS updated and
                # ``Granted: ...`` IS already printed, but the operator
                # gets a raw traceback and no recovery hint. Surface the
                # failure with the same controlled-exit shape used by
                # every other state-file failure in this command — the
                # grant survives, the operator can audit-log-replay
                # later if the path comes back.
                try:
                    audit_handle.write(json.dumps(envelope) + "\n")
                except OSError as exc:
                    print_error(
                        f"trust grant for {name!r} ({scope}) was written, but "
                        f"the audit-log write to {audit_log_path} failed: "
                        f"{exc}. Inspect filesystem state (disk space, "
                        f"permissions) and re-run `ooo plugin trust {name} "
                        f"--scope <...>` to re-emit the audit event."
                    )
                    raise typer.Exit(code=1) from exc
    finally:
        if audit_handle is not None:
            try:
                audit_handle.close()
            except OSError:
                # Close failures on append-mode logs are not actionable
                # past this point — every grant has already been
                # persisted and the OS will reclaim the descriptor on
                # process exit. Don't shadow the caller's success path
                # with a teardown failure.
                pass

    # Scope grants clear disable atomically via grant_many_and_clear_disable().
    # A bare re-enable still has no grant to combine with, so keep the
    # idempotent clear-only path for zero-required-permission plugins.
    if not scopes:
        try:
            trust.clear_disable(name)
        except (ValueError, OSError) as exc:
            print_error(
                f"clearing the disable record for {name!r} failed: {exc}. "
                f"The plugin remains disabled until `ooo plugin trust {name}` "
                "is re-run after the underlying filesystem issue is fixed."
            )
            raise typer.Exit(code=1) from exc
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
    entries = _read_lock_or_exit(lock)
    if name not in entries:
        print_error(f"{name!r} is not installed")
        raise typer.Exit(code=1)
    entry = entries[name]
    # Honor the explicit `--trust-root` override so that grants made
    # under a non-default root are actually removed (not silently left
    # behind).
    trust = TrustStore(root=trust_root or DEFAULT_TRUST_ROOT)
    # Wrap the trust-store mutations so a corrupt ``trust.json`` /
    # ``disabled.json`` (the state ``disable`` itself manages) produces
    # a recovery hint instead of a raw traceback in the very command
    # operators run to repair that state.
    try:
        removed_trust = trust.has_trust_record(name)
        if not removed_trust:
            # Disabling an already-untrusted plugin is valid: the disable
            # record is an independent revocation signal. However, when
            # the caller supplies the wrong trust root while a grant exists
            # in the command's adjacent/default trust store, success would
            # be a dangerous lie: the runtime's real grant remains intact
            # and the new disabled.json is written in a location dispatch
            # will never consult. Detect that false-success shape before
            # writing any state in the wrong root.
            candidate_roots = {DEFAULT_TRUST_ROOT, lock.path.parent / "trust"}
            for candidate_root in candidate_roots:
                candidate_root = candidate_root.expanduser()
                if candidate_root == trust.root.expanduser():
                    continue
                if TrustStore(root=candidate_root).has_trust_record(name):
                    print_error(
                        f"no trust grant for {name!r} exists under {trust.root}, "
                        f"but a grant exists under {candidate_root}; pass the "
                        "same --trust-root used for the grant before disabling."
                    )
                    raise typer.Exit(code=1)
        trust.apply_disable(
            name,
            source_type=entry.source_type
            or ("plugin_home" if entry.source_kind == "git" else "local_path"),
            source_identity=entry.source_identity or (entry.repository or entry.plugin_home),
        )
    except (ValueError, OSError) as exc:
        print_error(
            f"could not update trust state for {name!r}: {exc}. "
            f"Inspect the files under {trust.root / name}, then re-run "
            f"`ooo plugin disable {name}`."
        )
        raise typer.Exit(code=1) from exc
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

    entries = _read_lock_or_exit(lock)
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
    # Order matters: mutate the source of truth for "installed" before
    # clearing trust / disable state, and clear both before touching
    # on-disk plugin bytes. Once `lock.remove()` succeeds the firewall
    # treats the plugin as gone — which means leftover trust/disable
    # records or bytes (if later cleanup fails) cannot silently change
    # the behavior of an installed plugin. The opposite order can wipe
    # trust/disable state while a lockfile write failure leaves the
    # plugin installed, silently re-enabling or untrusting it.
    #
    # `wipe_subject` removes both the trust file and the disable
    # record (per the RFC: "remove ALSO deletes any disable record
    # for the plugin's install subject"). Wrap so a corrupt trust
    # file produces a recovery hint rather than a traceback in the
    # exact command operators run to repair plugin state.
    #
    # The full lockfile-mutation through plugin_home cleanup runs
    # inside ``lock.transaction()`` so the operation is atomic
    # against concurrent ``install``. Without the transaction
    # window, a concurrent ``install`` could race in between
    # ``lock.remove()`` and ``shutil.rmtree(plugin_home)``: it would
    # re-add the lockfile entry and recreate the plugin home, and
    # the still-running ``remove`` would then delete the freshly-
    # installed bytes — leaving the lockfile pointing at a missing
    # directory. Holding the file lock through the rmtree closes
    # that window.
    plugin_home_status = "plugin home"
    try:
        with lock.transaction():
            lock.remove(name)
            trust.wipe_subject(name)
            if plugin_home.is_dir():
                try:
                    shutil.rmtree(plugin_home)
                except OSError as exc:
                    # Bookkeeping state is already consistent
                    # (lockfile + trust both say uninstalled), so the
                    # plugin cannot be invoked. Surface the cleanup
                    # failure so the user can remove the leftover
                    # directory manually, but don't fail the command.
                    plugin_home_status = (
                        f"plugin home (BYTES NOT REMOVED: {plugin_home} — "
                        f"{type(exc).__name__}: {exc}; remove manually)"
                    )
    except (ValueError, OSError) as exc:
        print_error(
            f"could not finalize remove for {name!r}: {exc}. "
            f"Inspect the trust files under {trust.root / name} and the "
            f"lockfile at {lock.path}, then re-run `ooo plugin remove "
            f"{name}` after the underlying issue is fixed."
        )
        raise typer.Exit(code=1) from exc

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
