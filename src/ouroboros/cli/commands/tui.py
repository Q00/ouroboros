"""TUI command for Ouroboros.

Launch the interactive TUI monitor for real-time workflow monitoring.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from typing import Annotated

import typer

from ouroboros.cli.formatters.panels import print_error, print_info, print_success
from ouroboros.persistence.event_store import EventStore

DEFAULT_DB_PATH = Path(os.path.expanduser("~/.ouroboros/ouroboros.db"))

app = typer.Typer(
    name="tui",
    help="Interactive TUI monitor for Ouroboros workflows.",
    no_args_is_help=False,
)


@app.command(name="monitor")
def monitor_command(
    db_path: Annotated[
        Path,
        typer.Option(
            "--db-path",
            help="Path to the Ouroboros database file to monitor.",
            resolve_path=True,
            show_default=True,
        ),
    ] = DEFAULT_DB_PATH,
    backend: Annotated[
        str,
        typer.Option(
            "--backend",
            help="TUI backend to use: 'python' (default) or 'slt' (native binary).",
        ),
    ] = "python",
) -> None:
    """Launch interactive TUI monitor.

    Starts a terminal UI that shows a list of all sessions found in the
    database. You can then select a session to monitor in real-time.
    """
    if backend == "slt":
        _run_slt_backend(db_path)
        return

    print_info(f"Connecting to database: {db_path}")

    try:
        from ouroboros.tui import OuroborosTUI
    except ImportError as e:
        print_error(
            "TUI dependencies not installed.\n\n"
            "Install with:\n"
            "  pip install 'ouroboros-ai[tui]'\n\n"
            "Or run directly with uvx:\n"
            "  uvx --from 'ouroboros-ai[tui]' ouroboros tui monitor",
        )
        raise typer.Exit(1) from e

    # Initialize EventStore
    db_path.parent.mkdir(parents=True, exist_ok=True)
    event_store = EventStore(f"sqlite+aiosqlite:///{db_path}")

    # Initialize and run the TUI
    async def init_and_run() -> None:
        await event_store.initialize()
        tui = OuroborosTUI(event_store=event_store)
        await tui.run_async()

    try:
        asyncio.run(init_and_run())
    except Exception as e:
        print_error(f"Failed to run TUI: {e}")
        raise typer.Exit(1) from None


@app.command(name="open")
def open_command(
    db_path: Annotated[
        Path,
        typer.Option(
            "--db-path",
            help="Path to the Ouroboros database file to monitor.",
            resolve_path=True,
            show_default=True,
        ),
    ] = DEFAULT_DB_PATH,
    cwd: Annotated[
        Path | None,
        typer.Option(
            "--cwd",
            help="Working directory for the spawned TUI process.",
            resolve_path=True,
            show_default=False,
        ),
    ] = None,
) -> None:
    """Open the TUI monitor in a new terminal window when possible."""
    launch = build_tui_open_launch(db_path=db_path, cwd=cwd, env=os.environ)
    if launch.argv is None:
        print_info(launch.message)
        print_info(f"Run this in another terminal:\n  {launch.manual_command}")
        return

    try:
        subprocess.Popen(
            launch.argv,
            cwd=launch.cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        print_info(f"Could not open a new terminal window ({e}).")
        print_info(f"Run this in another terminal:\n  {launch.manual_command}")
        return

    print_success(launch.message)
    print_info(f"Manual command if the window did not appear:\n  {launch.manual_command}")


class TUIOpenPlan:
    """Launch plan for `ouroboros tui open`."""

    def __init__(
        self,
        *,
        argv: list[str] | None,
        cwd: Path,
        manual_command: str,
        message: str,
    ) -> None:
        self.argv = argv
        self.cwd = cwd
        self.manual_command = manual_command
        self.message = message


def build_tui_open_launch(
    *,
    db_path: Path = DEFAULT_DB_PATH,
    cwd: Path | None = None,
    env: os._Environ[str] | dict[str, str] = os.environ,
) -> TUIOpenPlan:
    """Build the terminal-specific TUI launch plan."""
    resolved_cwd = (cwd or Path.cwd()).expanduser().resolve()
    monitor_argv = _monitor_argv(db_path.expanduser())
    manual_command = _manual_command(resolved_cwd, monitor_argv)

    if _is_headless(env):
        return TUIOpenPlan(
            argv=None,
            cwd=resolved_cwd,
            manual_command=manual_command,
            message="No local terminal window was detected for this SSH/headless session.",
        )

    term_program = env.get("TERM_PROGRAM", "")
    dispatch = _dispatch_for_terminal(term_program, resolved_cwd, monitor_argv)
    if dispatch is None:
        return TUIOpenPlan(
            argv=None,
            cwd=resolved_cwd,
            manual_command=manual_command,
            message=(
                f"Unsupported terminal {term_program!r}; open the TUI manually."
                if term_program
                else "Could not detect a supported terminal; open the TUI manually."
            ),
        )

    return TUIOpenPlan(
        argv=dispatch,
        cwd=resolved_cwd,
        manual_command=manual_command,
        message=f"Opening Ouroboros TUI monitor in {term_program}.",
    )


def _monitor_argv(db_path: Path) -> list[str]:
    entrypoint = shutil.which("ouroboros")
    if entrypoint:
        return [entrypoint, "tui", "monitor", "--db-path", str(db_path)]
    return [
        "uvx",
        "--from",
        "ouroboros-ai[tui]",
        "ouroboros",
        "tui",
        "monitor",
        "--db-path",
        str(db_path),
    ]


def _manual_command(cwd: Path, argv: list[str]) -> str:
    return f"cd {shlex.quote(str(cwd))} && {shlex.join(argv)}"


def _is_headless(env: os._Environ[str] | dict[str, str]) -> bool:
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY"):
        return True
    return bool(
        sys.platform.startswith("linux") and not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))
    )


def _dispatch_for_terminal(term_program: str, cwd: Path, argv: list[str]) -> list[str] | None:
    terminal = term_program.lower()
    if terminal == "ghostty":
        return ["open", "-na", "Ghostty.app", "--args", f"--working-directory={cwd}", "-e", *argv]
    if terminal == "iterm.app":
        return _osascript_iterm(cwd, argv)
    if terminal == "apple_terminal":
        return _osascript_apple_terminal(cwd, argv)
    if terminal == "wezterm":
        wezterm = shutil.which("wezterm") or "wezterm"
        return [wezterm, "start", "--cwd", str(cwd), "--", *argv]
    if terminal == "vscode":
        return None
    return _linux_terminal_dispatch(cwd, argv)


def _osascript_apple_terminal(cwd: Path, argv: list[str]) -> list[str]:
    script = (
        'tell application "Terminal"\n'
        "  activate\n"
        f"  do script {_applescript_quote(_manual_command(cwd, argv))}\n"
        "end tell"
    )
    return ["osascript", "-e", script]


def _osascript_iterm(cwd: Path, argv: list[str]) -> list[str]:
    script = (
        'tell application "iTerm"\n'
        "  activate\n"
        f"  create window with default profile command {_applescript_quote(_manual_command(cwd, argv))}\n"
        "end tell"
    )
    return ["osascript", "-e", script]


def _applescript_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _linux_terminal_dispatch(cwd: Path, argv: list[str]) -> list[str] | None:
    command = _manual_command(cwd, argv)
    for candidate in ("gnome-terminal", "x-terminal-emulator", "konsole"):
        terminal = shutil.which(candidate)
        if terminal is None:
            continue
        if candidate == "konsole":
            return [terminal, "--workdir", str(cwd), "-e", *argv]
        return [terminal, "--working-directory", str(cwd), "--", "sh", "-lc", command]
    return None


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
) -> None:
    """Interactive TUI monitor for Ouroboros workflows."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(monitor_command)


def _run_slt_backend(db_path: Path) -> None:
    bin_path = shutil.which("ouroboros-tui")
    if bin_path is None:
        print_error(
            "ouroboros-tui not found.\n\n"
            "Install options:\n"
            "  Download pre-built binary:\n"
            "    https://github.com/Q00/ouroboros/releases/latest\n\n"
            "  Build from source (requires Rust):\n"
            "    cargo install --path crates/ouroboros-tui",
        )
        raise typer.Exit(1)

    if not sys.stdin.isatty():
        print_error(
            "SLT backend requires an interactive terminal.\n\n"
            "This usually happens when running via 'uvx'. Instead:\n"
            "  1. Run the binary directly:\n"
            "       ouroboros-tui --db-path " + str(db_path) + "\n\n"
            "  2. Or install ouroboros first, then run:\n"
            "       pip install ouroboros-ai\n"
            "       ouroboros monitor --backend slt",
        )
        raise typer.Exit(1)

    args = [bin_path, "--db-path", str(db_path)]
    if os.name == "nt":
        sys.exit(subprocess.call(args))
    else:
        os.execv(bin_path, args)


__all__ = ["app"]
