"""Self-update command for Ouroboros.

Native CLI counterpart of the `ooo update` skill (skills/update/SKILL.md):
  1. Version check     (installed vs latest on PyPI, pre-release aware)
  2. Package upgrade   (same installer that performed the install: uv tool > pipx > pip)
  3. Runtime refresh   (Claude Code plugin + `ouroboros setup --non-interactive`)

Never combines the [claude] and [mcp] extras: the Claude Agent SDK embeds
MCP 1.x while the protocol server requires MCP 2, so supported MCP hosts
launch their own isolated `ouroboros-ai[mcp]` process via uvx/pipx run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
from typing import Annotated
import urllib.request

import typer

from ouroboros import __version__
from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import (
    print_info,
    print_success,
    print_warning,
)

app = typer.Typer(
    name="update",
    help="Update Ouroboros to the latest version.",
)

PYPI_JSON_URL = "https://pypi.org/pypi/ouroboros-ai/json"
PACKAGE_SPEC = "ouroboros-ai[claude]"


# ── Version helpers ──────────────────────────────────────────────

_FALLBACK_VERSION_RE = re.compile(
    r"^\s*v?(?P<release>\d+(?:\.\d+)*)"
    r"(?:(?P<pre_kind>a|b|rc)(?P<pre_num>\d+))?"
    r"(?:\.dev(?P<dev_num>\d+))?"
)


def _fallback_version_key(version: str) -> tuple[tuple[int, ...], int, int]:
    """PEP 440-ish sort key for when `packaging` is not installed.

    Handles the shapes this project actually publishes (X.Y.Z, X.Y.Za1,
    X.Y.Zb2, X.Y.Zrc1, X.Y.Z.devN): dev < a < b < rc < final within a
    release. Unparseable versions sort lowest.
    """
    match = _FALLBACK_VERSION_RE.match(version)
    if match is None:
        return ((), -1, 0)
    release = tuple(int(part) for part in match.group("release").split("."))
    pre_kind = match.group("pre_kind")
    if pre_kind is not None:
        phase = {"a": 1, "b": 2, "rc": 3}[pre_kind]
        number = int(match.group("pre_num") or 0)
    elif match.group("dev_num") is not None:
        phase, number = 0, int(match.group("dev_num"))
    else:
        phase, number = 4, 0
    return (release, phase, number)


def _compare_versions(a: str, b: str) -> int:
    """Return -1, 0, or 1 comparing version `a` to `b`."""
    try:
        from packaging.version import InvalidVersion, Version

        try:
            va, vb = Version(a), Version(b)
            return (va > vb) - (va < vb)
        except InvalidVersion:
            pass
    except ImportError:
        pass
    ka, kb = _fallback_version_key(a), _fallback_version_key(b)
    return (ka > kb) - (ka < kb)


def _is_prerelease(version: str) -> bool:
    try:
        from packaging.version import InvalidVersion, Version

        try:
            return Version(version).is_prerelease
        except InvalidVersion:
            return False
    except ImportError:
        match = _FALLBACK_VERSION_RE.match(version)
        if match is None:
            return False
        return match.group("pre_kind") is not None or match.group("dev_num") is not None


def _latest_pypi_version(include_prereleases: bool, timeout: float = 10.0) -> str | None:
    """Query PyPI for the newest ouroboros-ai version, or None if unreachable."""
    try:
        context = ssl.create_default_context()
        with urllib.request.urlopen(PYPI_JSON_URL, timeout=timeout, context=context) as response:
            data = json.loads(response.read())
    except (OSError, ValueError):
        return None
    stable = data.get("info", {}).get("version")
    if not include_prereleases:
        return stable
    # Pre-release installs may sit ahead of info.version — scan all releases.
    # A release only counts with at least one non-yanked file: pip/uv skip
    # fully-yanked releases, so reporting one as "latest" would prompt an
    # update that installs something else.
    candidates = [
        version
        for version, files in data.get("releases", {}).items()
        if isinstance(files, list)
        and any(isinstance(file, dict) and not file.get("yanked", False) for file in files)
    ]
    if stable:
        candidates.append(stable)
    if not candidates:
        return None
    latest = candidates[0]
    for candidate in candidates[1:]:
        if _compare_versions(candidate, latest) > 0:
            latest = candidate
    return latest


# ── Installer detection and upgrade ──────────────────────────────


def _manager_env_root(command: list[str]) -> Path | None:
    """Ask a tool manager where it keeps its environments; None if unavailable."""
    if shutil.which(command[0]) is None:
        return None
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    lines = result.stdout.strip().splitlines()
    if not lines:
        return None
    root = Path(lines[0].strip())
    if not root.is_absolute():
        return None
    return root.resolve()


def _detect_installer(prefix: Path | None = None) -> str:
    """Return the installer that owns the *running* install: 'uv', 'pipx', or 'pip'.

    Ownership is anchored to the running interpreter's environment
    (sys.prefix), not to global tool listings — a stale uv or pipx
    installation elsewhere must not hijack the upgrade target. Falls back
    to pip against the running interpreter when no manager owns this env.
    """
    env = (prefix or Path(sys.prefix)).resolve()
    uv_root = _manager_env_root(["uv", "tool", "dir"])
    if uv_root is not None and env.is_relative_to(uv_root):
        return "uv"
    pipx_root = _manager_env_root(["pipx", "environment", "--value", "PIPX_LOCAL_VENVS"])
    if pipx_root is not None and env.is_relative_to(pipx_root):
        return "pipx"
    # Heuristic fallback for managers that cannot report their roots.
    parts = [part.lower() for part in env.parts]
    if "pipx" in parts and "venvs" in parts:
        return "pipx"
    if "uv" in parts and "tools" in parts:
        return "uv"
    return "pip"


def _upgrade_command(installer: str, prerelease: bool) -> list[str]:
    """Build the upgrade command for the detected installer."""
    if installer == "uv":
        command = ["uv", "tool", "install", "--upgrade"]
        if prerelease:
            command.append("--prerelease=allow")
        return [*command, PACKAGE_SPEC]
    if installer == "pipx":
        # `pipx upgrade` cannot add extras to an existing venv — reinstall.
        command = ["pipx", "install", "--force"]
        if prerelease:
            command.append("--pip-args=--pre")
        return [*command, PACKAGE_SPEC]
    command = [sys.executable, "-m", "pip", "install", "--upgrade"]
    if prerelease:
        command.append("--pre")
    return [*command, PACKAGE_SPEC]


def _run_step(
    command: list[str],
    *,
    description: str,
    dry_run: bool,
    timeout: float = 600.0,
) -> bool:
    """Run one update step, streaming its output. Returns True on success."""
    if dry_run:
        print_info(f"[dry-run] Would run: {' '.join(command)}")
        return True
    try:
        result = subprocess.run(command, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        print_warning(f"{description} failed — could not run {command[0]!r}.")
        return False
    if result.returncode != 0:
        print_warning(f"{description} exited with code {result.returncode}.")
        return False
    print_success(description)
    return True


# ── Runtime integration refresh ──────────────────────────────────


def _refresh_claude_plugin(dry_run: bool) -> bool | None:
    """Refresh the Claude Code plugin. Returns None when the claude CLI is absent."""
    if shutil.which("claude") is None:
        print_info("claude CLI not found — skipping Claude Code plugin refresh.")
        return None
    # Best effort: a missing marketplace entry must not block the install step.
    _run_step(
        ["claude", "plugin", "marketplace", "update", "ouroboros"],
        description="Refreshed ouroboros marketplace",
        dry_run=dry_run,
    )
    return _run_step(
        ["claude", "plugin", "install", "ouroboros@ouroboros"],
        description="Reinstalled Claude Code plugin",
        dry_run=dry_run,
    )


def _resolve_cli_binary() -> str | None:
    """Locate the console script to run post-upgrade steps through.

    Prefers the running environment's own script over PATH lookup so a
    stale binary earlier on PATH cannot serve the refreshed steps.
    """
    script_dir = Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin")
    for name in ("ouroboros", "ooo"):
        candidate = script_dir / name
        if candidate.exists():
            return str(candidate)
    for name in ("ouroboros", "ooo"):
        found = shutil.which(name)
        if found is not None:
            return found
    return None


def _refresh_runtime_config(runtime: str, dry_run: bool) -> bool:
    """Re-run setup through the (freshly upgraded) console script."""
    binary = _resolve_cli_binary()
    if binary is None:
        print_warning(
            "ouroboros binary not found — run "
            f"`ouroboros setup --runtime {runtime} --non-interactive` manually."
        )
        return False
    return _run_step(
        [binary, "setup", "--runtime", runtime, "--non-interactive"],
        description=f"Refreshed {runtime} runtime config",
        dry_run=dry_run,
    )


def _resolve_runtime(runtime: str) -> str:
    """Resolve --runtime auto to the best available runtime."""
    if runtime != "auto":
        return runtime
    if shutil.which("claude") is not None:
        return "claude"
    if shutil.which("codex") is not None:
        return "codex"
    return "none"


def _installed_version() -> str | None:
    """Read the version from the refreshed binary (post-upgrade, fresh process)."""
    binary = _resolve_cli_binary()
    if binary is None:
        return None
    try:
        result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"\d+\.\d+\.\d+[0-9a-zA-Z.+-]*", result.stdout)
    return match.group(0) if match else None


# ── CLI Command ──────────────────────────────────────────────────


@app.callback(invoke_without_command=True)
def update(
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Only report installed vs latest version — change nothing.",
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
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show the commands that would run without executing them.",
        ),
    ] = False,
    prerelease: Annotated[
        bool | None,
        typer.Option(
            "--prerelease/--no-prerelease",
            help="Include pre-releases (default: only when a pre-release is installed).",
        ),
    ] = None,
    runtime: Annotated[
        str,
        typer.Option(
            "--runtime",
            "-r",
            help="Runtime integration to refresh after upgrading "
            "(auto, claude, codex, ..., or none to skip).",
        ),
    ] = "auto",
) -> None:
    """Update Ouroboros to the latest version.

    Upgrades the PyPI package with the same installer that installed it
    (uv tool > pipx > pip), refreshes the Claude Code plugin when the
    claude CLI is available, and re-runs `ouroboros setup` for the
    selected runtime.

    [dim]Examples:[/dim]
    [dim]    ouroboros update              # interactive[/dim]
    [dim]    ouroboros update --check      # version check only[/dim]
    [dim]    ouroboros update -y           # no prompts (for scripts)[/dim]
    [dim]    ouroboros update --dry-run    # preview the commands[/dim]
    """
    console.print("\n[bold cyan]Ouroboros Update[/bold cyan]\n")

    current = __version__
    include_prereleases = _is_prerelease(current) if prerelease is None else prerelease
    latest = _latest_pypi_version(include_prereleases=include_prereleases)
    if latest is None:
        print_warning("Could not reach PyPI to check the latest version.")
        raise typer.Exit(1)

    console.print(f"Installed: [cyan]v{current}[/cyan]")
    console.print(f"Latest:    [cyan]v{latest}[/cyan]")

    if _compare_versions(current, latest) >= 0:
        console.print(f"\n[green]Ouroboros is up to date (v{current}).[/green]\n")
        raise typer.Exit()

    console.print(f"\nUpdate available: [yellow]v{current}[/yellow] → [green]v{latest}[/green]")
    console.print(f"[dim]Changes: https://github.com/Q00/ouroboros/releases/tag/v{latest}[/dim]\n")

    if check:
        raise typer.Exit()

    installer = _detect_installer()
    console.print(f"Installer: [cyan]{installer}[/cyan]\n")

    if not yes and not dry_run:
        if not typer.confirm(f"Update to v{latest}?", default=True):
            print_info("Cancelled.")
            raise typer.Exit()

    if not _run_step(
        _upgrade_command(installer, include_prereleases),
        description=f"Upgraded {PACKAGE_SPEC} via {installer}",
        dry_run=dry_run,
    ):
        console.print(
            "\n[bold yellow]Update failed — package upgrade did not complete.[/bold yellow]\n"
        )
        raise typer.Exit(1)

    failed: list[str] = []
    resolved_runtime = _resolve_runtime(runtime)
    if resolved_runtime == "none":
        print_info("Runtime refresh skipped — package upgrade only.")
    elif resolved_runtime == "claude":
        plugin_refreshed = _refresh_claude_plugin(dry_run)
        if plugin_refreshed is None:
            # claude CLI absent: a notice, not a failure — setup would also
            # fail without claude on PATH, so skip the config refresh too.
            print_info(
                "Skipping claude runtime config refresh — install the claude CLI "
                "and run `ouroboros setup --runtime claude` later."
            )
        else:
            if plugin_refreshed is False:
                failed.append("Claude Code plugin refresh")
            if not _refresh_runtime_config("claude", dry_run):
                failed.append("claude runtime config refresh")
    else:
        # Codex and the other setup-supported runtimes: setup re-installs
        # the packaged rules/skills, so no separate plugin step is needed.
        if not _refresh_runtime_config(resolved_runtime, dry_run):
            failed.append(f"{resolved_runtime} runtime config refresh")

    console.print()
    if dry_run:
        console.print("[yellow]Dry run — no changes made.[/yellow]\n")
        raise typer.Exit()

    installed = _installed_version()
    if failed:
        console.print("[bold yellow]Ouroboros partially updated.[/bold yellow]")
        console.print("[yellow]Could not complete:[/yellow]")
        for step in failed:
            console.print(f"  [yellow]![/yellow] {step}")
        console.print()
    else:
        console.print(f"[bold green]Updated to v{installed or latest}.[/bold green]")
    if resolved_runtime == "claude":
        console.print("[dim]Restart your Claude Code session to apply the update.[/dim]")
        console.print("[dim]If the CLAUDE.md block content changed, regenerate it: ooo setup[/dim]")
    elif resolved_runtime != "none":
        console.print(f"[dim]Restart your {resolved_runtime} session to apply the update.[/dim]")
    console.print()
    if failed:
        raise typer.Exit(1)
