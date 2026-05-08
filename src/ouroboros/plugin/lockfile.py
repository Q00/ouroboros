"""Plugin lockfile.

Persists records of installed plugins at `~/.ouroboros/plugins.lock` (TOML).
The lockfile is the source of truth for "what is installed" — the trust store
(see `trust_store.py`) is the source of truth for "what is trusted."

Per the locked Q00/ouroboros#732 spec:
  - Atomic writes (temp file + rename).
  - Concurrent-write safety via a POSIX file lock (fcntl).
  - Deterministic ordering: entries sorted by `name` so diffs are reviewable.
  - Schema versioned (`schema_version = "0.1"`).
  - Removal is atomic (no orphaned entries).

The TOML shape is fixed and small. We hand-roll serialization rather than
take on a `tomli_w` dependency. Reading uses stdlib `tomllib`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
import tomllib

LOCKFILE_SCHEMA_VERSION = "0.1"

# Default location. Overridable via constructor for tests.
DEFAULT_LOCKFILE_PATH = Path.home() / ".ouroboros" / "plugins.lock"


@dataclass(frozen=True)
class LockEntry:
    """One installed plugin's lockfile record."""

    name: str
    version: str
    source_kind: str  # "git" | "local"
    repository: str | None  # git URL when source_kind="git"; else None
    git_sha: str | None
    manifest_checksum: str  # "sha256:<hex>"
    installed_at: str  # RFC3339
    plugin_home: str  # filesystem path

    def to_toml_lines(self) -> list[str]:
        lines = ["[[plugin]]"]
        lines.append(f"name = {_toml_str(self.name)}")
        lines.append(f"version = {_toml_str(self.version)}")
        lines.append(f"source_kind = {_toml_str(self.source_kind)}")
        if self.repository is not None:
            lines.append(f"repository = {_toml_str(self.repository)}")
        if self.git_sha is not None:
            lines.append(f"git_sha = {_toml_str(self.git_sha)}")
        lines.append(f"manifest_checksum = {_toml_str(self.manifest_checksum)}")
        lines.append(f"installed_at = {_toml_str(self.installed_at)}")
        lines.append(f"plugin_home = {_toml_str(self.plugin_home)}")
        return lines


_TOML_BASIC_ESCAPES: dict[str, str] = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\t": "\\t",
    "\r": "\\r",
    "\b": "\\b",
    "\f": "\\f",
}


def _toml_str(value: str) -> str:
    """Serialize a string value as a TOML basic string.

    The lockfile normally stores names, paths, hashes and timestamps, none of
    which contain control characters in practice. The escape table previously
    only covered ``\\``, ``"``, ``\\n``, ``\\t`` and ``\\r``, so any other C0
    byte (e.g. ``\\x0b``, ``\\x0c``, ``\\x05``) was emitted verbatim into the
    output even though :func:`tomllib.loads` rejects bare C0 inside basic
    strings — meaning a value like ``"sha256:\\x0b..."`` produced a lockfile
    that the next ``Lockfile.read()`` call would fail to parse.

    The fix keeps the well-known escapes for ``\\b`` and ``\\f`` (TOML 1.0
    §2.1) and falls back to ``\\uXXXX`` for any remaining ``ord(ch) < 0x20``
    or ``ord(ch) == 0x7f`` (DEL) byte. ASCII printable values are still
    emitted verbatim, so existing well-formed lockfiles remain unchanged.
    """
    if not any(ch in _TOML_BASIC_ESCAPES or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return f'"{value}"'
    parts: list[str] = []
    for ch in value:
        if ch in _TOML_BASIC_ESCAPES:
            parts.append(_TOML_BASIC_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            parts.append(f"\\u{ord(ch):04x}")
        else:
            parts.append(ch)
    return f'"{"".join(parts)}"'


class Lockfile:
    """Atomic, file-locked plugins lockfile manager."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_LOCKFILE_PATH

    def _ensure_dir(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> dict[str, LockEntry]:
        """Read the lockfile, returning entries keyed by plugin name.

        Returns an empty dict if the file does not exist.
        Raises ValueError if the schema_version is unsupported.
        """
        if not self.path.is_file():
            return {}
        with self.path.open("rb") as handle:
            data = tomllib.load(handle)
        version = data.get("schema_version")
        if version != LOCKFILE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported lockfile schema_version {version!r}; "
                f"expected {LOCKFILE_SCHEMA_VERSION!r}"
            )
        result: dict[str, LockEntry] = {}
        for raw in data.get("plugin", []):
            entry = LockEntry(
                name=raw["name"],
                version=raw["version"],
                source_kind=raw["source_kind"],
                repository=raw.get("repository"),
                git_sha=raw.get("git_sha"),
                manifest_checksum=raw["manifest_checksum"],
                installed_at=raw["installed_at"],
                plugin_home=raw["plugin_home"],
            )
            result[entry.name] = entry
        return result

    def _write_atomic(self, entries: dict[str, LockEntry]) -> None:
        """Write the lockfile atomically (temp file + rename)."""
        self._ensure_dir()
        ordered = sorted(entries.values(), key=lambda e: e.name)
        lines = [f'schema_version = "{LOCKFILE_SCHEMA_VERSION}"', ""]
        for entry in ordered:
            lines.extend(entry.to_toml_lines())
            lines.append("")
        body = "\n".join(lines).rstrip() + "\n"

        # Write to temp file in the same directory, then atomic rename.
        fd, tmp_path = tempfile.mkstemp(prefix=".plugins.lock.", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        except Exception:
            # Best-effort cleanup of the temp file on failure.
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
            raise

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        """Acquire an exclusive flock for concurrent-write safety.

        POSIX-only. Falls through gracefully on platforms without fcntl
        (the file is still atomically replaced via os.replace, which gives
        last-writer-wins semantics — acceptable for non-concurrent use).
        """
        self._ensure_dir()
        try:
            import fcntl
        except ImportError:  # pragma: no cover — non-POSIX platforms
            yield
            return
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("w") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def add(self, entry: LockEntry) -> None:
        """Add or replace an entry. Holds the file lock for the duration."""
        with self._file_lock():
            entries = self.read()
            entries[entry.name] = entry
            self._write_atomic(entries)

    def remove(self, name: str) -> bool:
        """Remove an entry by name. Returns True if removed, False if absent."""
        with self._file_lock():
            entries = self.read()
            if name not in entries:
                return False
            entries.pop(name)
            self._write_atomic(entries)
            return True


__all__ = [
    "DEFAULT_LOCKFILE_PATH",
    "LOCKFILE_SCHEMA_VERSION",
    "LockEntry",
    "Lockfile",
]
