"""``ouroboros gui`` — the browser console for setup, settings, runs and chat."""

from __future__ import annotations

import sys
from typing import Annotated

import typer

from ouroboros.cli.formatters.panels import print_info


def _stdout_is_terminal() -> bool:
    """Whether someone is watching this process's output in a terminal.

    Not when the console was started in the background (a login service, an
    installer, ``nohup``), where Ctrl+C cannot reach it.
    """
    try:
        return bool(sys.stdout and sys.stdout.isatty())
    except (AttributeError, ValueError):  # replaced or closed stream
        return False


def _stop_hint() -> str:
    if _stdout_is_terminal():
        return "press Ctrl+C to stop."
    return "running in the background; stop it from the console's sidebar."


app = typer.Typer(
    name="gui",
    help="Open the Ouroboros console in your browser (connect tools, settings, runs, chat).",
    invoke_without_command=True,
)


@app.callback(invoke_without_command=True)
def gui(
    port: Annotated[
        int | None,
        typer.Option("--port", "-p", help="Port to serve on (default 10300, or any free port)."),
    ] = None,
    host: Annotated[
        str,
        typer.Option(
            "--host",
            help="Bind address. Use 0.0.0.0 to reach the console from another machine "
            "(the printed URL then carries an access token).",
        ),
    ] = "127.0.0.1",
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Print the URL without opening a browser.")
    ] = False,
    page: Annotated[
        str, typer.Option("--page", help="Page to open, e.g. models, integrations, chat.")
    ] = "",
    service: Annotated[
        bool,
        typer.Option(
            "--service", hidden=True, help="Login-service mode (used by launchd/systemd)."
        ),
    ] = False,
) -> None:
    """Serve the console until stopped; reuse a running one if there is one."""
    from ouroboros.gui.launcher import serve

    def ready(url: str, reused: bool) -> None:
        if reused:
            print_info(f"Ouroboros console is already running at [bold]{url}[/]")
        else:
            print_info(f"Ouroboros console at [bold]{url}[/] — {_stop_hint()}")
            if host not in ("127.0.0.1", "localhost", "::1"):
                print_info(
                    "Reachable from other machines. Keep the token in the URL private, or "
                    "tunnel instead: ssh -L <port>:localhost:<port> <this-host>"
                )

    serve(
        host=host,
        port=port,
        open_browser=not no_browser and not service,
        page=page,
        on_ready=ready,
        service=service,
    )
