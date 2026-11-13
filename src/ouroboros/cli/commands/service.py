"""``ouroboros service`` — run the console and shared MCP server at login."""

from __future__ import annotations

from typing import Annotated

import typer

from ouroboros.cli.formatters.panels import print_error, print_info, print_success

app = typer.Typer(
    name="service",
    help="Run the Ouroboros console (and its shared MCP server) in the background at login.",
    no_args_is_help=True,
)


def _show(state: dict) -> None:
    if not state["supported"]:
        print_error("Running at login is supported on macOS (launchd) and Linux (systemd).")
        return
    where = state["unit_path"]
    if not state["installed"]:
        print_info(f"Not installed ({state['manager']}). Run `ouroboros service install`.")
        return
    running = "running" if state["loaded"] else "installed but not running"
    pid = f" (pid {state['pid']})" if state.get("pid") else ""
    print_info(f"{state['manager']}: {running}{pid}\nUnit: {where}\nLog: {state['log_path']}")


@app.command("install")
def install(
    port: Annotated[int, typer.Option("--port", "-p", help="Console port.")] = 10300,
) -> None:
    """Install and start the login service."""
    from ouroboros.gui import service

    try:
        state = service.install(port)
    except service.ServiceError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from None
    print_success("Ouroboros console will start at login.")
    _show(state)


@app.command("uninstall")
def uninstall() -> None:
    """Stop and remove the login service."""
    from ouroboros.gui import service

    try:
        state = service.uninstall()
    except service.ServiceError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from None
    print_success("Removed the Ouroboros login service.")
    _show(state)


@app.command("status")
def status() -> None:
    """Show whether the login service is installed and running."""
    from ouroboros.gui import service

    _show(service.status())
