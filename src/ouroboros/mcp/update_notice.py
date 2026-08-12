"""Offline update nudge for MCP serve (#2066).

The Claude Code hook path already checks PyPI at most once per 24h and
caches the result in ``~/.ouroboros/version-check-cache.json`` (written by
``scripts/version-check.py``). Every other MCP host starts the server and
learns nothing about updates. This module surfaces that same cache — and
only the cache — as one line appended to the server's ``instructions``
field, so every MCP runtime gets the nudge with no network call on the
startup hot path. A missing, stale, or malformed cache yields no notice:
the nudge is advisory and must never delay or fail server startup.
"""

from __future__ import annotations

from importlib import metadata
import json
from pathlib import Path
import time

_CACHE_FILE = Path.home() / ".ouroboros" / "version-check-cache.json"
# Mirrors the writer's TTL (scripts/version-check.py): entries older than
# this are the writer's responsibility to refresh, not ours to trust.
_CACHE_TTL_SECONDS = 86400


def _cached_latest_version(installed_is_prerelease: bool) -> str | None:
    """Read the fresh cached latest version, or None when unusable."""
    try:
        raw = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    timestamp = raw.get("timestamp")
    if not isinstance(timestamp, (int, float)):
        return None
    age = time.time() - timestamp
    if not 0 <= age < _CACHE_TTL_SECONDS:
        return None
    key = "latest_version_pre" if installed_is_prerelease else "latest_version"
    latest = raw.get(key)
    if isinstance(latest, str) and latest:
        return latest
    return None


def cached_update_notice() -> str | None:
    """One-line update nudge from the 24h version cache, or None."""
    try:
        installed = metadata.version("ouroboros-ai")
    except metadata.PackageNotFoundError:
        return None
    try:
        from packaging.version import InvalidVersion, Version

        installed_version = Version(installed)
        latest = _cached_latest_version(installed_version.is_prerelease)
        if latest is None or Version(latest) <= installed_version:
            return None
    except InvalidVersion:
        return None
    return (
        f'Ouroboros {latest} is available (installed: {installed}). Suggest running "ooo update".'
    )


def append_cached_update_notice(instructions: str) -> str:
    """Append the cached update nudge to *instructions* when one exists."""
    notice = cached_update_notice()
    if notice is None:
        return instructions
    return instructions.rstrip("\n") + "\n\n" + notice + "\n"
