"""Setup command for Ouroboros.

Standalone setup that works in any terminal — not just inside Claude Code.
Detects available runtimes and configures Ouroboros accordingly.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
from typing import Annotated

import typer
import yaml

from ouroboros.cli.formatters.panels import print_error, print_info, print_success

app = typer.Typer(
    name="setup",
    help="Set up Ouroboros for your environment.",
    invoke_without_command=True,
)


def _detect_runtimes() -> dict[str, str | None]:
    """Detect available runtime CLIs in PATH."""
    runtimes: dict[str, str | None] = {}
    for name in ("claude", "codex", "opencode"):
        path = shutil.which(name)
        runtimes[name] = path
    return runtimes


def _setup_codex(codex_path: str) -> None:
    """Configure Ouroboros for the Codex runtime."""
    from ouroboros.config.loader import create_default_config, ensure_config_dir

    config_dir = ensure_config_dir()
    config_path = config_dir / "config.yaml"

    if config_path.exists():
        config_dict = yaml.safe_load(config_path.read_text()) or {}
    else:
        create_default_config(config_dir)
        config_dict = yaml.safe_load(config_path.read_text()) or {}

    # Set runtime and LLM backend to codex
    config_dict.setdefault("orchestrator", {})
    config_dict["orchestrator"]["runtime_backend"] = "codex"
    config_dict["orchestrator"]["codex_cli_path"] = codex_path

    config_dict.setdefault("llm", {})
    config_dict["llm"]["backend"] = "codex"

    with config_path.open("w") as f:
        yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)

    print_success(f"Configured Codex runtime (CLI: {codex_path})")
    print_info(f"Config saved to: {config_path}")


def _setup_claude(claude_path: str) -> None:
    """Configure Ouroboros for the Claude Code runtime."""
    from ouroboros.config.loader import create_default_config, ensure_config_dir

    config_dir = ensure_config_dir()
    config_path = config_dir / "config.yaml"

    if not config_path.exists():
        create_default_config(config_dir)

    # Register MCP server in ~/.claude/mcp.json
    mcp_config_path = Path.home() / ".claude" / "mcp.json"
    mcp_config_path.parent.mkdir(parents=True, exist_ok=True)

    mcp_data: dict = {}
    if mcp_config_path.exists():
        mcp_data = json.loads(mcp_config_path.read_text())

    mcp_data.setdefault("mcpServers", {})
    if "ouroboros" not in mcp_data["mcpServers"]:
        mcp_data["mcpServers"]["ouroboros"] = {
            "command": "uvx",
            "args": ["--from", "ouroboros-ai", "ouroboros", "mcp", "serve"],
        }
        with mcp_config_path.open("w") as f:
            json.dump(mcp_data, f, indent=2)
        print_success("Registered MCP server in ~/.claude/mcp.json")
    else:
        print_info("MCP server already registered.")

    print_success(f"Configured Claude Code runtime (CLI: {claude_path})")
    print_info(f"Config saved to: {config_path}")


@app.callback(invoke_without_command=True)
def setup(
    runtime: Annotated[
        str | None,
        typer.Option(
            "--runtime",
            "-r",
            help="Runtime backend to configure (claude, codex).",
        ),
    ] = None,
    non_interactive: Annotated[
        bool,
        typer.Option(
            "--non-interactive",
            help="Skip interactive prompts (for scripted installs).",
        ),
    ] = False,
) -> None:
    """Set up Ouroboros for your environment.

    Detects available runtimes (Claude Code, Codex) and configures
    Ouroboros to use the selected backend.

    [dim]Examples:[/dim]
    [dim]    ouroboros setup                    # auto-detect[/dim]
    [dim]    ouroboros setup --runtime codex    # use Codex[/dim]
    [dim]    ouroboros setup --runtime claude   # use Claude Code[/dim]
    """
    from ouroboros.cli.formatters import console

    console.print("\n[bold cyan]Ouroboros Setup[/bold cyan]\n")

    # Detect available runtimes
    detected = _detect_runtimes()
    available = {k: v for k, v in detected.items() if v is not None}

    if available:
        console.print("[bold]Detected runtimes:[/bold]")
        for name, path in available.items():
            console.print(f"  [green]✓[/green] {name} → {path}")
    else:
        console.print("[yellow]No runtimes detected in PATH.[/yellow]")

    unavailable = {k for k, v in detected.items() if v is None}
    for name in unavailable:
        console.print(f"  [dim]✗ {name} (not found)[/dim]")

    console.print()

    # Resolve which runtime to configure
    selected = runtime
    if selected is None:
        if len(available) == 1:
            selected = next(iter(available))
            print_info(f"Auto-selected: {selected}")
        elif len(available) > 1:
            if non_interactive:
                selected = "claude" if "claude" in available else next(iter(available))
                print_info(f"Non-interactive mode, selected: {selected}")
            else:
                choices = list(available.keys())
                for i, name in enumerate(choices, 1):
                    console.print(f"  [{i}] {name}")
                console.print()
                choice = typer.prompt("Select runtime", default="1")
                try:
                    idx = int(choice) - 1
                    selected = choices[idx]
                except (ValueError, IndexError):
                    selected = choice
        else:
            print_error(
                "No runtimes found.\n\n"
                "Install one of:\n"
                "  • Claude Code: https://claude.ai/download\n"
                "  • Codex CLI:   npm install -g @openai/codex"
            )
            raise typer.Exit(1)

    # Validate selection
    if selected in ("claude", "claude_code"):
        claude_path = available.get("claude")
        if not claude_path:
            print_error("Claude Code CLI not found in PATH.")
            raise typer.Exit(1)
        _setup_claude(claude_path)
    elif selected in ("codex", "codex_cli"):
        codex_path = available.get("codex")
        if not codex_path:
            print_error("Codex CLI not found in PATH.")
            raise typer.Exit(1)
        _setup_codex(codex_path)
    else:
        print_error(f"Unsupported runtime: {selected}")
        raise typer.Exit(1)

    console.print("\n[bold green]Setup complete![/bold green]")
    console.print("\n[dim]Next steps:[/dim]")
    console.print('  ouroboros init start "your idea here"')
    console.print("  ouroboros run workflow seed.yaml\n")
