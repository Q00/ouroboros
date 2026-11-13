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

Lazy registration:
    Top-level commands are declared in ``_COMMANDS`` and their modules are
    imported only when Click looks the command up, so ``ouroboros --version``
    or ``ouroboros job wait`` no longer imports every command module. Each
    command is converted exactly as ``app.add_typer`` / ``app.command``
    registration converted it, and ``--help``, completion, and dispatch see the
    same commands in the same order.
"""

from collections.abc import Iterator, Mapping, MutableMapping
import importlib
from typing import Annotated, Any, NamedTuple

import click
import typer
import typer.main

from ouroboros import __version__, telemetry
from ouroboros.cli.streams import UnicodeSafeTyperGroup

# Importing the config loader applies ``./.env`` and ``~/.ouroboros/.env`` to
# ``os.environ``. While this module imported every command module, that import
# happened at startup for every command (including ``codex`` and ``uninstall``,
# whose modules do not import the loader) and before any console was built
# (Rich reads NO_COLOR/FORCE_COLOR/COLUMNS on construction). Keep it here.
import ouroboros.config.loader  # noqa: F401


class _Group(NamedTuple):
    """A Typer sub-app, registered as ``app.add_typer(<module>.<attribute>, name=name)``."""

    name: str
    module: str
    attribute: str = "app"


class _Command(NamedTuple):
    """A function, registered as ``app.command(name=name, help=help, hidden=hidden)``."""

    name: str
    module: str
    attribute: str
    help: str | None = None
    hidden: bool = False


_COMMANDS_PACKAGE = "ouroboros.cli.commands"

# Every top-level command, in registration order. Add new commands here: the
# module is imported only when the command is looked up.
_COMMANDS: tuple[_Group | _Command, ...] = (
    _Command(
        "auto",
        f"{_COMMANDS_PACKAGE}.auto",
        "auto_command",
        help=(
            "Run bounded full-quality ooo auto pipeline.\n\n"
            "Detached auto background work is non-terminal tracked work. "
            "Wait for completion with:\n\n"
            "  ouroboros job wait JOB_ID\n\n"
            "Retrieve completed results with:\n\n"
            "  ouroboros job result JOB_ID"
        ),
    ),
    _Group("init", f"{_COMMANDS_PACKAGE}.init"),
    _Group("interview", f"{_COMMANDS_PACKAGE}.init"),
    _Command(
        "seed",
        f"{_COMMANDS_PACKAGE}.seed",
        "seed_command",
        help="Generate a Seed YAML specification from an interview.",
    ),
    _Group("run", f"{_COMMANDS_PACKAGE}.run"),
    _Group("job", f"{_COMMANDS_PACKAGE}.job"),
    _Group("artifacts", f"{_COMMANDS_PACKAGE}.artifacts"),
    _Group("harness", f"{_COMMANDS_PACKAGE}.harness"),
    _Group("config", f"{_COMMANDS_PACKAGE}.config"),
    _Group("status", f"{_COMMANDS_PACKAGE}.status"),
    _Group("cancel", f"{_COMMANDS_PACKAGE}.cancel"),
    _Group("cleanup", f"{_COMMANDS_PACKAGE}.cleanup"),
    _Group("codex", f"{_COMMANDS_PACKAGE}.codex"),
    _Group("doctor", f"{_COMMANDS_PACKAGE}.doctor"),
    _Group("mcp", f"{_COMMANDS_PACKAGE}.mcp"),
    _Group("setup", f"{_COMMANDS_PACKAGE}.setup"),
    _Group("detect", f"{_COMMANDS_PACKAGE}.detect"),
    _Group("tui", f"{_COMMANDS_PACKAGE}.tui"),
    _Group("pm", f"{_COMMANDS_PACKAGE}.pm"),
    _Command(
        "qa",
        f"{_COMMANDS_PACKAGE}.qa",
        "qa_command",
        help="General-purpose QA verdict for any artifact.",
    ),
    _Group("plugin", f"{_COMMANDS_PACKAGE}.plugin"),
    _Group("resume", f"{_COMMANDS_PACKAGE}.resume"),
    _Group("uninstall", f"{_COMMANDS_PACKAGE}.uninstall"),
    _Group("update", f"{_COMMANDS_PACKAGE}.update"),
    _Group("workflow-ir", f"{_COMMANDS_PACKAGE}.workflow_ir"),
    _Group("zcode", f"{_COMMANDS_PACKAGE}.zcode"),
    _Command("dispatch", f"{_COMMANDS_PACKAGE}.dispatch", "dispatch_command", hidden=True),
    # Top-level convenience alias, defined below.
    _Command("monitor", __name__, "monitor", hidden=True),
)

# Settings of the root ``typer.Typer``. Child commands are converted through a
# throwaway app built from the same settings, because Typer hands the parent's
# markup mode, exception, and metavar settings down while converting.
_APP_SETTINGS: dict[str, Any] = {
    "name": "ouroboros",
    "help": "Ouroboros - Self-Improving AI Workflow System",
    "no_args_is_help": True,
    "rich_markup_mode": "rich",
}


def _convert(registration: _Group | _Command) -> click.Command:
    """Import one registration and convert it the way root registration did."""
    target = getattr(importlib.import_module(registration.module), registration.attribute)
    owner = typer.Typer(**_APP_SETTINGS)
    if isinstance(registration, _Group):
        owner.add_typer(target, name=registration.name)
    else:
        owner.command(name=registration.name, help=registration.help, hidden=registration.hidden)(
            target
        )
    # ``get_group`` keeps a sub-app a group even when it has a single command;
    # ``get_command`` would collapse it.
    return typer.main.get_group(owner).commands[registration.name]


class _LazyCommands(MutableMapping[str, click.Command]):
    """The root command table, converting each command on first lookup.

    Names, membership, and iteration never import anything. Typer converts
    ``app.command`` registrations before ``add_typer`` ones, so that is the
    order the eager table had, and the order kept here.
    """

    def __init__(self, converted: Mapping[str, click.Command]) -> None:
        self._entries: dict[str, click.Command | _Group | _Command] = dict(converted)
        for registration in sorted(_COMMANDS, key=lambda item: isinstance(item, _Group)):
            self._entries.setdefault(registration.name, registration)

    def __getitem__(self, name: str) -> click.Command:
        entry = self._entries[name]
        # Test for the registration types: recent Typer releases build commands
        # from a vendored click, so they are not ``click.Command`` instances.
        if isinstance(entry, _Group | _Command):
            entry = self._entries[name] = _convert(entry)
        return entry

    def __setitem__(self, name: str, command: click.Command) -> None:
        self._entries[name] = command

    def __delitem__(self, name: str) -> None:
        del self._entries[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, name: object) -> bool:
        return name in self._entries


class _PluginAwareGroup(UnicodeSafeTyperGroup):
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
    first-party commands keep their fast path entirely, and the plugin
    machinery is imported only on this fallback.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.commands = _LazyCommands(self.commands)

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        cmd = super().get_command(ctx, cmd_name)
        if cmd is not None:
            return cmd
        from ouroboros.cli.commands.plugin_dispatch import build_plugin_dispatch_command

        return build_plugin_dispatch_command(cmd_name)


# Create the main Typer app. Commands come from ``_COMMANDS``.
app = typer.Typer(**_APP_SETTINGS, cls=_PluginAwareGroup)


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
    from ouroboros.cli.commands import tui

    tui.monitor_command(backend=backend)


def version_callback(value: bool) -> None:
    """Print version and exit."""
    if value:
        from ouroboros.cli.formatters import console

        console.print(f"[bold cyan]Ouroboros[/] version [green]{__version__}[/]")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: Annotated[  # noqa: ARG001 — consumed by the eager version_callback
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
        ouroboros zcode "Build a REST API"    Start with Zcode
        ouroboros monitor                     Launch TUI monitor

    Use [bold cyan]ouroboros COMMAND --help[/] for command-specific help.
    """
    telemetry.show_first_run_notice()
    telemetry.capture_cli_command(ctx.invoked_subcommand)


__all__ = ["app", "main"]
