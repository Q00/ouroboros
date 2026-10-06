"""Filesystem locations owned by the MCP server process."""

import os
from pathlib import Path
from typing import Any


def _user_home_directory() -> Path:
    """Resolve the account home without consulting process environment variables."""
    if os.name == "nt":
        import ctypes

        return _windows_home_directory(ctypes.windll.shell32)

    return _posix_home_directory()


def _windows_home_directory(shell32: Any) -> Path:
    """Resolve the profile path through the Windows known-folder API."""
    import ctypes
    from ctypes import wintypes

    profile = ctypes.create_unicode_buffer(32768)
    shell32.SHGetFolderPathW.argtypes = [
        wintypes.HWND,
        ctypes.c_int,
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
    ]
    shell32.SHGetFolderPathW.restype = ctypes.c_long
    result = shell32.SHGetFolderPathW(None, 40, None, 0, profile)
    if result != 0 or not profile.value:
        raise OSError(result, "Could not resolve the current user's profile directory")
    home = Path(profile.value)
    if not home.is_absolute():
        raise OSError("Current user's profile directory is not absolute")
    return home


def _posix_home_directory(
    passwd_path: Path = Path("/etc/passwd"), *, user_id: int | None = None
) -> Path:
    """Read the current account's home from the local passwd file only."""
    max_passwd_bytes = 1_048_576
    with passwd_path.open("rb") as passwd_file:
        if os.fstat(passwd_file.fileno()).st_size > max_passwd_bytes:
            raise OSError("Local passwd file exceeds the read limit")
        contents = passwd_file.read(max_passwd_bytes + 1)
    if len(contents) > max_passwd_bytes:
        raise OSError("Local passwd file exceeds the read limit")

    account_id = str(os.getuid() if user_id is None else user_id)
    for raw_line in contents.splitlines():
        fields = raw_line.split(b":")
        if len(fields) < 7 or fields[2] != account_id.encode("ascii"):
            continue
        value = os.fsdecode(fields[5])
        home = Path(value)
        if value and home.is_absolute():
            return home
        raise OSError("Could not resolve an absolute account home directory")
    raise OSError("No matching local passwd entry for the current user")


def owned_mcp_pid_registry_dir() -> Path:
    """Return the MCP server registry using the platform's normal home lookup."""
    return Path.home() / ".ouroboros" / "mcp-servers"


def diagnostic_mcp_pid_registry_dir() -> Path | None:
    """Resolve the registry locally without environment or account-service lookup."""
    try:
        home = _user_home_directory()
    except (KeyError, OSError):
        return None
    return home / ".ouroboros" / "mcp-servers"


__all__ = ["diagnostic_mcp_pid_registry_dir", "owned_mcp_pid_registry_dir"]
