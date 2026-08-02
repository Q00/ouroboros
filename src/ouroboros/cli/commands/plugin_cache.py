"""Staged refresh of URL-backed plugin source caches.

Both URL entry points (``ooo plugin add <repo-url>`` and
``ooo plugin install <name> --from <repo-url>``) cache their clone under
``cache_root/<sanitized-host-path>``. They used to delete that directory
before attempting the replacement clone, so a transient clone failure
destroyed the last-known-good cache (#1826). The refresh here clones into
a staging sibling and promotes it over the live cache only after the clone
has fully succeeded, mirroring the atomic-swap discipline the plugin_home
installs already follow.
"""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import secrets
import shutil
import subprocess


def url_cache_refresh_error(exc: subprocess.CalledProcessError | OSError, dest: Path) -> str:
    """Render clone and filesystem failures through one public CLI contract."""
    if isinstance(exc, subprocess.CalledProcessError):
        detail = exc.stderr.strip() if exc.stderr else exc
        return f"git clone failed: {detail}"
    return (
        f"plugin cache refresh failed at {dest}: {exc}. "
        "The previous cache was preserved when recovery was possible; "
        "check sibling .bak-* directories before retrying."
    )


def url_cache_destination(cache_root: Path, repo_url: str) -> Path:
    """Map a repository URL to its sanitized cache directory."""
    sanitized = (
        repo_url.replace("https://", "")
        .replace("http://", "")
        .replace("git@", "")
        .replace(":", "_")
        .replace("/", "_")
        .strip("_")
    )
    return cache_root / sanitized


def stage_url_cache_refresh(clone: Callable[[Path], str], clone_dest: Path) -> str:
    """Refresh ``clone_dest`` from a staged clone; keep the old cache on failure.

    ``clone`` populates the staging directory and returns the resolved git
    SHA. Only after it succeeds is the previous cache renamed aside and the
    staging tree promoted — both renames are atomic because staging and
    backup are siblings of the destination on the same filesystem. Any
    failure drops the staging tree, restores the prior cache, and re-raises,
    so a failed refresh leaves the last-known-good bytes untouched.
    """
    clone_dest.parent.mkdir(parents=True, exist_ok=True)
    suffix = secrets.token_hex(6)
    staging = clone_dest.with_name(f"{clone_dest.name}.staging-{suffix}")
    backup = clone_dest.with_name(f"{clone_dest.name}.bak-{suffix}")

    try:
        git_sha = clone(staging)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    backup_used = False
    try:
        if clone_dest.exists():
            os.rename(clone_dest, backup)
            backup_used = True
        os.rename(staging, clone_dest)
    except Exception:
        if backup_used and not clone_dest.exists() and backup.exists():
            try:
                os.rename(backup, clone_dest)
            except OSError:
                # Restore failed; the backup stays on disk for manual
                # recovery rather than being deleted with the staging tree.
                pass
        shutil.rmtree(staging, ignore_errors=True)
        raise

    # The old cache is gone from its live path; dropping the backup is
    # best-effort cleanup, not a correctness step.
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    return git_sha
