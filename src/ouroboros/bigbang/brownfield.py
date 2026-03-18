"""Brownfield repository registry — schema, validation, and file I/O.

Manages the global brownfield registry at ``~/.ouroboros/brownfield.json``.
Each entry describes an existing codebase that the PRD interview should be
aware of when generating product requirements.

Schema (JSON array)::

    [
        {
            "path": "/absolute/path/to/repo",
            "name": "human-friendly-name",
            "desc": "optional short description"
        }
    ]

All three fields are strings.  ``path`` and ``name`` are required (non-empty);
``desc`` defaults to ``""`` when absent.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

BROWNFIELD_PATH = Path.home() / ".ouroboros" / "brownfield.json"

_REQUIRED_KEYS = {"path", "name"}


# ── Schema dataclass ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BrownfieldEntry:
    """A single brownfield repository entry.

    Attributes:
        path: Absolute filesystem path to the repository root.
        name: Human-friendly project name.
        desc: Optional short description.
    """

    path: str
    name: str
    desc: str = ""

    def to_dict(self) -> dict[str, str]:
        """Serialize to a plain dict for JSON persistence."""
        return {"path": self.path, "name": self.name, "desc": self.desc}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BrownfieldEntry:
        """Create an entry from a dict, raising on missing required keys.

        Args:
            data: Dictionary with at least ``path`` and ``name`` keys.

        Returns:
            Validated BrownfieldEntry.

        Raises:
            ValueError: If required keys are missing or empty.
        """
        missing = _REQUIRED_KEYS - set(data.keys())
        if missing:
            raise ValueError(f"Missing required keys: {sorted(missing)}")

        path = str(data["path"]).strip()
        name = str(data["name"]).strip()

        if not path:
            raise ValueError("'path' must not be empty")
        if not name:
            raise ValueError("'name' must not be empty")

        return cls(path=path, name=name, desc=str(data.get("desc", "")))


# ── Validation ──────────────────────────────────────────────────────


def validate_entries(raw: Any) -> list[BrownfieldEntry]:
    """Validate raw JSON data against the brownfield schema.

    Accepts any value and returns a list of validated
    :class:`BrownfieldEntry` instances.  Invalid entries are logged
    and skipped rather than raising — the registry is best-effort.

    Args:
        raw: Parsed JSON value (should be a list of dicts).

    Returns:
        List of validated entries.

    Raises:
        ValueError: If *raw* is not a list.
    """
    if not isinstance(raw, list):
        raise ValueError(
            f"brownfield.json must be a JSON array, got {type(raw).__name__}"
        )

    entries: list[BrownfieldEntry] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            log.warning(
                "brownfield.skip_non_dict",
                index=idx,
                type=type(item).__name__,
            )
            continue
        try:
            entries.append(BrownfieldEntry.from_dict(item))
        except ValueError as exc:
            log.warning(
                "brownfield.skip_invalid_entry",
                index=idx,
                error=str(exc),
            )
    return entries


# ── File I/O ────────────────────────────────────────────────────────


def load_brownfield_repos(
    filepath: Path | None = None,
) -> list[BrownfieldEntry]:
    """Load the brownfield registry from disk.

    Returns an empty list when the file does not exist or cannot be
    parsed.  Individual entries that fail validation are skipped.

    Args:
        filepath: Path to the brownfield JSON file.  Defaults to
            :data:`BROWNFIELD_PATH`.

    Returns:
        List of validated brownfield entries.
    """
    if filepath is None:
        filepath = BROWNFIELD_PATH
    if not filepath.exists():
        return []

    try:
        text = filepath.read_text(encoding="utf-8")
        raw = json.loads(text)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("brownfield.load_failed", path=str(filepath), error=str(exc))
        return []

    try:
        return validate_entries(raw)
    except ValueError as exc:
        log.warning("brownfield.load_invalid", path=str(filepath), error=str(exc))
        return []


def save_brownfield_repos(
    entries: list[BrownfieldEntry],
    filepath: Path | None = None,
) -> None:
    """Persist the brownfield registry to disk.

    Creates parent directories as needed.

    Args:
        entries: List of entries to save.
        filepath: Path to the brownfield JSON file.  Defaults to
            :data:`BROWNFIELD_PATH`.
    """
    if filepath is None:
        filepath = BROWNFIELD_PATH
    filepath.parent.mkdir(parents=True, exist_ok=True)
    data = [e.to_dict() for e in entries]
    filepath.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.info("brownfield.saved", path=str(filepath), count=len(entries))


def register_brownfield_repo(
    path: str,
    name: str,
    desc: str = "",
    filepath: Path | None = None,
) -> list[BrownfieldEntry]:
    """Register a new brownfield repository (or update an existing one).

    De-duplicates by ``path``: if a repo with the same path exists it is
    replaced with the new entry.

    Args:
        path: Absolute filesystem path to the repo.
        name: Human-friendly name.
        desc: Optional description.
        filepath: Path to the brownfield JSON file.

    Returns:
        Updated list of entries (including the new/updated one).

    Raises:
        ValueError: If *path* or *name* are empty.
    """
    entry = BrownfieldEntry(path=path, name=name, desc=desc)

    repos = load_brownfield_repos(filepath)

    # De-duplicate by path
    repos = [r for r in repos if r.path != path]
    repos.append(entry)

    save_brownfield_repos(repos, filepath)
    return repos


def load_brownfield_repos_as_dicts(
    filepath: Path | None = None,
) -> list[dict[str, str]]:
    """Load brownfield repos and return as plain dicts.

    Convenience wrapper for callers that expect ``list[dict[str, str]]``
    (e.g. PRDInterviewEngine static methods for backward compatibility).

    Args:
        filepath: Path to the brownfield JSON file.

    Returns:
        List of repo dicts with keys: path, name, desc.
    """
    return [e.to_dict() for e in load_brownfield_repos(filepath)]
