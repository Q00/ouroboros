"""Canonical package-profile identities and compatibility diagnostics."""

from __future__ import annotations

from importlib import metadata as importlib_metadata

MCP_PROFILE = "mcp"
CLAUDE_CLI_PROFILE = "claude-cli"
CLAUDE_CLI_COMPAT_PROFILE = "claude"
CLAUDE_SDK_PROFILE = "claude-sdk"
CLAUDE_CLI_RUNTIME_BACKEND = "claude_mcp"
CLAUDE_SDK_RUNTIME_BACKEND = "claude"

UNSUPPORTED_CLAUDE_SDK_MCP_MESSAGE = (
    "Unsupported package profiles: ouroboros-ai[mcp] requires MCP 2, while "
    "ouroboros-ai[claude-sdk] requires MCP 1.x. Use ouroboros-ai[claude] "
    "(or [claude-cli]) with [mcp], or install [claude-sdk] in a separate environment."
)


def _installed_major(distribution: str) -> int | None:
    """Return an installed distribution's major version, if it is knowable."""
    try:
        version = importlib_metadata.version(distribution)
    except importlib_metadata.PackageNotFoundError:
        return None
    try:
        return int(version.split(".", maxsplit=1)[0])
    except ValueError:
        return None


def has_unsupported_claude_sdk_mcp_mix() -> bool:
    """Detect an environment forced past the MCP 2 / Claude SDK resolver guard."""
    return _installed_major("mcp") == 2 and _installed_major("claude-agent-sdk") is not None


def public_runtime_backend(runtime: str | None) -> str | None:
    """Map the public Claude profile name to its internal transport backend.

    Public CLI commands use ``claude`` for the ordinary dependency-free CLI
    profile. Persisted configuration keeps the historical internal names so a
    legacy ``runtime_backend: claude`` can still identify the SDK runtime.
    """
    if runtime is None:
        return None
    normalized = runtime.strip().lower()
    if normalized in {CLAUDE_CLI_COMPAT_PROFILE, CLAUDE_CLI_PROFILE}:
        return CLAUDE_CLI_RUNTIME_BACKEND
    if normalized in {CLAUDE_SDK_PROFILE, "claude_sdk"}:
        return CLAUDE_SDK_RUNTIME_BACKEND
    return normalized


__all__ = [
    "CLAUDE_CLI_COMPAT_PROFILE",
    "CLAUDE_CLI_PROFILE",
    "CLAUDE_CLI_RUNTIME_BACKEND",
    "CLAUDE_SDK_PROFILE",
    "CLAUDE_SDK_RUNTIME_BACKEND",
    "MCP_PROFILE",
    "UNSUPPORTED_CLAUDE_SDK_MCP_MESSAGE",
    "has_unsupported_claude_sdk_mcp_mix",
    "public_runtime_backend",
]
