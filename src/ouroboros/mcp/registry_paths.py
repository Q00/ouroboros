"""Filesystem locations owned by the MCP server process."""

from pathlib import Path


def owned_mcp_pid_registry_dir() -> Path:
    """Return the MCP server's per-instance PID registry directory."""
    return Path.home() / ".ouroboros" / "mcp-servers"


__all__ = ["owned_mcp_pid_registry_dir"]
