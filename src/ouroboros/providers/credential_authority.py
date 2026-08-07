"""Installation-held authority for secret-safe frugality identities."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import secrets
import stat

from ouroboros.config.models import get_config_dir

INSTALLATION_AUTHORITY_KEY_BYTES = 32
_INSTALLATION_AUTHORITY_KEY_FILE = "frugality-authority.key"
_CREDENTIAL_AUTHORITY_CONTEXT = b"ouroboros-frugality-credential-authority-v2"
_MAX_CREDENTIAL_BYTES = 128 * 1_024


def _read_installation_authority_key(path: Path) -> bytes | None:
    """Read one owner-only regular key file without following symlinks."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except (FileNotFoundError, OSError):
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            return None
        getuid = getattr(os, "getuid", None)
        if callable(getuid) and metadata.st_uid != getuid():
            return None
        value = os.read(descriptor, INSTALLATION_AUTHORITY_KEY_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    return value if len(value) == INSTALLATION_AUTHORITY_KEY_BYTES else None


def _remove_recoverable_partial_key(path: Path) -> bool:
    """Remove only an owner-only regular partial artifact at the exact path."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        opened = os.fstat(descriptor)
        getuid = getattr(os, "getuid", None)
        owner_matches = not callable(getuid) or opened.st_uid == getuid()
        recoverable = (
            stat.S_ISREG(opened.st_mode)
            and owner_matches
            and not stat.S_IMODE(opened.st_mode) & 0o077
            and opened.st_size != INSTALLATION_AUTHORITY_KEY_BYTES
        )
        current = path.lstat()
        if not recoverable or (current.st_dev, current.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            return False
        path.unlink()
        return True
    except (FileNotFoundError, OSError):
        return False
    finally:
        os.close(descriptor)


def _fsync_directory(directory: Path) -> bool:
    """Durably publish one directory entry when the platform permits it."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        return False
    return True


def installation_authority_key() -> bytes | None:
    """Load or transactionally provision the installation-held proof HMAC key."""
    directory = get_config_dir()
    path = directory / _INSTALLATION_AUTHORITY_KEY_FILE
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink():
            return None
        existing = _read_installation_authority_key(path)
        if existing is not None:
            return existing
        if os.path.lexists(path) and not _remove_recoverable_partial_key(path):
            return None
        candidate = secrets.token_bytes(INSTALLATION_AUTHORITY_KEY_BYTES)
        temporary_path = directory / (
            f".{_INSTALLATION_AUTHORITY_KEY_FILE}.{secrets.token_hex(16)}.tmp"
        )
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(temporary_path, flags, 0o600)
    except (OSError, RuntimeError, ValueError):
        return None
    write_complete = False
    try:
        remaining = memoryview(candidate)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("installation authority key write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
        write_complete = True
    except OSError:
        pass
    finally:
        os.close(descriptor)
    if not write_complete:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    try:
        os.link(temporary_path, path, follow_symlinks=False)
    except FileExistsError:
        return _read_installation_authority_key(path)
    except OSError:
        return None
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
    if not _fsync_directory(directory):
        return None
    return _read_installation_authority_key(path)


def credential_authority_digest(
    secret: str | None,
    *,
    installation_key: bytes | None = None,
) -> str | None:
    """Identify a credential without creating an offline guess verifier."""
    if not isinstance(secret, str) or not secret.strip():
        return None
    authority_key = (
        installation_key if installation_key is not None else installation_authority_key()
    )
    if (
        not isinstance(authority_key, bytes)
        or len(authority_key) != INSTALLATION_AUTHORITY_KEY_BYTES
    ):
        return None
    try:
        encoded_secret = secret.encode("utf-8")
    except UnicodeError:
        return None
    if len(encoded_secret) > _MAX_CREDENTIAL_BYTES:
        return None
    message = b"\x00".join((_CREDENTIAL_AUTHORITY_CONTEXT, encoded_secret))
    digest = hmac.new(authority_key, message, hashlib.sha256).hexdigest()
    return f"hmac-sha256-installation-key-v2:{digest}"


__all__ = [
    "INSTALLATION_AUTHORITY_KEY_BYTES",
    "credential_authority_digest",
    "installation_authority_key",
]
