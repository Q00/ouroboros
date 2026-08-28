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
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import subprocess

# `git clone` reaches the network, so it gets a generous ceiling; `git rev-parse`
# is a local object-store read that either answers immediately or is wedged.
# Both are bounded because `capture_output=True` hides a credential prompt: an
# untimed call turns an unreachable host into a silent, permanent hang of
# `ooo plugin add`.
GIT_CLONE_TIMEOUT_SECONDS = 300
GIT_REV_PARSE_TIMEOUT_SECONDS = 30


def _ssh_command_with_batch_mode(existing: str | None) -> str:
    """Build a GIT_SSH_COMMAND that preserves caller settings and adds BatchMode.

    When the caller has already set GIT_SSH_COMMAND (e.g. for a custom
    identity file, proxy jump, or nonstandard port), we append
    ``-oBatchMode=yes`` so the SSH transport fails on prompts without
    discarding the operator's transport configuration.

    If no GIT_SSH_COMMAND is inherited, we fall back to the minimal
    ``ssh -oBatchMode=yes``.

    The option is appended using shlex.quote on the existing value is
    unnecessary — ``-oBatchMode=yes`` is a literal flag with no
    shell-metacharacter content — and the existing command is already a
    shell-interpreted string (Git invokes it via ``sh -c "$GIT_SSH_COMMAND"
    host``). We therefore concatenate with a single space, which is safe
    because ``-oBatchMode=yes`` contains no characters requiring quoting.
    """
    if not existing:
        return "ssh -oBatchMode=yes"
    # If the caller already specifies BatchMode, respect it as-is.
    if "-oBatchMode=" in existing or "-o BatchMode=" in existing:
        return existing
    return f"{existing} -oBatchMode=yes"


def git_noninteractive_env() -> dict[str, str]:
    """Environment that makes Git fail instead of prompting for credentials.

    `GIT_TERMINAL_PROMPT=0` is Git's own kill switch for terminal prompts.
    Pointing `GIT_ASKPASS`/`SSH_ASKPASS` at `true` stops a GUI helper from
    waiting on input off-screen, and `ssh -oBatchMode=yes` does the same for
    the SSH transport.  Combined with a `DEVNULL` stdin, a private or moved
    repository fails fast with a real stderr message instead of blocking on a
    prompt nobody can see.

    Caller-supplied ``GIT_SSH_COMMAND`` is preserved: we append
    ``-oBatchMode=yes`` rather than replacing the entire value, so custom
    identity files, proxy jumps, wrapper scripts, and other transport
    settings continue to work.
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "true"
    env["SSH_ASKPASS"] = "true"
    env["GIT_SSH_COMMAND"] = _ssh_command_with_batch_mode(os.environ.get("GIT_SSH_COMMAND"))
    return env


class CacheRefreshRecoveryError(OSError):
    """Promotion failed and the previous cache remains at ``backup_path``."""

    def __init__(self, message: str, *, backup_path: Path) -> None:
        super().__init__(message)
        self.backup_path = backup_path


def url_cache_refresh_error(exc: subprocess.SubprocessError | OSError, dest: Path) -> str:
    """Render clone and filesystem failures through one public CLI contract."""
    if isinstance(exc, subprocess.TimeoutExpired):
        cmd = exc.cmd if isinstance(exc.cmd, str) else " ".join(str(part) for part in exc.cmd)
        return (
            f"git command timed out after {exc.timeout}s: {cmd}. "
            "The remote may be unreachable or the repository may require "
            "credentials that cannot be prompted for non-interactively."
        )
    if isinstance(exc, subprocess.CalledProcessError):
        detail = exc.stderr.strip() if exc.stderr else exc
        return f"git clone failed: {detail}"
    if isinstance(exc, CacheRefreshRecoveryError):
        return (
            f"plugin cache refresh failed at {dest}: {exc}. "
            f"The previous cache is retained at {exc.backup_path}; "
            f"restore it to {dest} before retrying."
        )
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
    # Do not extend the destination basename: a valid cache component may
    # already be near NAME_MAX. A bounded digest keeps temporary siblings
    # attributable to the destination without inheriting its length.
    identity = hashlib.sha256(os.fsencode(clone_dest.name)).hexdigest()[:12]
    sibling_prefix = f".ouroboros-cache-{identity}"
    staging = clone_dest.with_name(f"{sibling_prefix}.staging-{suffix}")
    backup = clone_dest.with_name(f"{sibling_prefix}.bak-{suffix}")

    try:
        git_sha = clone(staging)
    except BaseException:
        # BaseException so a user interrupt during the clone also drops the
        # partial staging tree; the live cache is untouched either way.
        shutil.rmtree(staging, ignore_errors=True)
        raise

    backup_used = False
    try:
        if clone_dest.exists():
            # Ownership is recorded before the rename: an interrupt landing
            # between the rename's side effect and this flag would otherwise
            # skip restoration and strand the cache under the backup name.
            # The handler's ``backup.exists()`` guard keeps the early flag
            # harmless when the rename itself never happened.
            backup_used = True
            os.rename(clone_dest, backup)
        os.rename(staging, clone_dest)
    except BaseException as exc:
        # The live cache may already be parked at ``backup`` here. User
        # interruption must take the same rollback path as an OSError rather
        # than strand the public destination and staging directory.
        recovery_error: CacheRefreshRecoveryError | None = None
        if backup_used and not clone_dest.exists() and backup.exists():
            try:
                os.rename(backup, clone_dest)
            except OSError as restore_error:
                # Restore failed; the backup stays on disk for manual
                # recovery rather than being deleted with the staging tree.
                if isinstance(exc, Exception):
                    recovery_error = CacheRefreshRecoveryError(
                        f"promotion failed ({exc}); automatic restoration failed ({restore_error})",
                        backup_path=backup,
                    )
        shutil.rmtree(staging, ignore_errors=True)
        if recovery_error is not None:
            raise recovery_error from exc
        raise

    # The old cache is gone from its live path; dropping the backup is
    # best-effort cleanup, not a correctness step.
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    return git_sha
