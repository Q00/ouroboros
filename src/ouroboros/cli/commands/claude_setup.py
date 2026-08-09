"""Claude CLI/SDK profile activation for ``ouroboros setup``."""

from pathlib import Path
from typing import Literal

from rich.markup import escape
import typer

from ouroboros.cli.formatters.panels import print_error, print_info, print_success, print_warning
from ouroboros.package_profiles import (
    UNSUPPORTED_CLAUDE_SDK_MCP_MESSAGE,
    has_unsupported_claude_sdk_mcp_mix,
)


def _activate_claude_runtime_config(
    claude_path: str,
    *,
    runtime_backend: Literal["claude", "claude_mcp"],
) -> Path | None:
    """Atomically persist one Claude transport without touching host MCP config."""
    from ouroboros.cli.runtime_activation import activate_claude_runtime

    return activate_claude_runtime(claude_path, runtime_backend=runtime_backend)


def _setup_claude_sdk_profile(claude_path: str, *, profile: str) -> bool:
    """Configure one public spelling of the isolated Agent SDK profile."""
    if has_unsupported_claude_sdk_mcp_mix():
        print_error(escape(UNSUPPORTED_CLAUDE_SDK_MCP_MESSAGE))
        raise typer.Exit(1)

    config_path = _activate_claude_runtime_config(claude_path, runtime_backend="claude")
    if config_path is None:
        return False
    print_warning(
        escape(
            "Claude SDK uses MCP 1.x in this environment. The Ouroboros MCP 2 server "
            "must run through its isolated ouroboros-ai[mcp] launcher."
        )
    )
    print_success(f"Configured Claude SDK runtime (CLI: {claude_path})")
    print_info(escape(f"Package profile: ouroboros-ai[{profile}] (SDK/MCP 1.x)"))
    print_info(f"Config saved to: {config_path}")
    return True


def setup_claude(claude_path: str) -> bool:
    """Configure the default isolated Claude Agent SDK package profile."""
    return _setup_claude_sdk_profile(claude_path, profile="claude")


def setup_claude_sdk(claude_path: str) -> bool:
    """Configure the explicit alias for the Claude Agent SDK profile."""
    return _setup_claude_sdk_profile(claude_path, profile="claude-sdk")


def setup_claude_cli(claude_path: str) -> bool:
    """Configure the dependency-free CLI worker used with an MCP 2 server."""
    config_path = _activate_claude_runtime_config(claude_path, runtime_backend="claude_mcp")
    if config_path is None:
        return False
    print_success(f"Configured Claude CLI runtime (CLI: {claude_path})")
    print_info(escape("Package profile: ouroboros-ai[claude-cli] (MCP 2 compatible)"))
    print_info(f"Config saved to: {config_path}")
    return True


__all__ = ["setup_claude", "setup_claude_cli", "setup_claude_sdk"]
