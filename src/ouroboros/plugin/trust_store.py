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

    @contextmanager
    def _trust_lock(self, plugin: str) -> Iterator[None]:
        """Hold an exclusive POSIX flock for the duration of a mutation.

        The trust file is the source of truth for authorization, so every
        mutating operation (`grant`, `reset_for_version_bump`, `remove`)
        must be serialised across processes — without this, concurrent
        writers both read the same prior file and the second `os.replace`
        wins, silently dropping a previously granted scope or
        reintroducing stale state. The lock is taken on a sidecar `.lock`
        file in the plugin's trust directory; on platforms without
        `fcntl` we degrade to last-writer-wins (no concurrent CLI usage
        is expected outside POSIX).
        """
        plugin_dir = self.root / plugin
        plugin_dir.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl
        except ImportError:  # pragma: no cover — non-POSIX platforms
            yield
            return
        lock_path = plugin_dir / "trust.json.lock"
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
        when: datetime | None = None,
    ) -> TrustRecord:
        """Grant `scope` to `plugin@version`. Idempotent: granting an
        already-granted scope is a no-op (timestamps preserved).

        The read-modify-write is serialised under a per-plugin file lock
        so that concurrent grants for different scopes do not race and
        silently drop one of them.
        """
        when = when or datetime.now(tz=UTC)
        ts = when.strftime("%Y-%m-%dT%H:%M:%SZ")

        with self._trust_lock(plugin):
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
        Per Q00/ouroboros-plugins#9 Q4 lock. Held under the per-plugin
        trust lock so it cannot race a concurrent `grant()`.
        """
        payload = {
            "schema_version": TRUST_SCHEMA_VERSION,
            "plugin": plugin,
            "version": new_version,
            "granted_scopes": [],
        }
        with self._trust_lock(plugin):
            self._write_atomic(plugin, payload)

    def remove(self, plugin: str) -> bool:
        """Remove the trust file for `plugin`. Returns True if removed.

        Held under the per-plugin trust lock so it cannot race a
        concurrent `grant()` or `reset_for_version_bump()` and re-create
        a partially written file.

        The sidecar lock file is intentionally left in place: unlinking
        it after releasing the flock would let a concurrent `grant()`
        racing this remove lock the *old* inode, while the next writer
        recreates the path and locks a *new* inode — so the two
        processes would no longer serialise. Keeping the lock file
        sticky guarantees every future writer locks the same inode that
        is still held open by any concurrent process. The trade-off is
        that the plugin directory is not pruned by `remove()` alone;
        directory cleanup is the caller's job (e.g. `ooo plugin remove`
        deletes the whole plugin home).
        """
        path = self._path(plugin)
        if not path.is_file():
            return False
        with self._trust_lock(plugin):
            # Re-check under the lock — another writer may have removed
            # the file between the precheck and acquisition.
            if not path.is_file():
                return False
            path.unlink()
        return True


__all__ = [
    "DEFAULT_TRUST_ROOT",
    "TRUST_SCHEMA_VERSION",
    "GrantedScope",
    "TrustRecord",
    "TrustStore",
]
