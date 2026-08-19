"""General Ouroboros diagnostic commands."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
from typing import Annotated, Any, Literal

from rich.console import Console
from rich.markup import escape
import typer

from ouroboros import __version__
from ouroboros.cli.commands.update import _compare_versions
from ouroboros.codex.home import resolve_codex_home

Status = Literal["pass", "warn", "fail"]

app = typer.Typer(
    name="doctor",
    help="Run read-only diagnostics for Ouroboros installations.",
    no_args_is_help=True,
)

_SYMBOLS: dict[Status, str] = {
    "pass": "[green]OK[/green]",
    "warn": "[yellow]WARN[/yellow]",
    "fail": "[red]FAIL[/red]",
}


@dataclass(frozen=True, slots=True)
class InstallSurface:
    """One observed installation or integration surface."""

    name: str
    status: Status
    message: str
    path: str | None = None
    version: str | None = None
    expected_version: str | None = None
    launcher: str | None = None
    remediation: str | None = None


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return None, f"invalid UTF-8 at byte {exc.start}: {exc.reason}"
    except OSError as exc:
        return None, str(exc)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}"
    if not isinstance(parsed, dict):
        return None, "expected a JSON object"
    return parsed, None


def _manifest_version(path: Path) -> tuple[str | None, str | None]:
    parsed, error = _read_json(path)
    if error is not None:
        return None, error
    version = parsed.get("version") if parsed else None
    if not isinstance(version, str) or not version:
        return None, "manifest has no string version"
    return version, None


def _launcher_parts(entry: object) -> tuple[str | None, list[str] | None, str | None]:
    if not isinstance(entry, dict):
        return None, None, "launcher entry is not an object"
    command = entry.get("command")
    args = entry.get("args", [])
    if not isinstance(command, str) or not command.strip():
        return None, None, "launcher command is missing or empty"
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        return None, None, "launcher args must be a list of strings"
    return command.strip(), args, None


def _format_launcher(entry: object) -> str | None:
    command, args, error = _launcher_parts(entry)
    if error is not None or command is None or args is None:
        return None
    return " ".join([command, *args]).strip()


def _read_mcp_launcher(path: Path) -> tuple[str | None, str | None, str | None, list[str] | None]:
    parsed, error = _read_json(path)
    if error is not None:
        return None, error, None, None
    servers = parsed.get("mcpServers") if parsed else None
    if not isinstance(servers, dict):
        return None, "missing mcpServers object", None, None
    entry = servers.get("ouroboros")
    if entry is None:
        return None, "missing mcpServers.ouroboros launcher", None, None
    launcher = _format_launcher(entry)
    command, args, parts_error = _launcher_parts(entry)
    if launcher is None:
        return None, parts_error or "invalid mcpServers.ouroboros launcher", None, None
    return launcher, None, command, args


def _version_status(version: str, expected: str) -> tuple[Status, str | None]:
    if expected == "0.0.0":
        return "pass", (
            "Running package reports editable/dev version 0.0.0, so plugin "
            "freshness is not inferred from that version string."
        )
    if version == expected:
        return "pass", None
    return "warn", (
        "This surface version differs from the running Ouroboros package. "
        "Update or reinstall that client integration before trusting CLI version alone."
    )


def _newer_version_name(left: str, right: str) -> str:
    comparison = _compare_versions(left, right)
    if comparison == 0:
        return max(left, right)
    return left if comparison > 0 else right


def _latest_versioned_child_with(path: Path, marker: str) -> tuple[Path | None, str | None]:
    if not path.is_dir():
        return None, None
    try:
        candidates = [child for child in path.iterdir() if (child / marker).exists()]
    except OSError as exc:
        return None, str(exc)
    if not candidates:
        return None, None
    latest = candidates[0]
    for candidate in candidates[1:]:
        if _newer_version_name(candidate.name, latest.name) == candidate.name:
            latest = candidate
    return latest, None


def _add_manifest_surface(
    surfaces: list[InstallSurface],
    *,
    name: str,
    path: Path,
    expected_version: str,
    version_label: str = "plugin",
    stale_check: bool = True,
) -> None:
    if not path.exists():
        surfaces.append(
            InstallSurface(
                name=name,
                status="warn",
                message=f"{version_label} manifest not found",
                path=str(path),
            )
        )
        return

    version, error = _manifest_version(path)
    if error is not None:
        surfaces.append(
            InstallSurface(
                name=name,
                status="fail",
                message=f"could not read {version_label} manifest: {error}",
                path=str(path),
            )
        )
        return

    if stale_check:
        status, remediation = _version_status(version or "", expected_version)
        if status == "pass":
            message = f"{version_label} version {version}"
            if expected_version == "0.0.0":
                message += " (package is editable/dev 0.0.0; stale comparison skipped)"
        else:
            message = f"{version_label} version {version} differs from package {expected_version}"
    else:
        status = "pass"
        remediation = None
        message = f"{version_label} version {version}"

    surfaces.append(
        InstallSurface(
            name=name,
            status=status,
            message=message,
            path=str(path),
            version=version,
            expected_version=expected_version if stale_check else None,
            remediation=remediation,
        )
    )


def _add_launcher_surface(
    surfaces: list[InstallSurface],
    *,
    name: str,
    path: Path,
    require_isolated_uvx: bool = False,
) -> None:
    if not path.exists():
        surfaces.append(
            InstallSurface(
                name=name,
                status="warn",
                message="MCP launcher not found",
                path=str(path),
            )
        )
        return

    launcher, error, command, args = _read_mcp_launcher(path)
    if error is not None:
        surfaces.append(
            InstallSurface(
                name=name,
                status="fail",
                message=f"could not read MCP launcher: {error}",
                path=str(path),
            )
        )
        return

    if require_isolated_uvx and not _is_claude_isolated_uvx_launcher(command, args):
        surfaces.append(
            InstallSurface(
                name=name,
                status="warn",
                message="launcher does not use isolated uvx",
                path=str(path),
                launcher=launcher,
                remediation="Update the plugin so it uses `uvx --isolated --python >=3.12`.",
            )
        )
        return

    surfaces.append(
        InstallSurface(
            name=name,
            status="pass",
            message="MCP launcher present",
            path=str(path),
            launcher=launcher,
        )
    )


def _is_claude_isolated_uvx_launcher(command: str | None, args: list[str] | None) -> bool:
    if command != "uvx" or args is None:
        return False
    if len(args) != 12:
        return False
    if args[0] != "--isolated":
        return False
    if args[1:3] != ["--python", ">=3.12"]:
        return False
    return args[3:] == [
        "--from",
        "ouroboros-ai[mcp]",
        "ouroboros",
        "mcp",
        "serve",
        "--runtime",
        "claude-cli",
        "--llm-backend",
        "claude_code",
    ]


def _resolve_install_codex_home(home: Path | None) -> Path:
    if os.environ.get("CODEX_HOME"):
        return resolve_codex_home()
    if home is not None:
        return home / ".codex"
    return resolve_codex_home()


def collect_install_surfaces(home: Path | None = None) -> list[InstallSurface]:
    """Collect read-only installation diagnostics from known user-level locations."""

    resolved_home = home or Path.home()
    expected_version = __version__
    surfaces: list[InstallSurface] = [
        InstallSurface(
            name="python_package",
            status="pass",
            message=f"running Ouroboros package version {expected_version}",
            version=expected_version,
        )
    ]

    codex_manifest = resolved_home / "plugins" / "ouroboros" / ".codex-plugin" / "plugin.json"
    _add_manifest_surface(
        surfaces,
        name="codex_personal_plugin",
        path=codex_manifest,
        expected_version=expected_version,
    )

    codex_home = _resolve_install_codex_home(home)
    codex_cache_root = codex_home / "plugins" / "cache" / "personal" / "ouroboros"
    codex_cache, codex_cache_error = _latest_versioned_child_with(codex_cache_root, ".mcp.json")
    if codex_cache_error is not None:
        surfaces.append(
            InstallSurface(
                name="codex_personal_plugin_launcher",
                status="fail",
                message=f"could not inspect Codex personal plugin cache: {codex_cache_error}",
                path=str(codex_cache_root),
            )
        )
    elif codex_cache is None:
        surfaces.append(
            InstallSurface(
                name="codex_personal_plugin_launcher",
                status="warn",
                message="Codex personal plugin cache launcher not found",
                path=str(codex_cache_root),
            )
        )
    else:
        _add_launcher_surface(
            surfaces,
            name="codex_personal_plugin_launcher",
            path=codex_cache / ".mcp.json",
        )

    claude_manifest = (
        resolved_home
        / ".claude"
        / "plugins"
        / "marketplaces"
        / "ouroboros"
        / ".claude-plugin"
        / "plugin.json"
    )
    _add_manifest_surface(
        surfaces,
        name="claude_plugin_marketplace",
        path=claude_manifest,
        expected_version=expected_version,
    )

    claude_cache_root = resolved_home / ".claude" / "plugins" / "cache" / "ouroboros" / "ouroboros"
    claude_cache, claude_cache_error = _latest_versioned_child_with(claude_cache_root, ".mcp.json")
    if claude_cache_error is not None:
        surfaces.append(
            InstallSurface(
                name="claude_plugin_launcher",
                status="fail",
                message=f"could not inspect Claude plugin cache: {claude_cache_error}",
                path=str(claude_cache_root),
            )
        )
    elif claude_cache is None:
        surfaces.append(
            InstallSurface(
                name="claude_plugin_launcher",
                status="warn",
                message="Claude plugin cache launcher not found",
                path=str(claude_cache_root),
            )
        )
    else:
        _add_launcher_surface(
            surfaces,
            name="claude_plugin_launcher",
            path=claude_cache / ".mcp.json",
            require_isolated_uvx=True,
        )

    claude_user_mcp = resolved_home / ".claude" / "mcp.json"
    if claude_user_mcp.exists():
        parsed, error = _read_json(claude_user_mcp)
        if error is not None:
            surfaces.append(
                InstallSurface(
                    name="claude_user_mcp_duplicate",
                    status="fail",
                    message=f"could not read Claude user MCP config: {error}",
                    path=str(claude_user_mcp),
                )
            )
        else:
            servers = parsed.get("mcpServers") if parsed else None
            has_duplicate = isinstance(servers, dict) and "ouroboros" in servers
            if has_duplicate:
                surfaces.append(
                    InstallSurface(
                        name="claude_user_mcp_duplicate",
                        status="warn",
                        message="user-level Claude MCP also defines `ouroboros`",
                        path=str(claude_user_mcp),
                        launcher=_format_launcher(servers.get("ouroboros")),
                        remediation=(
                            "Prefer the Claude Code plugin-managed server, or ensure this "
                            "user-level entry uses the same current isolated launcher."
                        ),
                    )
                )
            else:
                surfaces.append(
                    InstallSurface(
                        name="claude_user_mcp_duplicate",
                        status="pass",
                        message="no user-level Claude `ouroboros` MCP entry",
                        path=str(claude_user_mcp),
                    )
                )
    else:
        surfaces.append(
            InstallSurface(
                name="claude_user_mcp_duplicate",
                status="pass",
                message="Claude user MCP config not present",
                path=str(claude_user_mcp),
            )
        )

    zcode_cache_root = (
        resolved_home
        / ".zcode"
        / "cli"
        / "plugins"
        / "cache"
        / "zcode-plugins-official"
        / "ouroboros"
    )
    zcode_cache, zcode_cache_error = _latest_versioned_child_with(
        zcode_cache_root,
        ".zcode-plugin/plugin.json",
    )
    if zcode_cache_error is not None:
        surfaces.append(
            InstallSurface(
                name="zcode_bridge_plugin",
                status="fail",
                message=f"could not inspect ZCode bridge plugin cache: {zcode_cache_error}",
                path=str(zcode_cache_root),
            )
        )
        surfaces.append(
            InstallSurface(
                name="zcode_bridge_launcher",
                status="fail",
                message=f"could not inspect ZCode bridge plugin cache: {zcode_cache_error}",
                path=str(zcode_cache_root),
            )
        )
        return surfaces
    zcode_manifest = (zcode_cache or zcode_cache_root) / ".zcode-plugin" / "plugin.json"
    _add_manifest_surface(
        surfaces,
        name="zcode_bridge_plugin",
        path=zcode_manifest,
        expected_version=expected_version,
        version_label="bridge plugin",
        stale_check=False,
    )

    zcode_launcher = zcode_manifest.parent.parent / ".mcp.json"
    _add_launcher_surface(
        surfaces,
        name="zcode_bridge_launcher",
        path=zcode_launcher,
    )

    return surfaces


@app.command("install")
def install_doctor(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON to stdout."),
    ] = False,
) -> None:
    """Inspect package, plugin, and MCP install surfaces without mutating files."""

    surfaces = collect_install_surfaces()

    if as_json:
        print(json.dumps([asdict(surface) for surface in surfaces], indent=2))
    else:
        console = Console()
        console.print()
        console.print("[bold]Ouroboros Installation Doctor[/bold]")
        console.print()
        for surface in surfaces:
            console.print(
                f"  {_SYMBOLS[surface.status]}  [bold]{surface.name}[/bold]: "
                f"{escape(surface.message)}"
            )
            if surface.path:
                console.print(f"      [dim]path: {escape(surface.path)}[/dim]")
            if surface.launcher:
                console.print(f"      [dim]launcher: {escape(surface.launcher)}[/dim]")
            if surface.remediation:
                console.print(f"      [dim]hint: {escape(surface.remediation)}[/dim]")
        console.print()

    if any(surface.status == "fail" for surface in surfaces):
        raise typer.Exit(code=1)


__all__ = ["InstallSurface", "app", "collect_install_surfaces", "install_doctor"]
