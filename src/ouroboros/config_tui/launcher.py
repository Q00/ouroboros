"""Environment-aware launch dispatch for the settings GUI (#1414).

A bare ``ouroboros config`` cannot host a full-screen TUI everywhere it is
typed. In a real terminal the Textual app runs in-place; inside an AI
harness session (Claude Code / Codex), the Bash tool captures output and
owns the screen, so the browser console (``ouroboros gui``) opens on its
Agents & models page instead. That page saves through the same validated,
undoable write path as the terminal app (``config_tui.persistence``).
"""

from __future__ import annotations

import os
import shutil
import sys

from ouroboros.cli.formatters.panels import print_error, print_info

# Note the escaped brackets: rich would otherwise treat [tui] as a markup tag.
_TUI_INSTALL_HINT = (
    "Settings GUI dependencies not installed.\n\n"
    "Install in the current environment:\n"
    "  pip install 'ouroboros-ai\\[tui]'\n\n"
    "Or run in an isolated MCP v2 profile with uvx:\n"
    "  uvx --isolated --python '>=3.12' --from 'ouroboros-ai\\[mcp,tui]' "
    "ouroboros config"
)


def is_harness_context() -> bool:
    """True when running inside an AI harness (or any non-interactive stdout).

    ``CLAUDECODE=1`` is exported by Claude Code to its child processes; a
    non-TTY stdout covers Codex-style harnesses and pipes generally.
    """
    if os.environ.get("CLAUDECODE", "").strip():
        return True
    return not sys.stdout.isatty()


def _bootstrap_profile_args() -> list[str]:
    """Return the published MCP v2 and settings GUI profile selector."""
    return ["--from", "ouroboros-ai[mcp,tui]"]


def _relaunch_with_tui_profile() -> bool:
    """Replace a base install with an isolated GUI-capable invocation."""
    if os.environ.get("OUROBOROS_CONFIG_TUI_BOOTSTRAPPED"):
        return False
    uvx = shutil.which("uvx")
    if uvx is None:
        return False
    env = os.environ.copy()
    env["OUROBOROS_CONFIG_TUI_BOOTSTRAPPED"] = "1"
    args = [
        uvx,
        "--isolated",
        "--python",
        ">=3.12",
        *_bootstrap_profile_args(),
        "ouroboros",
        "config",
        *sys.argv[2:],
    ]
    os.execvpe(uvx, args, env)
    return True


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
        port: Fixed port for web mode; ``None`` or ``0`` picks a free one.
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
        if _relaunch_with_tui_profile():
            return
        print_error(_TUI_INSTALL_HINT)
        raise SystemExit(1) from None
    SettingsApp().run()


def _console_serve(**kwargs: object) -> None:
    """Serve the browser console (indirection so tests can replace it)."""
    from ouroboros.gui.launcher import serve

    serve(**kwargs)  # type: ignore[arg-type]


def _launch_web(
    *,
    host: str = "localhost",
    port: int | None = None,
    open_browser: bool = True,
) -> None:
    """Open the console's Agents & models page (reusing a running console)."""
    bind = "127.0.0.1" if host == "localhost" else host

    def ready(url: str, reused: bool) -> None:
        if reused:
            print_info(f"Ouroboros Settings are open in the console at [bold]{url}[/].")
            return
        if open_browser:
            print_info(
                f"Serving Ouroboros Settings at [bold]{url}[/] — opening your browser.\n"
                "Press Ctrl+C to stop."
            )
            return
        shown_port = url.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[-1]
        print_info(
            f"Serving Ouroboros Settings at [bold]{url}[/] (no browser on this host).\n"
            "From your own machine, open the URL directly, or tunnel first:\n"
            f"  ssh -L {shown_port}:localhost:{shown_port} <this-host>\n"
            "Press Ctrl+C to stop."
        )

    _console_serve(
        host=bind,
        port=None if port in (None, 0) else port,
        open_browser=open_browser,
        page="models",
        on_ready=ready,
    )


__all__ = ["is_harness_context", "is_remote_session", "launch_settings"]
