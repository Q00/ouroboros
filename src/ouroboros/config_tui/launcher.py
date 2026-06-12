"""Environment-aware launch dispatch for the settings GUI (#1414).

A bare ``ouroboros config`` cannot host a full-screen TUI everywhere it is
typed. In a real terminal the Textual app runs in-place; inside an AI
harness session (Claude Code / Codex), the Bash tool captures output and
owns the screen, so the same app is served over a local web server
(textual-serve) and the browser is opened instead. Browser *cockpit*
dashboards remain ourocode territory per the transparency RFC (#1392) —
this web mode is a thin transport for the identical settings app, not a
separate web UI.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import webbrowser

from ouroboros.cli.formatters.panels import print_error, print_info

# Note the escaped brackets: rich would otherwise treat [tui] as a markup tag.
_TUI_INSTALL_HINT = (
    "Settings GUI dependencies not installed.\n\n"
    "Install with:\n"
    "  pip install 'ouroboros-ai\\[tui]'\n\n"
    "Or run directly with uvx:\n"
    "  uvx --from 'ouroboros-ai\\[tui]' ouroboros config"
)


def is_harness_context() -> bool:
    """True when running inside an AI harness (or any non-interactive stdout).

    ``CLAUDECODE=1`` is exported by Claude Code to its child processes; a
    non-TTY stdout covers Codex-style harnesses and pipes generally.
    """
    if os.environ.get("CLAUDECODE", "").strip():
        return True
    return not sys.stdout.isatty()


def is_remote_session() -> bool:
    """True when this process runs on a machine the user is not sitting at.

    Covers SSH sessions and headless gateways (e.g. hermes driven from
    Discord on another box): opening a browser *here* would open it on the
    wrong machine, so web mode must print a reachable URL instead.
    """
    return bool(
        os.environ.get("SSH_CONNECTION", "").strip() or os.environ.get("SSH_TTY", "").strip()
    )


def launch_settings(
    *,
    force_web: bool = False,
    host: str = "localhost",
    port: int | None = None,
    open_browser: bool | None = None,
) -> None:
    """Launch the settings GUI in the mode that fits the environment.

    Args:
        force_web: Skip TTY detection and serve over HTTP (for remote/agent
            hosts where no local screen exists).
        host: Bind address for web mode. Use ``0.0.0.0`` to reach the GUI
            from another machine (e.g. when the agent runs on a server).
        port: Fixed port for web mode; ``None`` picks a free one.
        open_browser: Override browser auto-open. ``None`` = open only when
            this is not a remote session.
    """
    if force_web or is_harness_context():
        if open_browser is None:
            open_browser = not is_remote_session()
        _launch_web(host=host, port=port, open_browser=open_browser)
    else:
        _launch_inline()


def _launch_inline() -> None:
    try:
        from ouroboros.config_tui.app import SettingsApp
    except ImportError:
        print_error(_TUI_INSTALL_HINT)
        raise SystemExit(1) from None
    SettingsApp().run()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _import_server() -> type | None:
    """Import textual-serve's Server, or ``None`` when the extra is missing."""
    try:
        from textual_serve.server import Server
    except ImportError:
        return None
    return Server


def _launch_web(
    *,
    host: str = "localhost",
    port: int | None = None,
    open_browser: bool = True,
) -> None:
    server_cls = _import_server()
    if server_cls is None:
        print_error(_TUI_INSTALL_HINT)
        print_info("Manual fallback: run [bold]uv run ouroboros config[/] in a regular terminal.")
        raise SystemExit(1)

    if port is None:
        port = _free_port()
    display_host = "localhost" if host in ("localhost", "127.0.0.1", "0.0.0.0") else host
    url = f"http://{display_host}:{port}"
    if open_browser:
        print_info(
            f"Serving Ouroboros Settings at [bold]{url}[/] — opening your browser.\n"
            "Press Ctrl+C to stop."
        )
        # serve() blocks; open the browser once the server has had a beat to bind.
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    else:
        # Remote/agent host: a browser opened *here* would be on the wrong
        # machine. Hand the user a reachable URL instead.
        print_info(
            f"Serving Ouroboros Settings at [bold]{url}[/] (no browser on this host).\n"
            "From your own machine, open the URL directly, or tunnel first:\n"
            f"  ssh -L {port}:localhost:{port} <this-host>\n"
            "Press Ctrl+C to stop."
        )
    command = f"{sys.executable} -m ouroboros.config_tui"
    server = server_cls(command, host=host, port=port, title="Ouroboros Settings")
    server.serve()


__all__ = ["is_harness_context", "is_remote_session", "launch_settings"]
