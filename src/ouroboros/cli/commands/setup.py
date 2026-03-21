"""Setup command for Ouroboros brownfield repository management.

Scans the home directory for git repositories with GitHub origins,
registers them in the brownfield DB, and allows the user to toggle
default repositories for PM interview context (multi-default supported).

Usage:
    ouroboros setup              Scan, register, and toggle default repos
    ouroboros setup scan         Re-scan home directory for repos
    ouroboros setup list         List registered brownfield repos
    ouroboros setup default      Toggle default brownfield repos
"""

from __future__ import annotations

import asyncio

from rich.prompt import Prompt
from rich.table import Table
import typer

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
    help="Scan and register brownfield repos for PM interview context.",
    no_args_is_help=False,
    invoke_without_command=True,
)


# ── Helpers ──────────────────────────────────────────────────────


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


# ── Async core logic ─────────────────────────────────────────────


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
def setup_command(ctx: typer.Context) -> None:
    """Scan home directory for git repos and register them as brownfield context.

    This runs the full setup flow:
    1. Scan ~/ for git repos with GitHub origins
    2. Register found repos in the brownfield DB
    3. Display repos and let user select a default

    [bold]Examples:[/]

        ouroboros setup                 Full scan + select flow
        ouroboros setup scan            Re-scan only
        ouroboros setup list            List registered repos
        ouroboros setup default         Change default repo
    """
    if ctx.invoked_subcommand is not None:
        return

    console.print("\n[bold cyan]Ouroboros Setup[/] — Brownfield Repository Scanner\n")

    try:
        asyncio.run(_run_full_setup())
    except KeyboardInterrupt:
        print_info("\nSetup interrupted.")
        raise typer.Exit(code=0)


async def _run_full_setup() -> None:
    """Execute the full setup flow: scan → display → select default."""
    # Step 1: Scan
    console.print("[cyan]Scanning home directory for git repos...[/]\n")
    with console.status("[cyan]Scanning...[/]", spinner="dots"):
        repos = await _scan_and_register_repos()

    if not repos:
        print_warning("No git repos with GitHub origin found in ~/")
        return

    print_success(f"Found and registered {len(repos)} repo(s).\n")

    # Step 2: Display
    _display_repos_table(repos)
    console.print()

    # Step 3: Select defaults (multi-default — each toggle adds/removes)
    current_defaults = [r for r in repos if r.get("is_default")]
    if current_defaults:
        names = ", ".join(r["name"] for r in current_defaults)
        print_info(f"Current defaults: [cyan]{names}[/]")
        console.print()

    idx = _prompt_repo_selection(
        repos, "Toggle default repo (re-run to add/remove more, or skip to keep current)"
    )
    if idx is not None:
        selected = repos[idx]
        with console.status("[cyan]Updating defaults...[/]", spinner="dots"):
            success = await _set_default_repo(selected["path"])
        if success:
            print_success(f"Default toggled: [cyan]{selected['name']}[/] ({selected['path']})")
        else:
            print_error(f"Failed to update default: {selected['path']}")
    else:
        print_info("Skipped default selection.")

    console.print()
    print_success("Setup complete! Use [bold]ouroboros pm[/] to start a PM interview.\n")


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
        print_info("No repos registered. Run [bold]ouroboros setup[/] first.")
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
        print_warning("No repos registered. Run [bold]ouroboros setup[/] first.")
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
