"""Offline update nudge for MCP serve (#2066).

The Claude Code hook path checks PyPI at most once per 24h and caches the
result in ``~/.ouroboros/version-check-cache.json`` (written by
``scripts/version-check.py``). This module surfaces that cache as one line
appended to the server's ``instructions`` field with no network call on
the startup hot path.

Hosts without the Claude hook (Codex, OpenCode, Kiro, ...) have no cache
producer of their own, so the ``mcp serve`` startup path — and only that
path, never mere server construction — schedules one bounded background
refresh on a daemon thread when the installed channel has no fresh cached
value: the first start primes the cache off the hot path and a later
start delivers the nudge. The append seam is side-effect-free formatting
and fails open on every error — the nudge is advisory and must never
delay or fail server startup.
"""

from __future__ import annotations

from contextlib import contextmanager
from importlib import metadata
import json
import os
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
# One background refresh per process: repeated serve starts must not stack
# threads or PyPI requests. The event is read and set under the lock so
# concurrent callers cannot both observe it unset.
_REFRESH_LOCK = threading.Lock()
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


def _stamp_is_fresh(stamp: object) -> bool:
    """True when *stamp* is a timestamp within the writer's TTL."""
    if not isinstance(stamp, (int, float)):
        return False
    age = time.time() - stamp
    return 0 <= age < _CACHE_TTL_SECONDS


def _channel_key(installed_is_prerelease: bool) -> str:
    """The cache key for the installed release channel, matching the writer."""
    return "latest_version_pre" if installed_is_prerelease else "latest_version"


_CHANNEL_KEYS = ("latest_version", "latest_version_pre")


@contextmanager
def _cache_write_lock():
    """Serialize the shared cache read/merge/replace transaction."""
    lock_path = _CACHE_FILE.with_name(f"{_CACHE_FILE.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if handle.seek(0, 2) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _channel_is_fresh(payload: dict, channel_key: str) -> bool:
    """Channel-specific freshness (#2066 R3).

    The shared ``timestamp`` cannot attest a channel it did not refresh:
    a stable-channel refresh must not make an older prerelease value look
    fresh. Each channel therefore carries its own ``*_checked_at`` stamp.
    A payload with no per-channel stamps at all was written by the legacy
    single-timestamp writer and keeps the shared-timestamp semantics for
    one TTL window; once any stamp is present, an unstamped channel is
    stale.
    """
    stamp = payload.get(f"{channel_key}_checked_at")
    if stamp is not None:
        return _stamp_is_fresh(stamp)
    if any(payload.get(f"{key}_checked_at") is not None for key in _CHANNEL_KEYS):
        return False
    return _stamp_is_fresh(payload.get("timestamp"))


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
        channel_key = _channel_key(installed_version.is_prerelease)
        if payload is None or not _channel_is_fresh(payload, channel_key):
            return None
        latest = payload.get(channel_key)
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

    Runs on a daemon thread off the startup hot path. Writes the selected
    channel value and freshness stamp while preserving all other keys. The
    complete read/merge/replace transaction shares a cross-process lock with
    scripts/version-check.py, so independent channel writers cannot lose one
    another's update.
    """
    try:
        installed = metadata.version("ouroboros-ai")
        from packaging.version import Version

        include_prerelease = Version(installed).is_prerelease
        latest = _fetch_latest_from_pypi(include_prerelease=include_prerelease)
        if latest is None:
            return
        channel_key = _channel_key(include_prerelease)
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _cache_write_lock():
            payload = _load_cache_payload() or {}
            payload[channel_key] = latest
            # Freshness is attested per channel (#2066 R3): only the refreshed
            # channel gets a new stamp, and the shared legacy timestamp is left
            # alone so this write can never make the OTHER channel's older
            # value look current to any reader.
            payload[f"{channel_key}_checked_at"] = time.time()
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


def _channel_value_usable(payload: dict, channel_key: str) -> bool:
    """True when the selected channel holds a parseable version string."""
    value = payload.get(channel_key)
    if not isinstance(value, str) or not value:
        return False
    try:
        from packaging.version import InvalidVersion, Version

        Version(value)
    except InvalidVersion:
        return False
    return True


def maybe_schedule_cache_refresh() -> None:
    """Schedule one bounded cache refresh from the serve startup path.

    Freshness is channel-specific (#2066 R2): a fresh payload that lacks
    a usable value for the INSTALLED channel — a stable-only cache under
    a prerelease install, or the reverse — still refreshes, so a channel
    transition cannot strand the nudge. The once-per-process gate is
    read and set under a lock, so concurrent callers start exactly one
    thread. Fails open: this must never delay or fail serve startup.
    """
    try:
        if _REFRESH_STARTED.is_set():
            return
        installed = metadata.version("ouroboros-ai")
        from packaging.version import Version

        channel_key = _channel_key(Version(installed).is_prerelease)
        payload = _load_cache_payload()
        if (
            payload is not None
            and _channel_is_fresh(payload, channel_key)
            and _channel_value_usable(payload, channel_key)
        ):
            return
        with _REFRESH_LOCK:
            if _REFRESH_STARTED.is_set():
                return
            _REFRESH_STARTED.set()
        threading.Thread(
            target=_refresh_cache_now,
            name="ouroboros-update-notice-refresh",
            daemon=True,
        ).start()
    except Exception:
        return


def append_cached_update_notice(instructions: str) -> str:
    """Append the cached update nudge to *instructions* when one exists.

    Side-effect-free formatting: the refresh producer is scheduled only
    by ``maybe_schedule_cache_refresh()`` on the serve startup path, so
    non-serving ``create_ouroboros_server()`` callers (auto, detached
    job workers, in-process dispatch) never initiate network activity.
    This is the complete advisory boundary (#2066): any failure —
    including unexpected ``importlib.metadata`` errors — leaves the
    instructions unchanged rather than reaching server construction.
    """
    try:
        notice = cached_update_notice()
    except Exception:
        return instructions
    if notice is None:
        return instructions
    return instructions.rstrip("\n") + "\n\n" + notice + "\n"
