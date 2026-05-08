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
    """One installed plugin's lockfile record.

    Per the locked RFC (`docs/rfc/userlevel-plugins.md`, "Trust identity"),
    the install subject is keyed by the tuple
    ``(source.type, source_identity, artifact_digest)``:

    - ``source_type`` is the manifest's ``source.type`` enum
      (``local_path`` | ``plugin_home`` | ``first_party``). It is mapped
      from ``source_kind`` ("git" | "local") at install time.
    - ``source_identity`` is the normalized repo URL (for ``plugin_home``)
      or absolute resolved filesystem path (for ``local_path``).
    - ``artifact_digest`` is the canonical tree hash of the **complete
      installed artifact** (see ``ouroboros.plugin.digest``), recomputed
      before every invocation by the firewall.

    The triple is recorded at install time so the firewall can detect
    code substitution under the same source: a digest mismatch fails
    closed with ``result.status="trust_subject_changed"``.
    """

    name: str
    version: str
    source_kind: str  # "git" | "local"
    repository: str | None  # git URL when source_kind="git"; else None
    git_sha: str | None
    manifest_checksum: str  # "sha256:<hex>"
    installed_at: str  # RFC3339
    plugin_home: str  # filesystem path
    # New trust-subject fields (RFC). Older lockfile rows that pre-date
    # this contract will read as empty strings; the firewall treats
    # empty values conservatively (legacy → no digest enforcement, but
    # CLI install paths always populate them so prod records are bound).
    source_type: str = ""  # manifest source.type ("local_path" | "plugin_home")
    source_identity: str = ""  # normalized repo URL or absolute local path
    artifact_digest: str = ""  # canonical tree hash, "sha256:<hex>"

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
        if self.source_type:
            lines.append(f"source_type = {_toml_str(self.source_type)}")
        if self.source_identity:
            lines.append(f"source_identity = {_toml_str(self.source_identity)}")
        if self.artifact_digest:
            lines.append(f"artifact_digest = {_toml_str(self.artifact_digest)}")
        return lines

    def subject_for_disable(self) -> tuple[str, str]:
        """Canonical ``(source_type, source_identity)`` for disable lookup.

        Older lockfile rows that pre-date the trust-subject contract have
        empty ``source_type`` / ``source_identity`` columns. Both the
        write path (``ooo plugin disable``) and every read path
        (dispatcher, ``inspect``, ``list``) MUST agree on the same
        fallback for those rows; otherwise a ``disable`` write lands
        under one subject and the dispatcher's ``is_disabled_for_subject``
        check runs against another, leaving the user with a "disabled"
        plugin that ``ooo <name> ...`` still happily invokes.

        Fallback rules:
          - ``source_type``: stored value if set; else ``"plugin_home"``
            for git installs, ``"local_path"`` otherwise.
          - ``source_identity``: stored value if set; else the git
            ``repository`` URL or the local ``plugin_home`` path.
        """
        source_type = self.source_type or (
            "plugin_home" if self.source_kind == "git" else "local_path"
        )
        source_identity = self.source_identity or (self.repository or self.plugin_home)
        return source_type, source_identity


def _toml_str(value: str) -> str:
    """Serialize a string value as TOML basic string. Restricts content to
    avoid escaping edge cases — the lockfile only stores names, paths, hashes
    and timestamps, none of which contain control chars or non-ASCII in
    practice."""
    if any(ch == "\\" or ch == '"' or ord(ch) < 0x20 for ch in value):
        # Use TOML's basic string escapes for the small set we actually need.
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\t", "\\t")
            .replace("\r", "\\r")
        )
        return f'"{escaped}"'
    return f'"{value}"'


class Lockfile:
    """Atomic, file-locked plugins lockfile manager."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_LOCKFILE_PATH

    def _ensure_dir(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> dict[str, LockEntry]:
        """Read the lockfile, returning entries keyed by plugin name.

        Returns an empty dict if the file does not exist. Raises
        ``ValueError`` for any structurally-invalid lockfile
        (unsupported schema_version, malformed TOML, missing required
        per-entry fields, wrong shape). The CLI's lockfile-read
        wrapper catches ``ValueError``/``OSError`` and surfaces a
        recovery hint, so every shape of corruption MUST land in
        ``ValueError`` rather than escaping as ``KeyError`` /
        ``TypeError`` from the ``raw["name"]`` / ``raw["version"]`` /
        etc. lookups below — otherwise the operator-facing repair
        commands (``trust``, ``disable``, ``remove``, ``inspect``,
        ``list``) would traceback on a parseable-but-wrong lockfile.
        """
        if not self.path.is_file():
            return {}
        try:
            with self.path.open("rb") as handle:
                data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"lockfile {self.path} is not valid TOML: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"lockfile {self.path} is not a TOML table")
        version = data.get("schema_version")
        if version != LOCKFILE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported lockfile schema_version {version!r}; "
                f"expected {LOCKFILE_SCHEMA_VERSION!r}"
            )
        plugin_section = data.get("plugin", [])
        if not isinstance(plugin_section, list):
            raise ValueError(
                f"lockfile {self.path} has a non-list `plugin` section "
                f"(got {type(plugin_section).__name__})"
            )
        result: dict[str, LockEntry] = {}
        required_fields = (
            "name",
            "version",
            "source_kind",
            "manifest_checksum",
            "installed_at",
            "plugin_home",
        )
        for index, raw in enumerate(plugin_section):
            if not isinstance(raw, dict):
                raise ValueError(
                    f"lockfile {self.path}: plugin[{index}] is not a TOML table "
                    f"(got {type(raw).__name__})"
                )
            for field in required_fields:
                if field not in raw:
                    raise ValueError(
                        f"lockfile {self.path}: plugin[{index}] is missing required field {field!r}"
                    )
                if not isinstance(raw[field], str):
                    raise ValueError(
                        f"lockfile {self.path}: plugin[{index}].{field} is not a string "
                        f"(got {type(raw[field]).__name__})"
                    )
            entry = LockEntry(
                name=raw["name"],
                version=raw["version"],
                source_kind=raw["source_kind"],
                repository=raw.get("repository"),
                git_sha=raw.get("git_sha"),
                manifest_checksum=raw["manifest_checksum"],
                installed_at=raw["installed_at"],
                plugin_home=raw["plugin_home"],
                source_type=raw.get("source_type", ""),
                source_identity=raw.get("source_identity", ""),
                artifact_digest=raw.get("artifact_digest", ""),
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
