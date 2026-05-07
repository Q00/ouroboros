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
    plugin: str
    version: str
    granted_scopes: tuple[GrantedScope, ...] = ()

    def has_scope(self, scope: str) -> bool:
        """Exact-string scope check (per Q00/ouroboros-plugins#9 Q3 lock —
        parent scope does NOT imply child)."""
        return any(g.scope == scope for g in self.granted_scopes)

    def missing(self, required_scopes: Iterable[str]) -> list[str]:
        """Return required scopes that are not granted, in order."""
        granted = {g.scope for g in self.granted_scopes}
        return [s for s in required_scopes if s not in granted]


class TrustStore:
    """Per-plugin trust store at <root>/<plugin-name>/trust.json."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or DEFAULT_TRUST_ROOT

    def _path(self, plugin: str) -> Path:
        return self.root / plugin / "trust.json"

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
        when: datetime | None = None,
    ) -> TrustRecord:
        """Grant `scope` to `plugin@version`. Idempotent: granting an
        already-granted scope is a no-op (timestamps preserved)."""
        when = when or datetime.now(tz=UTC)
        ts = when.strftime("%Y-%m-%dT%H:%M:%SZ")

        existing = self.read(plugin)
        # Per Q00/ouroboros-plugins#9 Q4: version bump invalidates trust.
        if existing is not None and existing.version != version:
            existing = None  # treat as fresh

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
        self._write_atomic(plugin, payload)
        return TrustRecord(plugin=plugin, version=version, granted_scopes=tuple(granted))

    def reset_for_version_bump(self, plugin: str, new_version: str) -> None:
        """Invalidate trust because the plugin's version changed.

        Writes a new trust file with version=new_version and empty grants.
        Per Q00/ouroboros-plugins#9 Q4 lock.
        """
        payload = {
            "schema_version": TRUST_SCHEMA_VERSION,
            "plugin": plugin,
            "version": new_version,
            "granted_scopes": [],
        }
        self._write_atomic(plugin, payload)

    def remove(self, plugin: str) -> bool:
        """Remove the trust file for `plugin`. Returns True if removed."""
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


__all__ = [
    "DEFAULT_TRUST_ROOT",
    "TRUST_SCHEMA_VERSION",
    "GrantedScope",
    "TrustRecord",
    "TrustStore",
]
