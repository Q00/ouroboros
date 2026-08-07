"""Claude CLI/SDK profile activation for ``ouroboros setup``."""

from pathlib import Path

import typer
import yaml

from ouroboros.cli.formatters.panels import print_error, print_info, print_success, print_warning
from ouroboros.package_profiles import (
    UNSUPPORTED_CLAUDE_SDK_MCP_MESSAGE,
    has_unsupported_claude_sdk_mcp_mix,
)


def _write_claude_runtime_config(claude_path: str, *, runtime_backend: str) -> Path:
    """Persist one Claude transport choice without touching host MCP config."""
    from ouroboros.config.loader import create_default_config, ensure_config_dir

    config_dir = ensure_config_dir()
    config_path = config_dir / "config.yaml"
    if config_path.exists():
        config_dict = yaml.safe_load(config_path.read_text()) or {}
    else:
        create_default_config(config_dir)
        config_dict = yaml.safe_load(config_path.read_text()) or {}

    config_dict.setdefault("orchestrator", {})
    config_dict["orchestrator"]["runtime_backend"] = runtime_backend
    config_dict["orchestrator"]["cli_path"] = claude_path
    config_dict.setdefault("llm", {})
    config_dict["llm"]["backend"] = "claude"

    with config_path.open("w") as file:
        yaml.dump(config_dict, file, default_flow_style=False, sort_keys=False)
    return config_path


def setup_claude(claude_path: str) -> None:
    """Configure the default isolated Claude Agent SDK package profile."""
    if has_unsupported_claude_sdk_mcp_mix():
        print_error(UNSUPPORTED_CLAUDE_SDK_MCP_MESSAGE)
        raise typer.Exit(1)

    config_path = _write_claude_runtime_config(claude_path, runtime_backend="claude")
    print_warning(
        "Claude SDK uses MCP 1.x in this environment. The Ouroboros MCP 2 server "
        "must run through its isolated ouroboros-ai[mcp] launcher."
    )
    print_success(f"Configured Claude SDK runtime (CLI: {claude_path})")
    print_info("Package profile: ouroboros-ai[claude] (SDK/MCP 1.x)")
    print_info(f"Config saved to: {config_path}")


def setup_claude_sdk(claude_path: str) -> None:
    """Configure the explicit alias for the Claude Agent SDK profile."""
    setup_claude(claude_path)


def setup_claude_cli(claude_path: str) -> None:
    """Configure the dependency-free CLI worker used with an MCP 2 server."""
    config_path = _write_claude_runtime_config(claude_path, runtime_backend="claude_mcp")
    print_success(f"Configured Claude CLI runtime (CLI: {claude_path})")
    print_info("Package profile: ouroboros-ai[claude-cli] (MCP 2 compatible)")
    print_info(f"Config saved to: {config_path}")


__all__ = ["setup_claude", "setup_claude_cli", "setup_claude_sdk"]
