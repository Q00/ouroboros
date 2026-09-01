"""Canonical package-profile identities and compatibility diagnostics."""

from __future__ import annotations

from enum import Enum
from importlib import metadata as importlib_metadata

# uvx resolves tool dependencies against its default interpreter, which can be
# an older system Python even when a compatible one is installed. Every
# generated ``uvx`` launch must carry this floor so uv selects — or downloads —
# an interpreter satisfying the package's requires-python (pyproject.toml).
UVX_PYTHON_FLOOR = ">=3.12"

MCP_PROFILE = "mcp"
CLAUDE_CLI_PROFILE = "claude-cli"
CLAUDE_SDK_COMPAT_PROFILE = "claude"
CLAUDE_SDK_PROFILE = "claude-sdk"
CLAUDE_CLI_RUNTIME_BACKEND = "claude_mcp"
CLAUDE_SDK_RUNTIME_BACKEND = "claude"

UNSUPPORTED_CLAUDE_SDK_MCP_MESSAGE = (
    "Unsupported package profiles: ouroboros-ai[mcp] requires MCP 2, while "
    "ouroboros-ai[claude] and ouroboros-ai[claude-sdk] require MCP 1.x. "
    "Use [claude] alone for the Claude SDK runtime, or use [claude-cli] with "
    "[mcp]; run the MCP 2 server in a separate environment/process."
)

GJC_MCP_V2_INSTALL_MESSAGE = (
    "GJC requires the Ouroboros MCP v2 profile. Reinstall this environment with "
    "'ouroboros-ai[mcp]' before running setup; keep it separate from the MCP 1.x "
    "[claude] and [claude-sdk] profiles."
)


SDK_RUNTIME_IN_MCP_SERVER_MESSAGE = (
    "The MCP server cannot host the 'claude' SDK runtime in this process. "
    "Pass an executable runtime, for example "
    "'ouroboros mcp serve --runtime claude-cli', or set the runtime in config "
    "so this command stops inheriting the 'claude' default."
)


class PublicAgentRuntimeBackend(str, Enum):  # noqa: UP042
    """Public runtime spellings shared by every workflow CLI front door.

    Package-profile names are public aliases here: ``claude-sdk`` selects the
    canonical ``claude`` SDK backend and ``claude-cli`` selects the canonical
    ``claude_mcp`` worker. Internal transports such as ``codex_mcp`` stay out
    of this user-facing contract.
    """

    CLAUDE = "claude"
    CLAUDE_SDK = "claude-sdk"
    CLAUDE_CLI = "claude-cli"
    CODEX = "codex"
    OPENCODE = "opencode"
    HERMES = "hermes"
    GEMINI = "gemini"
    COPILOT = "copilot"
    GOOSE = "goose"
    KIRO = "kiro"
    PI = "pi"
    OMP = "omp"
    GJC = "gjc"
    ANTIGRAVITY = "antigravity"
    GROK = "grok"
    ZCODE = "zcode"
    HOST = "host"


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


def has_pinned_mcp_v2_profile() -> bool:
    """Return whether this interpreter has Ouroboros's exact MCP v2 client pin."""
    try:
        return importlib_metadata.version("mcp") == "2.0.0"
    except importlib_metadata.PackageNotFoundError:
        return False


def gjc_mcp_v2_profile_error() -> str | None:
    """Return the actionable GJC profile error, or ``None`` when ready."""
    return None if has_pinned_mcp_v2_profile() else GJC_MCP_V2_INSTALL_MESSAGE


def public_runtime_backend(runtime: str | None) -> str | None:
    """Map the public Claude profile name to its internal transport backend.

    Public ``claude`` preserves the historical Agent SDK runtime. The explicit
    ``claude-cli`` spelling selects the dependency-free subprocess worker used
    by an isolated MCP 2 server.
    """
    if runtime is None:
        return None
    normalized = runtime.strip().lower()
    if normalized == CLAUDE_CLI_PROFILE:
        return CLAUDE_CLI_RUNTIME_BACKEND
    if normalized in {CLAUDE_SDK_COMPAT_PROFILE, CLAUDE_SDK_PROFILE, "claude_sdk"}:
        return CLAUDE_SDK_RUNTIME_BACKEND
    return normalized


__all__ = [
    "CLAUDE_CLI_PROFILE",
    "CLAUDE_CLI_RUNTIME_BACKEND",
    "CLAUDE_SDK_COMPAT_PROFILE",
    "CLAUDE_SDK_PROFILE",
    "CLAUDE_SDK_RUNTIME_BACKEND",
    "MCP_PROFILE",
    "SDK_RUNTIME_IN_MCP_SERVER_MESSAGE",
    "PublicAgentRuntimeBackend",
    "GJC_MCP_V2_INSTALL_MESSAGE",
    "UNSUPPORTED_CLAUDE_SDK_MCP_MESSAGE",
    "has_unsupported_claude_sdk_mcp_mix",
    "gjc_mcp_v2_profile_error",
    "has_pinned_mcp_v2_profile",
    "public_runtime_backend",
]
