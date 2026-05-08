"""Per-plugin trust store.

Persists granted trust scopes per plugin at
`~/.ouroboros/plugins/<name>/trust.json`. Per the locked Q00/ouroboros#732
spec (which consumes the locked Q00/ouroboros-plugins#9 trust UX answers):

- **Per-user storage** (Q5): one trust file per installed plugin, in the
  same per-user directory as the plugin home.
- **Version-bump invalidation** (Q4): when a plugin's version changes,
  the trust file is reset (granted_scopes emptied). The user must re-grant
  scopes via `ooo plugin trust` after upgrading.
- **Exact scope grants** (Q3): each grant records the exact scope string;
  parent scopes do not imply children.
- **No raw tokens stored.** Only the scope name, timestamp, and granting
  user identity are persisted.

The trust store does NOT emit audit events itself; the firewall (#729) and
CLI (#731) emit `plugin.trusted` when grants happen, sourcing the data
from this store.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import tempfile

TRUST_SCHEMA_VERSION = "0.1"

# Default location root. Each plugin gets a subdirectory here.
#
# CRITICAL: this MUST live OUTSIDE the plugin install root
# (``~/.ouroboros/plugins``). The firewall recomputes the canonical tree
# hash of the installed plugin home before every invocation; if
# ``trust.json`` or ``disabled.json`` were written inside that subtree
# the very act of trusting a plugin would change its digest and cause
# the next invocation to fail closed with ``trust_subject_changed``.
# Trust metadata is metadata ABOUT the artifact, not part of it — it
# lives in a sibling directory so the hashed artifact stays immutable
# under user-driven trust state changes.
DEFAULT_TRUST_ROOT = Path.home() / ".ouroboros" / "trust"

# Plugin name pattern, matching plugin.schema.json `/name`. Enforced here at
# the persistence-API boundary so that a plugin name with path separators or
# `..` cannot escape the trust root via `<root>/<plugin>/trust.json` and
# read/write/delete arbitrary files. Higher layers (manifest validation,
# manager) also reject malformed names; this is defence in depth.
_PLUGIN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")


def _validate_plugin_name(plugin: str) -> None:
    """Reject plugin identifiers that could escape the trust root.

    Raises:
        ValueError: if ``plugin`` does not match the locked manifest name
            pattern (lowercase alphanumeric + dashes, 3-64 chars, no leading
            or trailing dash, no path separators).
    """
    if not isinstance(plugin, str) or not _PLUGIN_NAME_RE.fullmatch(plugin):
        raise ValueError(
            f"invalid plugin name {plugin!r}: must match "
            r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$"
        )


@dataclass(frozen=True)
class GrantedScope:
    scope: str
    granted_at: str  # RFC3339
    granted_by: str  # e.g. "user:<id>"


@dataclass(frozen=True)
class TrustRecord:
    """A grant record bound to a specific install subject.

    Per the locked RFC (`docs/rfc/userlevel-plugins.md`, "Trust identity"),
    trust is keyed by the tuple
    ``(source.type, source_identity, artifact_digest)``. Older trust files
    (pre-RFC) may not carry the new fields; they read as empty strings,
    and the firewall treats an empty record digest as "legacy / no
    enforcement" so the legacy code path keeps working in tests. CLI
    install paths always populate the new fields so production records
    are fully bound.
    """

    plugin: str
    version: str
    granted_scopes: tuple[GrantedScope, ...] = ()
    source_type: str = ""
    source_identity: str = ""
    artifact_digest: str = ""

    def has_scope(self, scope: str) -> bool:
        """Exact-string scope check (per Q00/ouroboros-plugins#9 Q3 lock —
        parent scope does NOT imply child)."""
        return any(g.scope == scope for g in self.granted_scopes)

    def missing(self, required_scopes: Iterable[str]) -> list[str]:
        """Return required scopes that are not granted, in order."""
        granted = {g.scope for g in self.granted_scopes}
        return [s for s in required_scopes if s not in granted]

    def matches_subject(
        self,
        *,
        version: str,
        source_type: str,
        source_identity: str,
        artifact_digest: str,
    ) -> bool:
        """True iff this record was granted against the given install subject.

        Per the RFC, the trust subject is `(version, source.type,
        source_identity, artifact_digest)`. ANY field changing voids the
        grant — that closes the same-name reinstall and code-substitution
        paths.

        Empty fields on this record are treated as "legacy / unbound" and
        skip the corresponding check (so pre-RFC trust files still resolve
        in tests). The CLI install paths set every field, so production
        records always go through the strict comparison.
        """
        if self.version != version:
            return False
        if self.source_type and self.source_type != source_type:
            return False
        if self.source_identity and self.source_identity != source_identity:
            return False
        return not (self.artifact_digest and self.artifact_digest != artifact_digest)


class TrustStore:
    """Per-plugin trust store at <root>/<plugin-name>/trust.json."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or DEFAULT_TRUST_ROOT

    def _path(self, plugin: str) -> Path:
        return self.root / plugin / "trust.json"

    def _disable_path(self, plugin: str) -> Path:
        return self.root / plugin / "disabled.json"

    def read(self, plugin: str) -> TrustRecord | None:
        """Read the trust record for `plugin`, or None if not present.

        Raises ``ValueError`` for any structurally-invalid trust file
        (malformed JSON, unsupported schema_version, missing required
        fields, wrong-typed values). The CLI's trust-state wrappers
        catch ``ValueError`` / ``OSError`` and surface a friendly
        recovery hint, so every shape of corruption MUST land in
        ``ValueError`` rather than escaping as ``KeyError`` /
        ``TypeError`` from the ``data["plugin"]`` / ``g["scope"]`` /
        etc. lookups below — otherwise the operator-facing repair
        commands (``trust``, ``inspect``, ``list``, dispatch) would
        traceback on a parseable-but-wrong trust file.
        """
        _validate_plugin_name(plugin)
        path = self._path(plugin)
        if not path.is_file():
            return None
        try:
            with path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ValueError(f"trust file {path} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"trust file {path} is not a JSON object")
        version = data.get("schema_version")
        if version != TRUST_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported trust file schema_version {version!r}; "
                f"expected {TRUST_SCHEMA_VERSION!r}"
            )
        for required in ("plugin", "version"):
            if required not in data:
                raise ValueError(f"trust file {path} is missing required field {required!r}")
            if not isinstance(data[required], str):
                raise ValueError(
                    f"trust file {path}: {required!r} is not a string "
                    f"(got {type(data[required]).__name__})"
                )
        granted_raw = data.get("granted_scopes", [])
        if not isinstance(granted_raw, list):
            raise ValueError(
                f"trust file {path}: 'granted_scopes' is not a list "
                f"(got {type(granted_raw).__name__})"
            )
        granted: list[GrantedScope] = []
        for index, g in enumerate(granted_raw):
            if not isinstance(g, dict):
                raise ValueError(
                    f"trust file {path}: granted_scopes[{index}] is not a JSON object "
                    f"(got {type(g).__name__})"
                )
            for field in ("scope", "granted_at", "granted_by"):
                if field not in g:
                    raise ValueError(
                        f"trust file {path}: granted_scopes[{index}] is missing "
                        f"required field {field!r}"
                    )
                if not isinstance(g[field], str):
                    raise ValueError(
                        f"trust file {path}: granted_scopes[{index}].{field} is not a string "
                        f"(got {type(g[field]).__name__})"
                    )
            granted.append(
                GrantedScope(
                    scope=g["scope"],
                    granted_at=g["granted_at"],
                    granted_by=g["granted_by"],
                )
            )
        return TrustRecord(
            plugin=data["plugin"],
            version=data["version"],
            granted_scopes=tuple(granted),
            source_type=data.get("source_type", ""),
            source_identity=data.get("source_identity", ""),
            artifact_digest=data.get("artifact_digest", ""),
        )

    def _write_atomic(self, plugin: str, payload: dict) -> None:
        path = self._path(plugin)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".trust.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    @contextmanager
    def _grant_lock(self, plugin: str) -> Iterator[None]:
        """Serialize ``grant`` / ``reset`` / ``remove`` updates for one plugin.

        Without this guard, two concurrent ``grant()`` calls for the same
        plugin can both observe the same prior file and each write back a
        one-scope payload, so the last writer silently deletes the other
        grant — a real trust-state data-loss bug. ``Lockfile`` uses the
        same ``fcntl.flock`` pattern; this mirrors it per-plugin so
        cross-plugin grants don't serialize against each other.

        Deliberately leaves ``trust.json.lock`` on disk after the
        critical section: POSIX ``flock`` is attached to the inode
        behind the lock-file path, so unlinking it would orphan the
        inode and let a concurrent ``grant()`` open a brand-new inode
        and run in parallel — reopening the very race the lock was
        added to close.
        """
        path = self._path(plugin)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl
        except ImportError:  # pragma: no cover — non-POSIX platforms
            yield
            return
        lock_path = path.with_suffix(path.suffix + ".lock")
        with lock_path.open("w") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def grant(
        self,
        *,
        plugin: str,
        version: str,
        scope: str,
        granted_by: str,
        source_type: str = "",
        source_identity: str = "",
        artifact_digest: str = "",
        when: datetime | None = None,
    ) -> TrustRecord:
        """Grant `scope` to the install subject of ``plugin``.

        Per the locked RFC, the trust subject is the tuple
        ``(version, source.type, source_identity, artifact_digest)``. ANY
        field changing voids prior grants — passing a different value for
        any field resets the file to a fresh subject before recording the
        grant.

        Older callers may omit the new fields (legacy path retained for
        unit tests of the firewall and trust store); production CLI
        callers always pass the full triple, so the install subject is
        bound for every real grant.

        Idempotent: granting an already-granted scope is a no-op
        (timestamps preserved).

        Concurrency-safe: the read-modify-write cycle is bracketed by
        a per-plugin POSIX file lock so two concurrent ``grant()``
        calls cannot drop one another's scope.
        """
        _validate_plugin_name(plugin)
        when = when or datetime.now(tz=UTC)
        ts = when.strftime("%Y-%m-%dT%H:%M:%SZ")

        with self._grant_lock(plugin):
            existing = self.read(plugin)
            if existing is not None and not _subject_matches(
                existing,
                version=version,
                source_type=source_type,
                source_identity=source_identity,
                artifact_digest=artifact_digest,
            ):
                existing = None  # subject changed — treat as fresh

            granted = list(existing.granted_scopes) if existing else []
            if all(g.scope != scope for g in granted):
                granted.append(GrantedScope(scope=scope, granted_at=ts, granted_by=granted_by))

            payload = {
                "schema_version": TRUST_SCHEMA_VERSION,
                "plugin": plugin,
                "version": version,
                "granted_scopes": [
                    {"scope": g.scope, "granted_at": g.granted_at, "granted_by": g.granted_by}
                    for g in granted
                ],
            }
            if source_type:
                payload["source_type"] = source_type
            if source_identity:
                payload["source_identity"] = source_identity
            if artifact_digest:
                payload["artifact_digest"] = artifact_digest
            self._write_atomic(plugin, payload)
            return TrustRecord(
                plugin=plugin,
                version=version,
                granted_scopes=tuple(granted),
                source_type=source_type,
                source_identity=source_identity,
                artifact_digest=artifact_digest,
            )

    def reset_for_subject_change(
        self,
        plugin: str,
        *,
        new_version: str,
        new_source_type: str = "",
        new_source_identity: str = "",
        new_artifact_digest: str = "",
    ) -> None:
        """Invalidate trust because the install subject changed.

        Per the locked RFC, ANY change to the
        ``(version, source.type, source_identity, artifact_digest)``
        tuple voids prior grants. Writes a fresh trust file pinned to the
        new subject with empty grants — the user must re-consent.

        Bracketed by the same per-plugin lock as ``grant()`` so a reset
        cannot race with a concurrent grant for the prior subject.
        """
        _validate_plugin_name(plugin)
        with self._grant_lock(plugin):
            payload: dict = {
                "schema_version": TRUST_SCHEMA_VERSION,
                "plugin": plugin,
                "version": new_version,
                "granted_scopes": [],
            }
            if new_source_type:
                payload["source_type"] = new_source_type
            if new_source_identity:
                payload["source_identity"] = new_source_identity
            if new_artifact_digest:
                payload["artifact_digest"] = new_artifact_digest
            self._write_atomic(plugin, payload)

    # Backwards-compatible alias retained because the previous lock-step
    # was version-only. Production callers should prefer
    # ``reset_for_subject_change`` so source_identity / artifact_digest
    # are recorded.
    def reset_for_version_bump(self, plugin: str, new_version: str) -> None:
        self.reset_for_subject_change(plugin, new_version=new_version)

    def remove(self, plugin: str) -> bool:
        """Remove the trust file for `plugin`. Returns True if removed.

        Does NOT remove the disable record — `disable` writes that record
        as an independent revocation signal (per the RFC, "Disable
        records are keyed by `(name, source.type, source_identity)`
        without `artifact_digest`, and survive every digest change").
        Use `clear_disable` for that, or call `wipe_subject` to remove
        both at once.
        """
        _validate_plugin_name(plugin)
        path = self._path(plugin)
        if not path.is_file():
            return False
        path.unlink()
        # Best-effort: remove the empty plugin dir if it's empty afterwards.
        try:
            path.parent.rmdir()
        except OSError:
            pass
        return True

    # ------------------------------------------------------------------
    # Disable-record API (RFC: independent revocation signal that survives
    # digest changes, and that the firewall checks before any trust check).
    # ------------------------------------------------------------------

    def is_disabled(self, plugin: str) -> bool:
        """True if a disable record exists for `plugin`.

        This is the **name-only** predicate; it is intentionally lossy
        because the RFC keys disable records by ``(name, source.type,
        source_identity)`` and many call sites only know the name. Use
        ``is_disabled_for_subject`` when the install subject is known
        (CLI dispatch, firewall) so a stale disable record from an old
        source does not block a fresh install from a different source.
        """
        return self._disable_path(plugin).is_file()

    def is_disabled_for_subject(
        self,
        plugin: str,
        *,
        source_type: str,
        source_identity: str,
    ) -> bool:
        """True iff a disable record exists AND its `(source.type,
        source_identity)` matches the given install subject.

        Per the locked RFC ("Disable records"), disable records are
        keyed by ``(name, source.type, source_identity)`` without
        ``artifact_digest``: they survive upgrades but DO NOT carry over
        to a re-install from a different source. This predicate is the
        production-correct check the firewall and the CLI's view layer
        should use; ``is_disabled`` remains for code paths that legacy
        only know the name.

        Records lacking source identity (pre-RFC writes / unknown
        provenance) are treated as still applying — failing closed
        keeps the safety property that an explicit disable cannot be
        silently bypassed by a re-install whose source-identity field
        was not yet recorded.
        """
        record = self.read_disable(plugin)
        if record is None:
            return False
        recorded_type = record.get("source_type", "")
        recorded_identity = record.get("source_identity", "")
        if not recorded_type and not recorded_identity:
            # Pre-RFC / partially-recorded disable — fail closed.
            return True
        if recorded_type and recorded_type != source_type:
            return False
        return not (recorded_identity and recorded_identity != source_identity)

    def read_disable(self, plugin: str) -> dict | None:
        """Return the parsed disable record, or None."""
        path = self._disable_path(plugin)
        if not path.is_file():
            return None
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def write_disable(
        self,
        plugin: str,
        *,
        source_type: str,
        source_identity: str,
        disabled_by: str = "user:cli",
        when: datetime | None = None,
    ) -> None:
        """Persist a disable record for `plugin`.

        The record carries ``source_type`` and ``source_identity`` (the
        subject-stable portion of the trust subject) so a future
        ``remove + add`` cycle that lands the same source still inherits
        the disable signal — exactly what the RFC asks for.
        """
        when = when or datetime.now(tz=UTC)
        payload = {
            "schema_version": TRUST_SCHEMA_VERSION,
            "plugin": plugin,
            "source_type": source_type,
            "source_identity": source_identity,
            "disabled_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "disabled_by": disabled_by,
        }
        path = self._disable_path(plugin)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".disabled.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def clear_disable(self, plugin: str) -> bool:
        """Remove the disable record for `plugin`. Returns True if removed."""
        path = self._disable_path(plugin)
        if not path.is_file():
            return False
        path.unlink()
        try:
            path.parent.rmdir()
        except OSError:
            pass
        return True

    def wipe_subject(self, plugin: str) -> None:
        """Remove every artifact for `plugin` (trust + disable + dir).

        Used by ``ooo plugin remove`` per the RFC: "remove ALSO deletes
        any disable record for the plugin's install subject — once the
        user has uninstalled it, the disable signal no longer applies and
        a future fresh install starts un-trusted-but-enabled".
        """
        self.remove(plugin)
        self.clear_disable(plugin)


def _subject_matches(
    record: TrustRecord,
    *,
    version: str,
    source_type: str,
    source_identity: str,
    artifact_digest: str,
) -> bool:
    """Internal helper for `grant` — `_subject_matches` is intentionally
    permissive about empty fields on the existing record so legacy trust
    files (no source_identity / artifact_digest persisted) are not
    spuriously voided by an otherwise-valid second grant from a CLI that
    now passes the triple. Once any field is set, it must match.
    """
    if record.version != version:
        return False
    if record.source_type and record.source_type != source_type:
        return False
    if record.source_identity and record.source_identity != source_identity:
        return False
    return not (record.artifact_digest and record.artifact_digest != artifact_digest)


__all__ = [
    "DEFAULT_TRUST_ROOT",
    "TRUST_SCHEMA_VERSION",
    "GrantedScope",
    "TrustRecord",
    "TrustStore",
]
