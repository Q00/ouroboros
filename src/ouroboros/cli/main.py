"""Ouroboros CLI main entry point.

This module defines the main Typer application and registers
all command groups for the Ouroboros CLI.

Command shortcuts (v0.8.0+):
    ouroboros run seed.yaml          # shorthand for: ouroboros run workflow seed.yaml
    ouroboros init "Build an API"    # shorthand for: ouroboros init start "Build an API"
    ouroboros monitor                # shorthand for: ouroboros tui monitor

Plugin dispatch (v0.10+):
    ouroboros <plugin-name> <command> [args...]
        Dispatches to an installed UserLevel plugin via the firewall.
        See `docs/rfc/userlevel-plugins.md` for the contract.
"""

from typing import Annotated

import click
import typer
from typer.core import TyperGroup

from ouroboros import __version__
from ouroboros.cli.commands import (
    auto,
    cancel,
    codex,
    config,
    detect,
    init,
    mcp,
    plugin,
    pm,
    resume,
    run,
    setup,
    status,
    tui,
    uninstall,
)
from ouroboros.cli.commands.plugin_dispatch import build_plugin_dispatch_command
from ouroboros.cli.formatters import console


class _PluginAwareGroup(TyperGroup):
    """A typer/click group that falls back to plugin dispatch for
    unknown top-level command names.

    When the user runs ``ooo <name> <command> [args...]`` and ``<name>``
    is not a registered subcommand of the main CLI, this group asks
    ``build_plugin_dispatch_command(name)`` for a one-shot Click
    command that resolves ``<name>`` against the user's installed
    plugin lockfile and invokes it through the firewall. If no
    matching plugin is installed, control falls back to the default
    "no such command" error path so users get the standard typer
    diagnostic rather than a silent no-op.

    The dispatch lookup is deliberately localized to this method so it
    runs only when typer's own resolution fails — registered
    first-party commands keep their fast path entirely.
    """

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        cmd = super().get_command(ctx, cmd_name)
        if cmd is not None:
            return cmd
        return build_plugin_dispatch_command(cmd_name)


# Create the main Typer app
app = typer.Typer(
    name="ouroboros",
    help="Ouroboros - Self-Improving AI Workflow System",
    no_args_is_help=True,
    rich_markup_mode="rich",
    cls=_PluginAwareGroup,
)

# Register direct commands and command groups
app.command(name="auto", help="Run bounded full-quality ooo auto pipeline.")(auto.auto_command)
app.add_typer(init.app, name="init")
app.add_typer(run.app, name="run")
app.add_typer(config.app, name="config")
app.add_typer(status.app, name="status")
app.add_typer(cancel.app, name="cancel")
app.add_typer(codex.app, name="codex")
app.add_typer(mcp.app, name="mcp")
app.add_typer(setup.app, name="setup")
app.add_typer(detect.app, name="detect")
app.add_typer(tui.app, name="tui")
app.add_typer(pm.app, name="pm")
app.add_typer(plugin.app, name="plugin")
app.add_typer(resume.app, name="resume")
app.add_typer(uninstall.app, name="uninstall")


# Top-level convenience aliases
@app.command(hidden=True)
def monitor(
    backend: Annotated[
        str,
        typer.Option(
            "--backend",
            help="TUI backend to use: 'python' (default) or 'slt' (native binary).",
        ),
    ] = "python",
) -> None:
    """Launch the TUI monitor (shorthand for 'ouroboros tui monitor')."""
    tui.monitor_command(backend=backend)


def version_callback(value: bool) -> None:
    """Print version and exit."""
    if value:
        console.print(f"[bold cyan]Ouroboros[/] version [green]{__version__}[/]")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            "-V",
            callback=version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = None,
) -> None:
    """Ouroboros - Self-Improving AI Workflow System.

    A self-improving AI workflow system with 6 phases:
    Big Bang, PAL Router, Execution, Resilience, Evaluation, and Consensus.

    [bold]Quick Start:[/]

        ouroboros init "Build a REST API"     Start interview
        ouroboros run seed.yaml               Execute workflow
        ouroboros monitor                     Launch TUI monitor

    Use [bold cyan]ouroboros COMMAND --help[/] for command-specific help.
    """
    pass


__all__ = ["app", "main"]
