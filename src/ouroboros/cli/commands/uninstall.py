"""Uninstall command for Ouroboros.

Cleanly reverses everything `ouroboros setup` did:
  1. MCP server registration  (~/.claude/mcp.json, ~/.codex/config.toml)
  2. CLAUDE.md integration block (<!-- ooo:START --> … <!-- ooo:END -->)
  3. Codex artifacts          (current packaged rules/skills plus legacy ~/.codex/skills/ouroboros/)
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
    is_v2_bridge_plugin_entry,
    opencode_config_dir,
)
from ouroboros.codex import CODEX_RULE_FILENAME, resolve_codex_home, resolve_packaged_codex_assets

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


def _opencode_unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _load_opencode_uninstall_config(config_path: Path) -> tuple[str, dict, object] | None:
    """Read a stable editable config, refusing ambiguous V2 JSONC rewrites."""
    from ouroboros.cli.commands.setup import _snapshot_path

    try:
        snapshot = _snapshot_path(config_path)
        raw = config_path.read_text(encoding="utf-8")
        parsed = json.loads(_strip_jsonc(raw), object_pairs_hook=_opencode_unique_keys)
        if not isinstance(parsed, dict):
            raise ValueError("expected config object")
        # JSONC-to-JSON rewriting erases user comments. Retain legacy V1
        # behavior, but never expand that data loss to V2 configurations.
        if "plugins" in parsed:
            json.loads(raw, object_pairs_hook=_opencode_unique_keys)
        return raw, parsed, snapshot
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        print_warning(
            f"Cannot safely edit {config_path}; remove the Ouroboros entries manually "
            "to preserve comments and ambiguous settings."
        )
        return None


def _write_opencode_uninstall_config(config_path: Path, data: dict, snapshot: object) -> bool:
    from ouroboros.cli.commands.setup import _atomic_write_text_if_current_matches

    try:
        _atomic_write_text_if_current_matches(
            config_path, json.dumps(data, indent=2) + "\n", snapshot
        )
    except OSError:
        print_warning(f"Could not safely write {config_path} — skipping.")
        return False
    return True


def _remove_opencode_mcp(dry_run: bool) -> bool:
    """Remove ouroboros entry from OpenCode config (opencode.jsonc or opencode.json)."""
    config_path = _find_opencode_config()
    if config_path is None:
        return False

    loaded = _load_opencode_uninstall_config(config_path)
    if loaded is None:
        return False
    original_text, data, snapshot = loaded
    mcp = data.get("mcp")
    if not isinstance(mcp, dict) or "ouroboros" not in mcp:
        return False

    if dry_run:
        print_info(f"[dry-run] Would remove ouroboros from {config_path}")
        return True

    del mcp["ouroboros"]

    from ouroboros.cli.commands.setup import _warn_if_opencode_jsonc_comments_removed

    _warn_if_opencode_jsonc_comments_removed(config_path, original_text)

    if not _write_opencode_uninstall_config(config_path, data, snapshot):
        return False
    print_success(f"Removed ouroboros from {config_path}")
    return True


def _remove_claude_md_block(project_dir: Path, dry_run: bool) -> bool:
    """Remove <!-- ooo:START --> … <!-- ooo:END --> block from CLAUDE.md."""
    claude_md = project_dir / "CLAUDE.md"
    if not claude_md.exists():
        return False

    try:
        content = claude_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
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
        claude_md.write_text(cleaned, encoding="utf-8")
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
    """Remove the installed bridge and its V1 or V2 config registration."""
    config_dir = opencode_config_dir()
    plugin_dir = config_dir / "plugins" / "ouroboros-bridge"
    config_path = find_opencode_config(allow_default=False)
    removed = False
    if config_path is not None:
        loaded = _load_opencode_uninstall_config(config_path)
        if loaded is None:
            return False
        raw, data, snapshot = loaded
        count = 0
        for key, matcher in (
            ("plugin", is_bridge_plugin_entry),
            (
                "plugins",
                lambda entry: is_v2_bridge_plugin_entry(entry, config_dir, config_path.parent),
            ),
        ):
            entries = data.get(key, [])
            if isinstance(entries, list):
                kept = [entry for entry in entries if not matcher(entry)]
                count += len(entries) - len(kept)
                if len(kept) != len(entries):
                    data[key] = kept
        if count:
            if dry_run:
                print_info(f"[dry-run] Would remove bridge plugin from {config_path}")
                removed = True
            else:
                from ouroboros.cli.commands.setup import _warn_if_opencode_jsonc_comments_removed

                _warn_if_opencode_jsonc_comments_removed(config_path, raw)
                if not _write_opencode_uninstall_config(config_path, data, snapshot):
                    return False
                print_success(f"Removed bridge plugin entry from {config_path}")
                removed = True
    if plugin_dir.exists():
        if dry_run:
            print_info(f"[dry-run] Would remove {plugin_dir}/")
            return True
        try:
            shutil.rmtree(plugin_dir)
        except OSError:
            print_warning(f"Could not remove {plugin_dir}/ — skipping.")
            return False
        print_success(f"Removed OpenCode bridge plugin ({plugin_dir}/)")
        removed = True
    return removed


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
    manual_cleanup: list[str] = []

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
    opencode_data: dict | None = None
    opencode_editable = True
    if opencode_config is not None:
        try:
            preview_data = json.loads(
                _strip_jsonc(opencode_config.read_text(encoding="utf-8")),
                object_pairs_hook=_opencode_unique_keys,
            )
            if not isinstance(preview_data, dict):
                raise ValueError("expected config object")
            opencode_data = preview_data
            v1 = preview_data.get("plugin", [])
            v2 = preview_data.get("plugins", [])
            mcp = preview_data.get("mcp", {})
            bridge_dir = opencode_config_dir() / "plugins" / "ouroboros-bridge"
            has_owned_entry = (
                (isinstance(mcp, dict) and "ouroboros" in mcp)
                or (isinstance(v1, list) and any(is_bridge_plugin_entry(e) for e in v1))
                or (
                    isinstance(v2, list)
                    and any(
                        is_v2_bridge_plugin_entry(e, opencode_config_dir(), opencode_config.parent)
                        for e in v2
                    )
                )
            )
            if bridge_dir.exists() or has_owned_entry:
                loaded = _load_opencode_uninstall_config(opencode_config)
                if loaded is None:
                    opencode_editable = False
                    manual_cleanup.append(
                        f"OpenCode config ({opencode_config}) and bridge directory"
                    )
                else:
                    _, opencode_data, _ = loaded
                    loaded_mcp = opencode_data.get("mcp", {})
                    if isinstance(loaded_mcp, dict) and "ouroboros" in loaded_mcp:
                        targets.append(f"OpenCode MCP config ({opencode_config})")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            opencode_editable = False
            manual_cleanup.append(f"OpenCode config ({opencode_config}) and bridge directory")

    codex_dir = resolve_codex_home()
    try:
        with resolve_packaged_codex_assets() as assets:
            managed_relative_paths = set(assets.managed_relative_install_paths)
    except FileNotFoundError:
        managed_relative_paths = {Path("rules") / CODEX_RULE_FILENAME}
    managed_relative_paths.add(Path("skills") / "ouroboros")
    if any((codex_dir / relative_path).exists() for relative_path in managed_relative_paths):
        targets.append("Codex rules and skills (~/.codex/)")

    cwd = Path.cwd()
    claude_md = cwd / "CLAUDE.md"
    try:
        if claude_md.exists() and "<!-- ooo:START -->" in claude_md.read_text(encoding="utf-8"):
            targets.append(f"CLAUDE.md integration block ({claude_md})")
    except UnicodeDecodeError:
        print_warning(f"Could not read {claude_md} — skipping.")
    except OSError:
        pass

    ooo_dir = cwd / ".ouroboros"
    if ooo_dir.exists():
        targets.append(f"Project config ({ooo_dir}/)")

    bridge_plugin_dir = opencode_config_dir() / "plugins" / "ouroboros-bridge"
    if opencode_editable:
        if bridge_plugin_dir.exists():
            targets.append(f"OpenCode bridge plugin ({bridge_plugin_dir}/)")
        elif opencode_data is not None and opencode_config is not None:
            # Directory gone but an entry may linger in the selected config.
            v1 = opencode_data.get("plugin", [])
            v2 = opencode_data.get("plugins", [])
            if (isinstance(v1, list) and any(is_bridge_plugin_entry(e) for e in v1)) or (
                isinstance(v2, list)
                and any(
                    is_v2_bridge_plugin_entry(e, opencode_config_dir(), opencode_config.parent)
                    for e in v2
                )
            ):
                targets.append(f"OpenCode bridge plugin entry in {opencode_config}")

    data_dir = Path.home() / ".ouroboros"
    if not keep_data and data_dir.exists():
        targets.append("Data directory (~/.ouroboros/)")

    if not targets and not manual_cleanup:
        console.print("[green]Nothing to remove — Ouroboros is not installed.[/green]\n")
        raise typer.Exit()

    if targets:
        console.print("[bold]Will remove:[/bold]")
        for t in targets:
            console.print(f"  [red]-[/red] {t}")
        console.print()
    if manual_cleanup:
        console.print("[bold yellow]Manual cleanup required; will be preserved:[/bold yellow]")
        for item in manual_cleanup:
            console.print(f"  [yellow]-[/yellow] {item}")
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
    failed: list[str] = list(manual_cleanup)

    if not _remove_claude_mcp(dry_run=False):
        # Only record as failed if we expected to clean it (was in targets)
        if any("mcp.json" in t for t in targets):
            failed.append("~/.claude/mcp.json")

    if not _remove_codex_mcp(dry_run=False):
        if any("codex/config.toml" in t for t in targets):
            failed.append("~/.codex/config.toml")

    if any("OpenCode MCP" in t for t in targets) and not _remove_opencode_mcp(dry_run=False):
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

    if any("OpenCode bridge plugin" in t for t in targets) and not _remove_opencode_bridge_plugin(
        dry_run=False
    ):
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
