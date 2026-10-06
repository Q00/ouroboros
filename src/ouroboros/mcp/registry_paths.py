"""Filesystem locations owned by the MCP server process."""

from pathlib import Path


def owned_mcp_pid_registry_dir() -> Path:
    """Return the MCP server registry using the platform's normal home lookup."""
    return Path.home() / ".ouroboros" / "mcp-servers"


def diagnostic_mcp_pid_registry_dir() -> Path | None:
    """Use the server's home location without exposing it in diagnostic output."""
    try:
        registry_dir = owned_mcp_pid_registry_dir()
        if not registry_dir.is_absolute() or "\x00" in str(registry_dir):
            return None
    except (KeyError, OSError, RuntimeError, ValueError):
        return None
    return registry_dir


__all__ = ["diagnostic_mcp_pid_registry_dir", "owned_mcp_pid_registry_dir"]
