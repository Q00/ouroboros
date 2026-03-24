"""Setup command for Ouroboros.

Standalone setup that works in any terminal — not just inside Claude Code.
Detects available runtimes and configures Ouroboros accordingly.

Also provides brownfield repository management subcommands:
    ouroboros setup scan         Re-scan home directory for repos
    ouroboros setup list         List registered brownfield repos
    ouroboros setup default      Toggle default brownfield repos
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
from typing import Annotated

from rich.prompt import Prompt
from rich.table import Table
import typer
import yaml

from ouroboros.bigbang.brownfield import scan_and_register, set_default_repo
from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import (
    print_error,
    print_info,
    print_success,
    print_warning,
)
from ouroboros.persistence.brownfield import BrownfieldStore

app = typer.Typer(
    name="setup",
    help="Set up Ouroboros for your environment.",
    invoke_without_command=True,
)


# ── Runtime detection helpers ────────────────────────────────────


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


# ── Brownfield repo helpers ──────────────────────────────────────


def _display_repos_table(
    repos: list[dict],
    *,
    show_default: bool = True,
) -> None:
    """Display a Rich table of brownfield repos.

    Args:
        repos: List of BrownfieldRepo-like dicts/objects with
               path, name, desc, is_default attributes.
        show_default: Whether to show the default marker column.
    """
    table = Table(show_header=True, header_style="bold cyan", expand=False)
    table.add_column("#", style="dim", width=4)
    if show_default:
        table.add_column("★", width=3)
    table.add_column("Name", style="cyan")
    table.add_column("Path", style="dim")
    table.add_column("Description", style="dim italic")

    for idx, repo in enumerate(repos, 1):
        is_def = repo.get("is_default", False)
        default_marker = "[bold yellow]★[/]" if is_def else ""
        name = repo.get("name", "unnamed")
        path = repo.get("path", "")
        desc = repo.get("desc", "") or ""

        row = [str(idx)]
        if show_default:
            row.append(default_marker)
        row.extend([name, path, desc])
        table.add_row(*row)

    console.print(table)


def _prompt_repo_selection(
    repos: list[dict],
    prompt_text: str = "Toggle default repo",
) -> int | None:
    """Prompt the user to select a repo to toggle as default.

    Args:
        repos: List of repo dicts.
        prompt_text: Prompt text to display.

    Returns:
        0-based index of the selected repo, or None if cancelled.
    """
    raw = Prompt.ask(
        f"[yellow]{prompt_text}[/] (1-{len(repos)}, or 'skip' to skip)",
        default="skip",
    )

    stripped = raw.strip().lower()
    if stripped in ("skip", "s", ""):
        return None

    try:
        num = int(stripped)
        if 1 <= num <= len(repos):
            return num - 1
    except ValueError:
        pass

    print_warning(f"Invalid selection: {raw}")
    return None


# ── Brownfield async core logic ──────────────────────────────────


async def _scan_and_register_repos() -> list[dict]:
    """Scan home directory and register repos in DB.

    Uses upsert semantics so that manually-registered repos outside the
    scan root are preserved across re-scans.

    Returns:
        List of repo dicts with path, name, desc, is_default.
    """
    store = BrownfieldStore()
    try:
        await store.initialize()
        repos = await scan_and_register(store)
        return [
            {
                "path": r.path,
                "name": r.name,
                "desc": r.desc or "",
                "is_default": r.is_default,
            }
            for r in repos
        ]
    finally:
        await store.close()


async def _list_repos() -> list[dict]:
    """List all registered brownfield repos from DB.

    Returns:
        List of repo dicts with path, name, desc, is_default.
    """
    store = BrownfieldStore()
    try:
        await store.initialize()
        repos = await store.list()
        return [
            {
                "path": r.path,
                "name": r.name,
                "desc": r.desc or "",
                "is_default": r.is_default,
            }
            for r in repos
        ]
    finally:
        await store.close()


async def _set_default_repo(path: str) -> bool:
    """Toggle a repo's default status in DB.

    If the repo is currently a default, removes it.
    If not, adds it as a default.

    Args:
        path: Absolute path of the repo.

    Returns:
        True if successful.
    """
    store = BrownfieldStore()
    try:
        await store.initialize()
        repos = await store.list()
        current = next((r for r in repos if r.path == path), None)
        if current is None:
            return False
        if current.is_default:
            # Remove from defaults
            result = await store.update_is_default(path, is_default=False)
        else:
            # Add as default
            result = await set_default_repo(store, path)
        return result is not None
    finally:
        await store.close()


# ── CLI Commands ─────────────────────────────────────────────────


@app.callback(invoke_without_command=True)
def setup(
    ctx: typer.Context,
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
    [dim]    ouroboros setup scan               # scan brownfield repos[/dim]
    [dim]    ouroboros setup list               # list brownfield repos[/dim]
    [dim]    ouroboros setup default            # toggle default repos[/dim]
    """
    if ctx.invoked_subcommand is not None:
        return

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


# ── Brownfield subcommands ───────────────────────────────────────


@app.command()
def scan() -> None:
    """Re-scan home directory and register new repos.

    Scans ~/ for git repos with GitHub origins and updates the
    brownfield registry. Existing repos are preserved (upsert).
    """
    console.print("\n[bold cyan]Brownfield Scan[/]\n")

    try:
        repos = asyncio.run(_run_scan_only())
    except KeyboardInterrupt:
        print_info("\nScan interrupted.")
        raise typer.Exit(code=0)

    if not repos:
        print_warning("No repos found.")
        return

    print_success(f"Registered {len(repos)} repo(s).\n")
    _display_repos_table(repos)


async def _run_scan_only() -> list[dict]:
    """Scan and register, returning repo list."""
    with console.status("[cyan]Scanning home directory...[/]", spinner="dots"):
        return await _scan_and_register_repos()


@app.command(name="list")
def list_command() -> None:
    """List all registered brownfield repos."""
    console.print("\n[bold cyan]Registered Brownfield Repos[/]\n")

    try:
        repos = asyncio.run(_list_repos())
    except KeyboardInterrupt:
        raise typer.Exit(code=0)

    if not repos:
        print_info("No repos registered. Run [bold]ouroboros setup scan[/] first.")
        return

    _display_repos_table(repos)

    total = len(repos)
    default_count = sum(1 for r in repos if r.get("is_default"))
    console.print(f"\n[dim]Total: {total} repo(s), {default_count} default(s)[/]\n")


@app.command()
def default() -> None:
    """Toggle default brownfield repos for PM interviews.

    Displays all registered repos and lets you toggle defaults (multi-default supported).
    """
    console.print("\n[bold cyan]Set Default Brownfield Repos[/]\n")

    try:
        asyncio.run(_run_set_default())
    except KeyboardInterrupt:
        print_info("\nCancelled.")
        raise typer.Exit(code=0)


async def _run_set_default() -> None:
    """Interactive default repo selection."""
    repos = await _list_repos()

    if not repos:
        print_warning("No repos registered. Run [bold]ouroboros setup scan[/] first.")
        return

    _display_repos_table(repos)
    console.print()

    idx = _prompt_repo_selection(repos, "Select default repos")
    if idx is None:
        print_info("No changes made.")
        return

    selected = repos[idx]
    with console.status("[cyan]Setting defaults...[/]", spinner="dots"):
        success = await _set_default_repo(selected["path"])

    if success:
        print_success(f"Default repos updated: [cyan]{selected['name']}[/] ({selected['path']})")
    else:
        print_error(f"Failed to set defaults: {selected['path']}")
