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


_CODEX_MCP_SECTION = """# Ouroboros MCP hookup for Codex CLI.
# Keep Ouroboros runtime settings and per-role model overrides in
# ~/.ouroboros/config.yaml (for example: clarification.default_model,
# llm.qa_model, evaluation.semantic_model, consensus.*).
# This file is only for the Codex MCP/env registration block.

[mcp_servers.ouroboros]
command = "uvx"
args = ["--from", "ouroboros-ai", "ouroboros", "mcp", "serve"]

[mcp_servers.ouroboros.env]
OUROBOROS_AGENT_RUNTIME = "codex"
OUROBOROS_LLM_BACKEND = "codex"
"""

_CODEX_MCP_COMMENT_LINES = (
    "# Ouroboros MCP hookup for Codex CLI.",
    "# Keep Ouroboros runtime settings and per-role model overrides in",
    "# ~/.ouroboros/config.yaml (for example: clarification.default_model,",
    "# llm.qa_model, evaluation.semantic_model, consensus.*).",
    "# This file is only for the Codex MCP/env registration block.",
)


def _is_codex_ouroboros_table_header(line: str) -> bool:
    """Return True when the line starts the managed Codex MCP table."""
    return line == "[mcp_servers.ouroboros]" or line.startswith("[mcp_servers.ouroboros.")


def _trim_managed_codex_comments(lines: list[str]) -> None:
    """Remove the managed Codex comment block immediately before a table."""
    while lines and not lines[-1].strip():
        lines.pop()

    comment_index = len(lines)
    for expected in reversed(_CODEX_MCP_COMMENT_LINES):
        if comment_index == 0 or lines[comment_index - 1] != expected:
            return
        comment_index -= 1

    del lines[comment_index:]


def _upsert_codex_mcp_section(raw: str) -> tuple[str, bool]:
    """Insert or replace the managed Codex MCP block.

    Returns:
        Tuple of (updated_contents, existed_before).
    """
    section_lines = _CODEX_MCP_SECTION.strip("\n").splitlines()
    input_lines = raw.splitlines()
    output_lines: list[str] = []
    index = 0
    existed_before = False
    inserted = False

    while index < len(input_lines):
        stripped = input_lines[index].strip()
        if _is_codex_ouroboros_table_header(stripped):
            existed_before = True
            if not inserted:
                _trim_managed_codex_comments(output_lines)
                if output_lines and output_lines[-1].strip():
                    output_lines.append("")
                output_lines.extend(section_lines)
                inserted = True

            index += 1
            while index < len(input_lines):
                next_stripped = input_lines[index].strip()
                is_table_header = next_stripped.startswith("[") and next_stripped.endswith("]")
                if is_table_header and not _is_codex_ouroboros_table_header(next_stripped):
                    break
                index += 1
            continue

        output_lines.append(input_lines[index])
        index += 1

    if not inserted:
        if output_lines and output_lines[-1].strip():
            output_lines.append("")
        output_lines.extend(section_lines)

    return "\n".join(output_lines).rstrip() + "\n", existed_before


def _register_codex_mcp_server() -> None:
    """Register the Ouroboros MCP/env hookup in ~/.codex/config.toml."""
    import tomllib

    codex_config = Path.home() / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True, exist_ok=True)

    if codex_config.exists():
        raw = codex_config.read_text(encoding="utf-8")
        try:
            tomllib.loads(raw)
        except tomllib.TOMLDecodeError:
            print_error(f"Could not parse {codex_config} — skipping MCP registration.")
            return

        updated_raw, existed_before = _upsert_codex_mcp_section(raw)
        if updated_raw == raw:
            print_info("Codex MCP server already up to date.")
            return

        codex_config.write_text(updated_raw, encoding="utf-8")
        if existed_before:
            print_success(f"Updated Ouroboros MCP server in {codex_config}")
        else:
            print_success(f"Registered Ouroboros MCP server in {codex_config}")
    else:
        codex_config.write_text(_CODEX_MCP_SECTION.lstrip("\n"), encoding="utf-8")
        print_success(f"Registered Ouroboros MCP server in {codex_config}")


def _print_codex_config_guidance(config_path: Path) -> None:
    """Explain where Codex users should configure Ouroboros vs. Codex settings."""
    print_info(f"Configure Ouroboros runtime and per-role model overrides in {config_path}.")
    print_info("Use ~/.codex/config.toml only for the Codex MCP/env hookup written by setup.")


def _install_codex_artifacts() -> None:
    """Install packaged Ouroboros rules and skills into ~/.codex/."""
    from ouroboros.codex import install_codex_rules, install_codex_skills

    codex_dir = Path.home() / ".codex"

    try:
        rules_path = install_codex_rules(codex_dir=codex_dir, prune=True)
        print_success(f"Installed Codex rules → {rules_path}")
    except FileNotFoundError:
        print_error("Could not locate packaged Codex rules.")

    try:
        skill_paths = install_codex_skills(codex_dir=codex_dir, prune=True)
        print_success(f"Installed {len(skill_paths)} Codex skills → {codex_dir / 'skills'}")
    except FileNotFoundError:
        print_error("Could not locate packaged Codex skills.")


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

    # Install Codex-native rules and skills into ~/.codex/
    _install_codex_artifacts()

    # Register MCP server in Codex config (~/.codex/config.toml)
    _register_codex_mcp_server()
    _print_codex_config_guidance(config_path)

    # Also register MCP server for Codex users who also have Claude Code
    mcp_config_path = Path.home() / ".claude" / "mcp.json"
    if mcp_config_path.exists() or (Path.home() / ".claude").is_dir():
        mcp_config_path.parent.mkdir(parents=True, exist_ok=True)

        mcp_data: dict = {}
        if mcp_config_path.exists():
            mcp_data = json.loads(mcp_config_path.read_text())

        mcp_data.setdefault("mcpServers", {})
        entry = mcp_data["mcpServers"].get("ouroboros", {})
        if not entry:
            entry = {
                "command": "uvx",
                "args": ["--from", "ouroboros-ai", "ouroboros", "mcp", "serve"],
            }
        removed_timeout = entry.pop("timeout", None) is not None
        mcp_data["mcpServers"]["ouroboros"] = entry

        with mcp_config_path.open("w") as f:
            json.dump(mcp_data, f, indent=2)

        if removed_timeout:
            print_info("Removed legacy Claude MCP timeout override.")
        else:
            print_info("Updated Claude MCP server config.")


def _setup_claude(claude_path: str) -> None:
    """Configure Ouroboros for the Claude Code runtime."""
    from ouroboros.config.loader import create_default_config, ensure_config_dir

    config_dir = ensure_config_dir()
    config_path = config_dir / "config.yaml"

    if config_path.exists():
        config_dict = yaml.safe_load(config_path.read_text()) or {}
    else:
        create_default_config(config_dir)
        config_dict = yaml.safe_load(config_path.read_text()) or {}

    # Set runtime and LLM backend to claude
    config_dict.setdefault("orchestrator", {})
    config_dict["orchestrator"]["runtime_backend"] = "claude"
    config_dict["orchestrator"]["cli_path"] = claude_path

    config_dict.setdefault("llm", {})
    config_dict["llm"]["backend"] = "claude"

    with config_path.open("w") as f:
        yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)

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
        entry = mcp_data["mcpServers"]["ouroboros"]
        if "timeout" in entry:
            del entry["timeout"]
            with mcp_config_path.open("w") as f:
                json.dump(mcp_data, f, indent=2)
            print_info("Removed legacy MCP timeout override.")
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
