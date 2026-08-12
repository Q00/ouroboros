"""Offline update nudge for MCP serve (#2066).

The Claude Code hook path checks PyPI at most once per 24h and caches the
result in ``~/.ouroboros/version-check-cache.json`` (written by
``scripts/version-check.py``). This module surfaces that cache as one line
appended to the server's ``instructions`` field with no network call on
the startup hot path.

Hosts without the Claude hook (Codex, OpenCode, Kiro, ...) have no cache
producer of their own, so a serve start that finds the cache missing or
stale also schedules one bounded background refresh on a daemon thread:
the first start primes the cache off the hot path and a later start
delivers the nudge. The append seam fails open on every error — the
nudge is advisory and must never delay or fail server startup.
"""

from __future__ import annotations

from importlib import metadata
import json
from pathlib import Path
import tempfile
import threading
import time
import urllib.request

_CACHE_FILE = Path.home() / ".ouroboros" / "version-check-cache.json"
# Mirrors the writer's TTL (scripts/version-check.py): entries older than
# this are refreshed, not trusted.
_CACHE_TTL_SECONDS = 86400
_PYPI_JSON_URL = "https://pypi.org/pypi/ouroboros-ai/json"
_REFRESH_TIMEOUT_SECONDS = 5
# One background refresh per process: repeated server constructions must
# not stack threads or PyPI requests.
_REFRESH_STARTED = threading.Event()


def _load_cache_payload() -> dict | None:
    """Parse the cache file, or None when missing or malformed."""
    try:
        raw = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def _cache_is_fresh(payload: dict) -> bool:
    """True when the cache timestamp is within the writer's TTL."""
    timestamp = payload.get("timestamp")
    if not isinstance(timestamp, (int, float)):
        return False
    age = time.time() - timestamp
    return 0 <= age < _CACHE_TTL_SECONDS


def _channel_key(installed_is_prerelease: bool) -> str:
    """The cache key for the installed release channel, matching the writer."""
    return "latest_version_pre" if installed_is_prerelease else "latest_version"


def cached_update_notice() -> str | None:
    """One-line update nudge from the 24h version cache, or None."""
    try:
        installed = metadata.version("ouroboros-ai")
    except metadata.PackageNotFoundError:
        return None
    try:
        from packaging.version import InvalidVersion, Version

        installed_version = Version(installed)
        payload = _load_cache_payload()
        if payload is None or not _cache_is_fresh(payload):
            return None
        latest = payload.get(_channel_key(installed_version.is_prerelease))
        if not isinstance(latest, str) or not latest:
            return None
        if Version(latest) <= installed_version:
            return None
    except InvalidVersion:
        return None
    return (
        f'Ouroboros {latest} is available (installed: {installed}). Suggest running "ooo update".'
    )


def _fetch_latest_from_pypi(*, include_prerelease: bool) -> str | None:
    """One bounded PyPI query, mirroring scripts/version-check.py."""
    import ssl

    try:
        context = ssl.create_default_context()
    except Exception:
        return None
    with urllib.request.urlopen(  # noqa: S310
        _PYPI_JSON_URL, timeout=_REFRESH_TIMEOUT_SECONDS, context=context
    ) as response:
        data = json.loads(response.read())
    if not include_prerelease:
        return data["info"]["version"]
    from packaging.version import Version

    all_versions = [Version(v) for v in data.get("releases", {}) if data["releases"][v]]
    if not all_versions:
        return data["info"]["version"]
    return str(max(all_versions))


def _refresh_cache_now() -> None:
    """Refresh the shared cache once; every failure is swallowed.

    Runs on a daemon thread off the startup hot path. Writes the same
    keys as scripts/version-check.py — the channel value plus the shared
    timestamp — preserving any other keys, with the writer's atomic
    tempfile-then-replace so a concurrent hook-side refresh cannot be
    torn.
    """
    try:
        installed = metadata.version("ouroboros-ai")
        from packaging.version import Version

        include_prerelease = Version(installed).is_prerelease
        latest = _fetch_latest_from_pypi(include_prerelease=include_prerelease)
        if latest is None:
            return
        payload = _load_cache_payload() or {}
        payload[_channel_key(include_prerelease)] = latest
        payload["timestamp"] = time.time()
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=_CACHE_FILE.parent, suffix=".tmp")
        try:
            with open(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload))
            Path(tmp_path).replace(_CACHE_FILE)
        except BaseException:
            Path(tmp_path).unlink(missing_ok=True)
            raise
    except Exception:
        return


def _start_background_refresh_if_stale() -> None:
    """Schedule one bounded cache refresh when no fresh cache exists."""
    if _REFRESH_STARTED.is_set():
        return
    payload = _load_cache_payload()
    if payload is not None and _cache_is_fresh(payload):
        return
    _REFRESH_STARTED.set()
    threading.Thread(
        target=_refresh_cache_now,
        name="ouroboros-update-notice-refresh",
        daemon=True,
    ).start()


def append_cached_update_notice(instructions: str) -> str:
    """Append the cached update nudge to *instructions* when one exists.

    This is the complete advisory boundary (#2066): any failure —
    including unexpected ``importlib.metadata`` errors — leaves the
    instructions unchanged rather than reaching server construction.
    """
    try:
        notice = cached_update_notice()
        _start_background_refresh_if_stale()
    except Exception:
        return instructions
    if notice is None:
        return instructions
    return instructions.rstrip("\n") + "\n\n" + notice + "\n"
