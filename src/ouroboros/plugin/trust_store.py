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

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tempfile

TRUST_SCHEMA_VERSION = "0.1"

# Default location root. Each plugin gets a subdirectory here.
DEFAULT_TRUST_ROOT = Path.home() / ".ouroboros" / "plugins"


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
        """Read the trust record for `plugin`, or None if not present."""
        path = self._path(plugin)
        if not path.is_file():
            return None
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        version = data.get("schema_version")
        if version != TRUST_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported trust file schema_version {version!r}; "
                f"expected {TRUST_SCHEMA_VERSION!r}"
            )
        return TrustRecord(
            plugin=data["plugin"],
            version=data["version"],
            granted_scopes=tuple(
                GrantedScope(
                    scope=g["scope"],
                    granted_at=g["granted_at"],
                    granted_by=g["granted_by"],
                )
                for g in data.get("granted_scopes", [])
            ),
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
        """
        when = when or datetime.now(tz=UTC)
        ts = when.strftime("%Y-%m-%dT%H:%M:%SZ")

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
        """
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
