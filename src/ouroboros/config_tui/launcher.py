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


def launch_settings() -> None:
    """Launch the settings GUI in the mode that fits the environment."""
    if is_harness_context():
        _launch_web()
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


def _launch_web(*, open_browser: bool = True) -> None:
    server_cls = _import_server()
    if server_cls is None:
        print_error(_TUI_INSTALL_HINT)
        print_info("Manual fallback: run [bold]uv run ouroboros config[/] in a regular terminal.")
        raise SystemExit(1)

    port = _free_port()
    url = f"http://localhost:{port}"
    print_info(
        f"Serving Ouroboros Settings at [bold]{url}[/] — opening your browser.\n"
        "Press Ctrl+C to stop."
    )
    if open_browser:
        # serve() blocks; open the browser once the server has had a beat to bind.
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    command = f"{sys.executable} -m ouroboros.config_tui"
    server = server_cls(command, host="localhost", port=port, title="Ouroboros Settings")
    server.serve()


__all__ = ["is_harness_context", "launch_settings"]
