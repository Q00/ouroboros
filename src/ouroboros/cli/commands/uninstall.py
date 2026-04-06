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
# Each returns True on success, False on skip/failure.
# Failures are reported via print_warning — never raise.


def _remove_claude_mcp(dry_run: bool) -> bool:
    """Remove ouroboros entry from ~/.claude/mcp.json."""
    mcp_path = Path.home() / ".claude" / "mcp.json"
    if not mcp_path.exists():
        return False

    try:
        data = json.loads(mcp_path.read_text())
    except (json.JSONDecodeError, OSError):
        print_warning("~/.claude/mcp.json is malformed — skipping.")
        return False
    servers = data.get("mcpServers", {})
    if "ouroboros" not in servers:
        return False

    if dry_run:
        print_info("[dry-run] Would remove ouroboros from ~/.claude/mcp.json")
        return True

    del servers["ouroboros"]
    try:
        mcp_path.write_text(json.dumps(data, indent=2) + "\n")
    except OSError:
        print_warning("Could not write ~/.claude/mcp.json — skipping.")
        return False
    print_success("Removed ouroboros from ~/.claude/mcp.json")
    return True


def _remove_codex_mcp(dry_run: bool) -> bool:
    """Remove ouroboros MCP section from ~/.codex/config.toml."""
    codex_config = Path.home() / ".codex" / "config.toml"
    if not codex_config.exists():
        return False

    try:
        raw = codex_config.read_text()
    except OSError:
        print_warning("~/.codex/config.toml is unreadable — skipping.")
        return False
    if "[mcp_servers.ouroboros]" not in raw:
        return False

    if dry_run:
        print_info("[dry-run] Would remove ouroboros from ~/.codex/config.toml")
        return True

    lines = raw.splitlines()
    output: list[str] = []
    skip = False
    in_comment_block = False

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("# Ouroboros MCP hookup"):
            in_comment_block = True
            continue
        if in_comment_block and stripped.startswith("#"):
            continue
        in_comment_block = False

        if stripped == "[mcp_servers.ouroboros]" or stripped.startswith("[mcp_servers.ouroboros."):
            skip = True
            continue
        if skip:
            if stripped.startswith("[") and stripped.endswith("]"):
                # Next TOML table header — stop skipping
                skip = False
                output.append(line)
            elif stripped.startswith("#"):
                # Comment after the managed section — preserve it
                skip = False
                output.append(line)
            # else: key=value lines or blank lines inside the table — skip them
            continue

        output.append(line)

    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(output)).strip() + "\n"
    try:
        codex_config.write_text(cleaned)
    except OSError:
        print_warning("Could not write ~/.codex/config.toml — skipping.")
        return False
    print_success("Removed ouroboros from ~/.codex/config.toml")
    return True


def _remove_codex_artifacts(dry_run: bool) -> bool:
    """Remove Codex rules and skills installed by setup.

    Returns True only if ALL existing artifacts were removed successfully.
    Returns False if any artifact could not be removed.
    """
    rules_path = Path.home() / ".codex" / "rules" / "ouroboros.md"
    skills_path = Path.home() / ".codex" / "skills" / "ouroboros"
    had_work = False
    all_ok = True

    if rules_path.exists():
        had_work = True
        if dry_run:
            print_info(f"[dry-run] Would remove {rules_path}")
        else:
            try:
                rules_path.unlink()
                print_success(f"Removed {rules_path}")
            except OSError:
                print_warning(f"Could not remove {rules_path} — skipping.")
                all_ok = False

    if skills_path.exists():
        had_work = True
        if dry_run:
            print_info(f"[dry-run] Would remove {skills_path}/")
        else:
            try:
                shutil.rmtree(skills_path)
                print_success(f"Removed {skills_path}/")
            except OSError:
                print_warning(f"Could not remove {skills_path}/ — skipping.")
                all_ok = False

    return had_work and all_ok


def _strip_jsonc(text: str) -> str:
    """Strip JSONC features (comments, trailing commas) to produce valid JSON.

    .. deprecated::
        Forwards to :func:`ouroboros.cli.jsonc.strip_jsonc` which handles
        quoted strings correctly.
    """
    from ouroboros.cli.jsonc import strip_jsonc

    return strip_jsonc(text)


def _remove_opencode_mcp(dry_run: bool) -> bool:
    """Remove ouroboros entry from OpenCode config (~/.config/opencode/opencode.json)."""
    config_path = Path.home() / ".config" / "opencode" / "opencode.json"
    if not config_path.exists():
        return False

    try:
        data = json.loads(_strip_jsonc(config_path.read_text()))
    except (json.JSONDecodeError, OSError):
        print_warning(f"{config_path} is malformed — skipping.")
        return False
    mcp = data.get("mcp")
    if not isinstance(mcp, dict) or "ouroboros" not in mcp:
        return False

    if dry_run:
        print_info(f"[dry-run] Would remove ouroboros from {config_path}")
        return True

    del mcp["ouroboros"]
    try:
        config_path.write_text(json.dumps(data, indent=2) + "\n")
    except OSError:
        print_warning(f"Could not write {config_path} — skipping.")
        return False
    print_success(f"Removed ouroboros from {config_path}")
    return True


