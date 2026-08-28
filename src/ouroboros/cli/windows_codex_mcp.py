"""Native-Windows Codex Desktop MCP topology policy."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import tomllib

from rich.markup import escape

HTTP_MCP_SECTION = """# Ouroboros native-Windows HTTP MCP (explicit opt-in; no persistence).
[mcp_servers.ouroboros]
url = "http://127.0.0.1:8765/mcp"
"""


@dataclass(frozen=True, slots=True)
class WindowsCodexMcpDecision:
    handled: bool
    success: bool
    rendered_section: str | None = None
    message: str | None = None
    error: str | None = None


def is_native_windows() -> bool:
    return os.name == "nt"


def _launcher_is_usable(launcher: tuple[str, list[str]]) -> bool:
    command, args = launcher
    try:
        result = subprocess.run(
            [command, *args, "--help"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def resolve_windows_codex_mcp_mode(
    mode: str,
    *,
    codex_config: Path,
    launcher: tuple[str, list[str]] | None,
) -> WindowsCodexMcpDecision:
    """Resolve native-Windows modes without installing background persistence."""
    existing_entry = False
    if codex_config.exists():
        try:
            parsed = tomllib.loads(codex_config.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            return WindowsCodexMcpDecision(
                handled=True,
                success=False,
                error=f"Could not parse {codex_config} — Codex setup not saved.",
            )
        servers = parsed.get("mcp_servers")
        existing_entry = isinstance(servers, dict) and isinstance(servers.get("ouroboros"), dict)
    if mode == "stdio":
        return WindowsCodexMcpDecision(
            handled=True,
            success=False,
            error=(
                "Native-Windows Codex Desktop stdio MCP can terminate the app server. "
                "Use --mcp-mode http and run the printed loopback server command, or use WSL 2."
            ),
        )
    if mode == "auto":
        return WindowsCodexMcpDecision(
            handled=True,
            success=True,
            message=(
                "Preserved existing Codex MCP config on native Windows."
                if existing_entry
                else "Skipped persistent MCP registration on native Windows. "
                "Use --mcp-mode http for the explicit loopback HTTP topology."
            ),
        )
    if mode != "http":
        return WindowsCodexMcpDecision(handled=False, success=False)
    if launcher is None:
        return WindowsCodexMcpDecision(
            handled=True,
            success=False,
            error="Could not find a launchable MCP [mcp] command for explicit HTTP mode.",
        )
    if not _launcher_is_usable(launcher):
        return WindowsCodexMcpDecision(
            handled=True,
            success=False,
            error="The selected MCP launcher failed its `mcp serve --help` activation probe.",
        )
    command, launcher_args = launcher
    http_args = [
        *launcher_args,
        "--transport",
        "streamable-http",
        "--host",
        "127.0.0.1",
        "--port",
        "8765",
    ]
    rendered = subprocess.list2cmdline([command, *http_args])
    return WindowsCodexMcpDecision(
        handled=True,
        success=True,
        rendered_section=HTTP_MCP_SECTION,
        message=f"Start the MCP server before opening Codex Desktop: {rendered}",
    )


def apply_windows_codex_mcp_mode(
    mode: str,
    *,
    codex_config: Path,
    launcher: tuple[str, list[str]] | None,
    print_error: object,
    print_info: object,
) -> tuple[bool, bool, str | None]:
    """Resolve the mode, report its message, and return integration state."""
    decision = resolve_windows_codex_mcp_mode(
        mode,
        codex_config=codex_config,
        launcher=launcher,
    )
    if decision.error:
        print_error(decision.error)  # type: ignore[operator]
    if decision.message:
        print_info(escape(decision.message))  # type: ignore[operator]
    return decision.handled, decision.success, decision.rendered_section
