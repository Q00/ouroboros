"""Uninstall command for Ouroboros.

Cleanly reverses everything `ouroboros setup` did:
  1. MCP server registration  (~/.claude/mcp.json, ~/.codex/config.toml)
  2. CLAUDE.md integration block (<!-- ooo:START --> … <!-- ooo:END -->)
  3. Codex artifacts          (~/.codex/rules/ouroboros.md, ~/.codex/skills/ouroboros/)
  4. Data directory           (~/.ouroboros/)

Does NOT remove:
  - The Python package itself (user runs pip/uv/pipx uninstall separately)
  - The Claude Code plugin   (user runs `claude plugin uninstall ouroboros`)
  - Project source code or git history
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
from typing import Annotated

import typer

from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import (
    print_info,
    print_success,
    print_warning,
)

app = typer.Typer(
    name="uninstall",
    help="Cleanly remove Ouroboros from your system.",
)


# ── Removal helpers ──────────────────────────────────────────────


def _remove_claude_mcp(dry_run: bool) -> bool:
    """Remove ouroboros entry from ~/.claude/mcp.json."""
    mcp_path = Path.home() / ".claude" / "mcp.json"
    if not mcp_path.exists():
        print_info("~/.claude/mcp.json not found — skipping.")
        return False

    try:
        data = json.loads(mcp_path.read_text())
    except (json.JSONDecodeError, OSError):
        print_warning("~/.claude/mcp.json is malformed — skipping.")
        return False
    servers = data.get("mcpServers", {})
    if "ouroboros" not in servers:
        print_info("No ouroboros entry in mcp.json — skipping.")
        return False

    if dry_run:
        print_info("[dry-run] Would remove ouroboros from ~/.claude/mcp.json")
        return True

    del servers["ouroboros"]
    mcp_path.write_text(json.dumps(data, indent=2) + "\n")
    print_success("Removed ouroboros from ~/.claude/mcp.json")
    return True


def _remove_codex_mcp(dry_run: bool) -> bool:
    """Remove ouroboros MCP section from ~/.codex/config.toml."""
    codex_config = Path.home() / ".codex" / "config.toml"
    if not codex_config.exists():
        return False

    raw = codex_config.read_text()
    if "[mcp_servers.ouroboros]" not in raw:
        return False

    if dry_run:
        print_info("[dry-run] Would remove ouroboros from ~/.codex/config.toml")
        return True

    # Remove [mcp_servers.ouroboros] and [mcp_servers.ouroboros.env] sections
    # plus the preceding comment block
    lines = raw.splitlines()
    output: list[str] = []
    skip = False
    in_comment_block = False

    for line in lines:
        stripped = line.strip()

        # Detect managed comment block
        if stripped.startswith("# Ouroboros MCP hookup"):
            in_comment_block = True
            continue
        if in_comment_block and stripped.startswith("#"):
            continue
        in_comment_block = False

        # Detect ouroboros TOML tables
        if stripped == "[mcp_servers.ouroboros]" or stripped.startswith("[mcp_servers.ouroboros."):
            skip = True
            continue
        if skip:
            # End of ouroboros section when we hit another table header
            if stripped.startswith("[") and stripped.endswith("]"):
                skip = False
                output.append(line)
            continue

        output.append(line)

    # Collapse excessive blank lines
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(output)).strip() + "\n"
    codex_config.write_text(cleaned)
    print_success("Removed ouroboros from ~/.codex/config.toml")
    return True


def _remove_codex_artifacts(dry_run: bool) -> bool:
    """Remove Codex rules and skills installed by setup."""
    removed = False
    rules_path = Path.home() / ".codex" / "rules" / "ouroboros.md"
    skills_path = Path.home() / ".codex" / "skills" / "ouroboros"

    if rules_path.exists():
        if dry_run:
            print_info(f"[dry-run] Would remove {rules_path}")
        else:
            rules_path.unlink()
            print_success(f"Removed {rules_path}")
        removed = True

    if skills_path.exists():
        if dry_run:
            print_info(f"[dry-run] Would remove {skills_path}/")
        else:
            shutil.rmtree(skills_path)
            print_success(f"Removed {skills_path}/")
        removed = True

    return removed


def _remove_claude_md_block(project_dir: Path, dry_run: bool) -> bool:
    """Remove <!-- ooo:START --> … <!-- ooo:END --> block from CLAUDE.md."""
    claude_md = project_dir / "CLAUDE.md"
    if not claude_md.exists():
        return False

    content = claude_md.read_text()
    if "<!-- ooo:START -->" not in content:
        return False

    if dry_run:
        print_info(f"[dry-run] Would remove ooo block from {claude_md}")
        return True

    cleaned = re.sub(
        r"<!-- ooo:START -->.*?<!-- ooo:END -->\n?",
        "",
        content,
        flags=re.DOTALL,
    )
    claude_md.write_text(cleaned)
    print_success(f"Removed Ouroboros block from {claude_md}")
    return True


def _remove_data_dir(dry_run: bool) -> bool:
    """Remove ~/.ouroboros/ directory."""
    data_dir = Path.home() / ".ouroboros"
    if not data_dir.exists():
        print_info("~/.ouroboros/ not found — skipping.")
        return False

    if dry_run:
        print_info("[dry-run] Would remove ~/.ouroboros/")
        return True

    shutil.rmtree(data_dir)
    print_success("Removed ~/.ouroboros/")
    return True


def _remove_project_dir(project_dir: Path, dry_run: bool) -> bool:
    """Remove .ouroboros/ directory in the current project."""
    ooo_dir = project_dir / ".ouroboros"
    if not ooo_dir.exists():
        return False

    if dry_run:
        print_info(f"[dry-run] Would remove {ooo_dir}/")
        return True

    shutil.rmtree(ooo_dir)
    print_success(f"Removed {ooo_dir}/")
    return True


# ── CLI Command ──────────────────────────────────────────────────


@app.callback(invoke_without_command=True)
def uninstall(
    keep_data: Annotated[
        bool,
        typer.Option(
            "--keep-data",
            help="Keep ~/.ouroboros/ data directory (seeds, logs, DB).",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show what would be removed without actually deleting.",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Skip confirmation prompt.",
        ),
    ] = False,
) -> None:
    """Cleanly remove all Ouroboros configuration from your system.

    Reverses everything `ouroboros setup` did. Does NOT remove the
    Python package itself — run `pip uninstall ouroboros-ai` separately.

    [dim]Examples:[/dim]
    [dim]    ouroboros uninstall              # interactive[/dim]
    [dim]    ouroboros uninstall -y           # no prompts[/dim]
    [dim]    ouroboros uninstall --dry-run    # preview only[/dim]
    [dim]    ouroboros uninstall --keep-data  # preserve seeds and DB[/dim]
    """
    console.print("\n[bold red]Ouroboros Uninstall[/bold red]\n")

    # Preview what will be removed
    targets: list[str] = []

    mcp_path = Path.home() / ".claude" / "mcp.json"
    if mcp_path.exists():
        try:
            mcp_data = json.loads(mcp_path.read_text())
            if "ouroboros" in mcp_data.get("mcpServers", {}):
                targets.append("MCP server registration (~/.claude/mcp.json)")
        except (json.JSONDecodeError, OSError):
            pass

    codex_config = Path.home() / ".codex" / "config.toml"
    if codex_config.exists() and "[mcp_servers.ouroboros]" in codex_config.read_text():
        targets.append("Codex MCP config (~/.codex/config.toml)")

    codex_rules = Path.home() / ".codex" / "rules" / "ouroboros.md"
    codex_skills = Path.home() / ".codex" / "skills" / "ouroboros"
    if codex_rules.exists() or codex_skills.exists():
        targets.append("Codex rules and skills (~/.codex/)")

    cwd = Path.cwd()
    claude_md = cwd / "CLAUDE.md"
    if claude_md.exists() and "<!-- ooo:START -->" in claude_md.read_text():
        targets.append(f"CLAUDE.md integration block ({claude_md})")

    ooo_dir = cwd / ".ouroboros"
    if ooo_dir.exists():
        targets.append(f"Project config ({ooo_dir}/)")

    data_dir = Path.home() / ".ouroboros"
    if not keep_data and data_dir.exists():
        targets.append("Data directory (~/.ouroboros/)")

    if not targets:
        console.print("[green]Nothing to remove — Ouroboros is not installed.[/green]\n")
        raise typer.Exit()

    console.print("[bold]Will remove:[/bold]")
    for t in targets:
        console.print(f"  [red]-[/red] {t}")
    console.print()

    console.print("[bold]Will NOT remove:[/bold]")
    console.print("  [dim]- Python package (run: pip uninstall ouroboros-ai)[/dim]")
    console.print("  [dim]- Claude Code plugin (run: claude plugin uninstall ouroboros)[/dim]")
    console.print("  [dim]- Your project source code or git history[/dim]")
    if keep_data:
        console.print("  [dim]- ~/.ouroboros/ data (--keep-data flag)[/dim]")
    console.print()

    if dry_run:
        console.print("[yellow]Dry run — no changes made.[/yellow]\n")
        raise typer.Exit()

    if not yes:
        confirm = typer.confirm("Proceed with uninstall?", default=False)
        if not confirm:
            print_info("Cancelled.")
            raise typer.Exit()

    # Execute removal — track skipped items for accurate summary
    console.print()
    skipped: list[str] = []

    if not _remove_claude_mcp(dry_run=False):
        mcp_path = Path.home() / ".claude" / "mcp.json"
        if mcp_path.exists():
            skipped.append("~/.claude/mcp.json (malformed or inaccessible)")

    _remove_codex_mcp(dry_run=False)
    _remove_codex_artifacts(dry_run=False)
    _remove_claude_md_block(cwd, dry_run=False)
    _remove_project_dir(cwd, dry_run=False)
    if not keep_data:
        _remove_data_dir(dry_run=False)

    # Final summary
    console.print()
    if skipped:
        console.print("[bold yellow]Ouroboros partially removed.[/bold yellow]")
        console.print("[yellow]Could not clean:[/yellow]")
        for s in skipped:
            console.print(f"  [yellow]![/yellow] {s}")
        console.print()
    else:
        console.print("[bold green]Ouroboros has been removed.[/bold green]")
    console.print()
    console.print("[dim]To finish cleanup:[/dim]")
    console.print(
        "  uv tool uninstall ouroboros-ai     [dim]# or: pip uninstall ouroboros-ai[/dim]"
    )
    console.print("  claude plugin uninstall ouroboros   [dim]# if using Claude Code plugin[/dim]")
    console.print()