def _remove_claude_md_block(project_dir: Path, dry_run: bool) -> bool:
    """Remove <!-- ooo:START --> … <!-- ooo:END --> block from CLAUDE.md."""
    claude_md = project_dir / "CLAUDE.md"
    if not claude_md.exists():
        return False

    try:
        content = claude_md.read_text()
    except OSError:
        print_warning(f"Could not read {claude_md} — skipping.")
        return False
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
    try:
        claude_md.write_text(cleaned)
    except OSError:
        print_warning(f"Could not write {claude_md} — skipping.")
        return False
    print_success(f"Removed Ouroboros block from {claude_md}")
    return True


def _remove_data_dir(dry_run: bool) -> bool:
    """Remove ~/.ouroboros/ directory."""
    data_dir = Path.home() / ".ouroboros"
    if not data_dir.exists():
        return False

    if dry_run:
        print_info("[dry-run] Would remove ~/.ouroboros/")
        return True

    try:
        shutil.rmtree(data_dir)
    except OSError:
        print_warning("Could not fully remove ~/.ouroboros/ — partial cleanup.")
        return False
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

    try:
        shutil.rmtree(ooo_dir)
    except OSError:
        print_warning(f"Could not remove {ooo_dir}/ — skipping.")
        return False
    print_success(f"Removed {ooo_dir}/")
    return True


# ── CLI Command ──────────────────────────────────────────────────


@app.callback(invoke_without_command=True)
def uninstall(
    keep_data: Annotated[
        bool,
        typer.Option(
            "--keep-data",
            help="Keep entire ~/.ouroboros/ directory (config, credentials, seeds, logs, DB).",
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
    [dim]    ouroboros uninstall --keep-data  # preserve ~/.ouroboros/[/dim]
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
            targets.append("MCP server registration (~/.claude/mcp.json — may be malformed)")

    codex_config = Path.home() / ".codex" / "config.toml"
    try:
        if codex_config.exists() and "[mcp_servers.ouroboros]" in codex_config.read_text():
            targets.append("Codex MCP config (~/.codex/config.toml)")
    except OSError:
        targets.append("Codex MCP config (~/.codex/config.toml — may be unreadable)")

    opencode_config = Path.home() / ".config" / "opencode" / "opencode.json"
    try:
        if opencode_config.exists():
            oc_data = json.loads(_strip_jsonc(opencode_config.read_text()))
            if isinstance(oc_data.get("mcp"), dict) and "ouroboros" in oc_data["mcp"]:
                targets.append(f"OpenCode MCP config ({opencode_config})")
    except (json.JSONDecodeError, OSError):
        targets.append(f"OpenCode MCP config ({opencode_config} — may be malformed)")

    codex_rules = Path.home() / ".codex" / "rules" / "ouroboros.md"
    codex_skills = Path.home() / ".codex" / "skills" / "ouroboros"
    if codex_rules.exists() or codex_skills.exists():
        targets.append("Codex rules and skills (~/.codex/)")

    cwd = Path.cwd()
    claude_md = cwd / "CLAUDE.md"
    try:
        if claude_md.exists() and "<!-- ooo:START -->" in claude_md.read_text():
            targets.append(f"CLAUDE.md integration block ({claude_md})")
    except OSError:
        pass

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
        console.print("  [dim]- ~/.ouroboros/ (--keep-data)[/dim]")
    console.print()

    if dry_run:
        console.print("[yellow]Dry run — no changes made.[/yellow]\n")
        raise typer.Exit()

    if not yes:
        confirm = typer.confirm("Proceed with uninstall?", default=False)
        if not confirm:
            print_info("Cancelled.")
            raise typer.Exit()

    # Execute removal — track failures only for items we expected to remove.
    # Each helper returns True on success, False on skip/failure.
    console.print()
    failed: list[str] = []

    if not _remove_claude_mcp(dry_run=False):
        # Only record as failed if we expected to clean it (was in targets)
        if any("mcp.json" in t for t in targets):
            failed.append("~/.claude/mcp.json")

    if not _remove_codex_mcp(dry_run=False):
        if any("codex/config.toml" in t for t in targets):
            failed.append("~/.codex/config.toml")

    if not _remove_opencode_mcp(dry_run=False):
        if any("OpenCode MCP" in t for t in targets):
            failed.append("OpenCode MCP config")

    if not _remove_codex_artifacts(dry_run=False):
        if any("Codex rules" in t for t in targets):
            failed.append("~/.codex/ rules/skills")

    if not _remove_claude_md_block(cwd, dry_run=False):
        if any("CLAUDE.md" in t for t in targets):
            failed.append("CLAUDE.md block")

    if not _remove_project_dir(cwd, dry_run=False):
        if any("Project config" in t for t in targets):
            failed.append(f"{cwd}/.ouroboros/")

    if not keep_data:
        if not _remove_data_dir(dry_run=False):
            if any("Data directory" in t for t in targets):
                failed.append("~/.ouroboros/")

    # Final summary
    console.print()
    if failed:
        console.print("[bold yellow]Ouroboros partially removed.[/bold yellow]")
        console.print("[yellow]Could not clean:[/yellow]")
        for s in failed:
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
