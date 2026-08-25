"""Cleanly reverse setup-owned runtime integrations and local state.

This includes MCP registrations, runtime-specific skills and routing guides,
bridge configuration, project integration blocks, and Ouroboros data. It does
not remove the Python package, runtime plugins, project source, or git history.
"""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import re
import shutil
import tomllib
from typing import Annotated

import typer

from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import (
    print_info,
    print_success,
    print_warning,
)
from ouroboros.cli.opencode_config import (
    find_opencode_config,
    is_bridge_plugin_entry,
    opencode_config_dir,
)
from ouroboros.codex import CODEX_RULE_FILENAME, resolve_codex_home, resolve_packaged_codex_assets
from ouroboros.gjc import (
    gjc_agent_dir,
    gjc_bridge_path,
    gjc_instruction_path,
    gjc_mcp_bridge_config_path,
    gjc_skills_root,
    is_setup_managed_gjc_bridge,
    is_setup_managed_gjc_instruction,
    is_setup_managed_gjc_mcp_bridge_config,
    is_setup_managed_gjc_mcp_entry,
    persisted_gjc_mcp_entry,
    remove_gjc_skills,
    remove_persisted_gjc_mcp_server,
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
    from ouroboros.cli.commands.setup import (
        _atomic_write_text_if_current_matches,
        _codex_mcp_entry_from_toml,
        _has_managed_codex_mcp_comment,
        _is_setup_managed_codex_mcp_entry,
        _remove_codex_mcp_section,
        _snapshot_path,
    )

    codex_config = resolve_codex_home() / "config.toml"
    if not codex_config.exists():
        return False

    config_snapshot = _snapshot_path(codex_config)
    try:
        raw = codex_config.read_text()
    except OSError:
        print_warning("~/.codex/config.toml is unreadable — skipping.")
        return False
    try:
        parsed = tomllib.loads(raw)
    except tomllib.TOMLDecodeError:
        print_warning("~/.codex/config.toml is malformed — skipping Codex MCP removal.")
        return False
    entry = _codex_mcp_entry_from_toml(parsed)
    if entry is None:
        return False
    if not _is_setup_managed_codex_mcp_entry(
        entry,
        has_managed_comment=_has_managed_codex_mcp_comment(raw),
    ):
        print_info("Preserved user-managed Ouroboros MCP config in ~/.codex/config.toml")
        return False

    if dry_run:
        print_info("[dry-run] Would remove ouroboros from ~/.codex/config.toml")
        return True

    cleaned, removed = _remove_codex_mcp_section(raw)
    if not removed:
        print_warning("Codex MCP ownership was detected but no removable TOML entry was found.")
        return False
    try:
        tomllib.loads(cleaned)
    except tomllib.TOMLDecodeError:
        print_warning("Codex MCP removal would create malformed TOML — skipping.")
        return False
    try:
        _atomic_write_text_if_current_matches(
            codex_config,
            cleaned,
            config_snapshot,
        )
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
    codex_dir = resolve_codex_home()
    try:
        with resolve_packaged_codex_assets() as assets:
            managed_relative_paths = set(assets.managed_relative_install_paths)
    except FileNotFoundError:
        managed_relative_paths = {Path("rules") / CODEX_RULE_FILENAME}
    managed_relative_paths.add(Path("skills") / "ouroboros")  # legacy setup path

    rule_paths = [
        codex_dir / relative_path
        for relative_path in managed_relative_paths
        if relative_path.parts[:1] == ("rules",) and (codex_dir / relative_path).exists()
    ]
    skill_paths = [
        codex_dir / relative_path
        for relative_path in managed_relative_paths
        if relative_path.parts[:1] == ("skills",) and (codex_dir / relative_path).exists()
    ]
    had_work = False
    all_ok = True

    for rules_path in rule_paths:
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

    for skills_path in skill_paths:
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


def _find_opencode_config() -> Path | None:
    """Locate existing OpenCode config (``opencode.jsonc`` or ``opencode.json``).

    Delegates to :func:`ouroboros.cli.opencode_config.find_opencode_config`
    with ``allow_default=False`` so that uninstall skips cleanly when no
    config file exists.
    """
    return find_opencode_config(allow_default=False)


def _remove_opencode_mcp(dry_run: bool) -> bool:
    """Remove ouroboros entry from OpenCode config (opencode.jsonc or opencode.json)."""
    config_path = _find_opencode_config()
    if config_path is None:
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

    # Warn if we're about to overwrite a .jsonc file that contained comments.
    if config_path.suffix == ".jsonc":
        try:
            original_text = config_path.read_text(encoding="utf-8")
        except OSError:
            original_text = ""
        if "//" in original_text or "/*" in original_text:
            print_warning(
                f"Note: JSONC comments in {config_path} were removed during config update."
            )

    try:
        config_path.write_text(json.dumps(data, indent=2) + "\n")
    except OSError:
        print_warning(f"Could not write {config_path} — skipping.")
        return False
    print_success(f"Removed ouroboros from {config_path}")
    return True


def _remove_gjc_artifacts(dry_run: bool) -> bool:
    """Remove setup-owned GJC skills, active route, MCP state, config, and guide."""
    agent_dir = gjc_agent_dir()
    skills = remove_gjc_skills(agent_dir=agent_dir, dry_run=True)
    durable_mcp = persisted_gjc_mcp_entry()
    managed_mcp = is_setup_managed_gjc_mcp_entry(durable_mcp)
    bridge_config = gjc_mcp_bridge_config_path()
    managed_bridge_config = is_setup_managed_gjc_mcp_bridge_config(bridge_config)
    compatibility_bridge = gjc_bridge_path()
    managed_compatibility_bridge = is_setup_managed_gjc_bridge(compatibility_bridge)
    guide = gjc_instruction_path()
    managed_guide = is_setup_managed_gjc_instruction(guide)
    if not any(
        (skills, managed_mcp, managed_bridge_config, managed_compatibility_bridge, managed_guide)
    ):
        return False
    if dry_run:
        if skills:
            print_info(f"[dry-run] Would remove {len(skills)} GJC Ouroboros skills")
        if managed_mcp:
            print_info("[dry-run] Would remove Ouroboros MCP registration from GJC")
        if managed_bridge_config:
            print_info(f"[dry-run] Would remove GJC MCP bridge config: {bridge_config}")
        if managed_compatibility_bridge:
            print_info(f"[dry-run] Would remove GJC compatibility bridge: {compatibility_bridge}")
        if managed_guide:
            print_info(f"[dry-run] Would remove GJC routing guide: {guide}")
        return True

    if managed_mcp:
        if not remove_persisted_gjc_mcp_server():
            print_warning(
                "Could not remove the setup-owned GJC MCP registration; "
                "preserved the remaining ownership artifacts for a later retry."
            )
            return False
        print_success("Removed Ouroboros MCP registration from GJC")

    all_ok = True
    if skills:
        try:
            removed = remove_gjc_skills(agent_dir=agent_dir)
        except OSError as exc:
            print_warning(f"Could not remove GJC Ouroboros skills: {exc}")
            removed = ()
        all_ok = len(removed) == len(skills)
        if all_ok:
            print_success(f"Removed {len(removed)} GJC Ouroboros skills")
    # Re-validate each ownership judgment inside the destructive boundary:
    # the discovery checks above may be stale by the time removal happens.
    if managed_bridge_config:
        all_ok = (
            _claim_remove_owned_file(bridge_config, is_setup_managed_gjc_mcp_bridge_config)
            and all_ok
        )
    if managed_compatibility_bridge:
        all_ok = (
            _claim_remove_owned_file(compatibility_bridge, is_setup_managed_gjc_bridge) and all_ok
        )
    if managed_guide:
        all_ok = _claim_remove_owned_file(guide, is_setup_managed_gjc_instruction) and all_ok
    return all_ok


def _claim_remove_owned_file(path: Path, is_owned: Callable[[Path], bool]) -> bool:
    """Claim, re-validate, and remove one setup-owned file; prune its directory."""
    from ouroboros.core.fs_ownership import claim_and_remove_owned

    try:
        if not claim_and_remove_owned(path, is_owned=is_owned):
            return False
    except OSError:
        return False
    try:
        path.parent.rmdir()
    except OSError:
        pass
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


def _remove_opencode_bridge_plugin(dry_run: bool) -> bool:
    """Remove the ouroboros-bridge plugin from OpenCode's plugin directory and config."""
    plugin_dir = opencode_config_dir() / "plugins" / "ouroboros-bridge"
    removed_files = False

    if plugin_dir.exists():
        if dry_run:
            print_info(f"[dry-run] Would remove {plugin_dir}/")
            removed_files = True
        else:
            try:
                shutil.rmtree(plugin_dir)
                print_success(f"Removed OpenCode bridge plugin ({plugin_dir}/)")
                removed_files = True
            except OSError:
                print_warning(f"Could not remove {plugin_dir}/ — skipping.")

    # Also remove from opencode config plugin array.
    # Use find_opencode_config() for correct precedence (.jsonc before .json)
    # — avoids drift with the shared helper in opencode_config.py.
    config_path = find_opencode_config(allow_default=False)

    if config_path is not None:
        try:
            raw = config_path.read_text()
            # Use the shared JSONC stripper — consistent with the other
            # uninstall paths (lines ~200, ~421) and handles block comments
            # + trailing commas that the previous inline single-line
            # stripper missed.
            data = json.loads(_strip_jsonc(raw))
            plugins = data.get("plugin", [])
            if isinstance(plugins, list):
                # Tail-match removal: sweep any bridge-plugin entry (exact
                # canonical path, legacy installs, XDG/root migrations,
                # Windows separator variants). Mirrors setup's dedupe so
                # uninstall actually cleans what setup can register.
                kept = [e for e in plugins if not is_bridge_plugin_entry(e)]
                if len(kept) != len(plugins):
                    if dry_run:
                        print_info(f"[dry-run] Would remove bridge plugin from {config_path}")
                        return True
                    data["plugin"] = kept
                    with config_path.open("w") as f:
                        json.dump(data, f, indent=2)
                        f.write("\n")
                    removed_count = len(plugins) - len(kept)
                    suffix = "" if removed_count == 1 else f" ({removed_count} entries)"
                    print_success(f"Removed bridge plugin entry from {config_path}{suffix}")
                    removed_files = True
        except (json.JSONDecodeError, OSError, KeyError):
            pass  # Best effort — don't fail uninstall over config parse

    return removed_files


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

    codex_config = resolve_codex_home() / "config.toml"
    try:
        if codex_config.exists():
            raw_codex_config = codex_config.read_text()
            try:
                parsed_codex_config = tomllib.loads(raw_codex_config)
            except tomllib.TOMLDecodeError:
                targets.append("Codex MCP config (~/.codex/config.toml — may be malformed)")
            else:
                from ouroboros.cli.commands.setup import (
                    _codex_mcp_entry_from_toml,
                    _has_managed_codex_mcp_comment,
                    _is_setup_managed_codex_mcp_entry,
                )

                entry = _codex_mcp_entry_from_toml(parsed_codex_config)
                if isinstance(entry, dict) and _is_setup_managed_codex_mcp_entry(
                    entry,
                    has_managed_comment=_has_managed_codex_mcp_comment(raw_codex_config),
                ):
                    targets.append("Codex MCP config (~/.codex/config.toml)")
    except OSError:
        targets.append("Codex MCP config (~/.codex/config.toml — may be unreadable)")

    opencode_config = _find_opencode_config()
    try:
        if opencode_config is not None:
            oc_data = json.loads(_strip_jsonc(opencode_config.read_text()))
            if isinstance(oc_data.get("mcp"), dict) and "ouroboros" in oc_data["mcp"]:
                targets.append(f"OpenCode MCP config ({opencode_config})")
    except (json.JSONDecodeError, OSError):
        targets.append(f"OpenCode MCP config ({opencode_config} — may be malformed)")

    codex_dir = resolve_codex_home()
    try:
        with resolve_packaged_codex_assets() as assets:
            managed_relative_paths = set(assets.managed_relative_install_paths)
    except FileNotFoundError:
        managed_relative_paths = {Path("rules") / CODEX_RULE_FILENAME}
    managed_relative_paths.add(Path("skills") / "ouroboros")
    if any((codex_dir / relative_path).exists() for relative_path in managed_relative_paths):
        targets.append("Codex rules and skills (~/.codex/)")

    gjc_agent_root = gjc_agent_dir()
    gjc_skill_root = gjc_skills_root(gjc_agent_root)
    if remove_gjc_skills(agent_dir=gjc_agent_root, dry_run=True):
        targets.append(f"GJC Ouroboros skills ({gjc_skill_root}/)")
    bridge_config = gjc_mcp_bridge_config_path()
    if is_setup_managed_gjc_mcp_bridge_config(bridge_config):
        targets.append(f"GJC MCP bridge config ({bridge_config})")
    if is_setup_managed_gjc_mcp_entry(persisted_gjc_mcp_entry()):
        targets.append("GJC Ouroboros MCP registration")
    compatibility_bridge = gjc_bridge_path()
    if is_setup_managed_gjc_bridge(compatibility_bridge):
        targets.append(f"GJC Ouroboros compatibility bridge ({compatibility_bridge})")
    guide = gjc_instruction_path()
    if is_setup_managed_gjc_instruction(guide):
        targets.append(f"GJC Ouroboros routing guide ({guide})")

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

    bridge_plugin_dir = opencode_config_dir() / "plugins" / "ouroboros-bridge"
    if bridge_plugin_dir.exists():
        targets.append(f"OpenCode bridge plugin ({bridge_plugin_dir}/)")
    else:
        # Directory gone but config entry may linger — check opencode config
        _oc_cfg = find_opencode_config(allow_default=False)
        if _oc_cfg is not None:
            try:
                _oc_data = json.loads(_strip_jsonc(_oc_cfg.read_text()))
                _oc_plugins = _oc_data.get("plugin", [])
                if isinstance(_oc_plugins, list) and any(
                    is_bridge_plugin_entry(e) for e in _oc_plugins
                ):
                    targets.append(f"OpenCode bridge plugin entry in {_oc_cfg}")
            except (json.JSONDecodeError, OSError):
                pass

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

    if not _remove_gjc_artifacts(dry_run=False):
        if any(target.startswith("GJC Ouroboros") for target in targets):
            failed.append("GJC Ouroboros skills/MCP")

    if not _remove_claude_md_block(cwd, dry_run=False):
        if any("CLAUDE.md" in t for t in targets):
            failed.append("CLAUDE.md block")

    if not _remove_project_dir(cwd, dry_run=False):
        if any("Project config" in t for t in targets):
            failed.append(f"{cwd}/.ouroboros/")

    if not _remove_opencode_bridge_plugin(dry_run=False):
        if any("OpenCode bridge plugin" in t for t in targets):
            failed.append("OpenCode bridge plugin")

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
    if failed:
        raise typer.Exit(1)
