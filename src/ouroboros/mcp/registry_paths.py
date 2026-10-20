"""Filesystem locations owned by the MCP server process."""

from pathlib import Path


def owned_mcp_pid_registry_dir() -> Path:
    """Return the MCP server registry using the platform's normal home lookup."""
    return Path.home() / ".ouroboros" / "mcp-servers"


def diagnostic_mcp_location() -> tuple[Path | None, Path | None]:
    """Keep the private home for redaction even when registry access is invalid."""
    try:
        registry_dir = owned_mcp_pid_registry_dir()
        home = registry_dir.parent.parent
        if not registry_dir.is_absolute() or "\x00" in str(registry_dir):
            return home, None
    except (KeyError, OSError, RuntimeError, ValueError):
        return None, None
    return home, registry_dir


def diagnostic_mcp_pid_registry_dir() -> Path | None:
    """Use the server's home location without exposing it in diagnostic output."""
    return diagnostic_mcp_location()[1]


__all__ = [
    "diagnostic_mcp_location",
    "diagnostic_mcp_pid_registry_dir",
    "owned_mcp_pid_registry_dir",
]
