"""Lazy top-level command registry in ``ouroboros.cli.main``.

``cli/main.py`` declares every top-level command in ``_COMMANDS`` and imports a
command's module only when Click looks the command up. These tests hold it to
the eager registration it replaced:

* ``_eager_reference_app`` is the previous registration, kept here as the
  reference: ``app.add_typer`` / ``app.command`` for every command, onto a root
  group with the previous plugin fallback. It is driven by the same table, so a
  command added to ``_COMMANDS`` is compared against what eager registration
  makes of it.
* Help for the root and for every command path (hidden ones included), error
  output, telemetry, and shell completion must be byte-identical.
* A fresh interpreter running ``ouroboros --version`` imports no command module
  and none of their heavy dependencies, but still applies ``.env`` at startup.
"""

from __future__ import annotations

from collections.abc import Iterator
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import typer
import typer.main
from typer.testing import CliRunner

from ouroboros import telemetry
from ouroboros.cli import main as cli_main
from ouroboros.cli.streams import UnicodeSafeTyperGroup

REPO_ROOT = Path(__file__).resolve().parents[3]


class _EagerPluginAwareGroup(UnicodeSafeTyperGroup):
    """The root group before lazy loading: plain table, plugin fallback on a miss."""

    def get_command(self, ctx: Any, cmd_name: str) -> Any:
        cmd = super().get_command(ctx, cmd_name)
        if cmd is not None:
            return cmd
        from ouroboros.cli.commands.plugin_dispatch import build_plugin_dispatch_command

        return build_plugin_dispatch_command(cmd_name)


def _eager_reference_app() -> typer.Typer:
    """Register every command up front, the way ``cli/main.py`` used to."""
    reference = typer.Typer(**cli_main._APP_SETTINGS, cls=_EagerPluginAwareGroup)
    for registration in cli_main._COMMANDS:
        module = importlib.import_module(registration.module)
        target = getattr(module, registration.attribute)
        if isinstance(registration, cli_main._Group):
            reference.add_typer(target, name=registration.name)
        else:
            reference.command(
                name=registration.name, help=registration.help, hidden=registration.hidden
            )(target)
    reference.callback()(cli_main.main)
    return reference


def _command_paths(command: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    """Yield every command path below ``command``, hidden commands included."""
    yield path
    if not hasattr(command, "list_commands"):
        return
    ctx = typer.Context(command, info_name=path[-1] if path else "ouroboros")
    for name in command.list_commands(ctx):
        yield from _command_paths(command.get_command(ctx, name), (*path, name))


@pytest.fixture
def telemetry_calls(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    calls: list[object] = []
    monkeypatch.setattr(telemetry, "show_first_run_notice", lambda: None)
    monkeypatch.setattr(telemetry, "capture_cli_command", calls.append)
    return calls


def _observe(app: typer.Typer, args: list[str], calls: list[object]) -> dict[str, object]:
    calls.clear()
    result = CliRunner().invoke(app, args, prog_name="ouroboros")
    error = result.exception
    return {
        "exit_code": result.exit_code,
        "output": result.output,
        "telemetry": list(calls),
        "exception": None if error is None or isinstance(error, SystemExit) else repr(error),
    }


# Invocations that must fail (or exit early) identically: typo suggestions read
# the table's names, unknown names reach the plugin fallback, and option and
# argument errors are raised by the lazily converted command.
_EDGE_INVOCATIONS = [
    [],
    ["--version"],
    ["-V"],
    ["--bogus"],
    ["stauts"],
    ["confg", "show"],
    ["no-such-plugin", "run"],
    ["job"],
    ["job", "wait"],
    ["job", "wait", "--bogus"],
    ["config", "nope"],
    ["monitor", "--bogus"],
]


def test_every_help_page_and_edge_case_matches_eager_registration(
    telemetry_calls: list[object],
) -> None:
    reference = _eager_reference_app()
    eager_paths = list(_command_paths(typer.main.get_command(reference)))
    lazy_paths = list(_command_paths(typer.main.get_command(cli_main.app)))

    assert lazy_paths == eager_paths
    # Root, every top-level command, and their subcommands.
    assert len(eager_paths) > len(cli_main._COMMANDS) + 1

    invocations = [[*path, "--help"] for path in eager_paths] + _EDGE_INVOCATIONS
    mismatches = [
        " ".join(args) or "<no arguments>"
        for args in invocations
        if _observe(cli_main.app, args, telemetry_calls)
        != _observe(reference, args, telemetry_calls)
    ]

    assert not mismatches, f"lazy and eager CLI output differ for: {mismatches}"


def test_help_page_comparison_detects_a_different_surface(
    telemetry_calls: list[object],
) -> None:
    """Guard the comparison itself: one changed help string must show up."""
    reference = _eager_reference_app()
    changed = typer.Typer(**cli_main._APP_SETTINGS, cls=_EagerPluginAwareGroup)
    changed.registered_commands = list(reference.registered_commands)
    changed.registered_groups = reference.registered_groups[:-1]
    changed.callback()(cli_main.main)

    assert _observe(changed, ["--help"], telemetry_calls) != _observe(
        reference, ["--help"], telemetry_calls
    )


@pytest.mark.parametrize("shell", ["zsh", "bash", "fish"])
@pytest.mark.parametrize(
    "words", ["ouroboros ", "ouroboros j", "ouroboros job ", "ouroboros mcp s"]
)
def test_shell_completion_matches_eager_registration(shell: str, words: str) -> None:
    env = {"_OUROBOROS_COMPLETE": f"complete_{shell}", "_TYPER_COMPLETE_ARGS": words}
    if shell == "bash":
        env = {
            "_OUROBOROS_COMPLETE": "complete_bash",
            "COMP_WORDS": words,
            "COMP_CWORD": str(len(words.split(" ")) - 1),
        }
    elif shell == "fish":
        env["_TYPER_COMPLETE_FISH_ACTION"] = "get-args"

    def complete(app: typer.Typer) -> tuple[int, str]:
        result = CliRunner().invoke(app, [], env=env, prog_name="ouroboros")
        return result.exit_code, result.output

    lazy = complete(cli_main.app)

    assert lazy == complete(_eager_reference_app())
    assert lazy[1].strip()


def test_lookup_converts_only_the_requested_command(telemetry_calls: list[object]) -> None:
    with patch.object(cli_main, "_convert", wraps=cli_main._convert) as convert:
        version = CliRunner().invoke(cli_main.app, ["--version"])
        assert version.exit_code == 0
        assert convert.call_count == 0

        config_help = CliRunner().invoke(cli_main.app, ["config", "--help"])
        assert config_help.exit_code == 0
        assert [call.args[0].name for call in convert.call_args_list] == ["config"]

        convert.reset_mock()
        root_help = CliRunner().invoke(cli_main.app, ["--help"])
        assert root_help.exit_code == 0
        assert sorted(call.args[0].name for call in convert.call_args_list) == sorted(
            registration.name for registration in cli_main._COMMANDS
        )


def test_table_names_membership_and_length_do_not_convert() -> None:
    group = typer.main.get_command(cli_main.app)
    with patch.object(cli_main, "_convert", wraps=cli_main._convert) as convert:
        names = list(group.commands)
        assert "job" in group.commands
        assert "evolve" not in group.commands
        assert len(group.commands) == len(names)
        assert list(group.commands.keys()) == names
        assert convert.call_count == 0

    assert len(set(names)) == len(cli_main._COMMANDS)
    # Typer lists ``app.command`` registrations ahead of ``add_typer`` ones.
    commands = [r.name for r in cli_main._COMMANDS if isinstance(r, cli_main._Command)]
    groups = [r.name for r in cli_main._COMMANDS if isinstance(r, cli_main._Group)]
    assert names == commands + groups


def test_shared_sub_app_is_named_after_each_registration() -> None:
    group = typer.main.get_command(cli_main.app)
    ctx = typer.Context(group, info_name="ouroboros")

    init = group.get_command(ctx, "init")
    interview = group.get_command(ctx, "interview")

    assert (init.name, interview.name) == ("init", "interview")
    assert init is not interview
    assert list(init.commands) == list(interview.commands)


def test_converted_command_is_reused_within_one_group() -> None:
    group = typer.main.get_command(cli_main.app)
    ctx = typer.Context(group, info_name="ouroboros")
    with patch.object(cli_main, "_convert", wraps=cli_main._convert) as convert:
        first = group.get_command(ctx, "status")
        second = group.get_command(ctx, "status")

    assert first is second
    assert convert.call_count == 1


def test_already_converted_entry_takes_precedence_over_the_table() -> None:
    sentinel = MagicMock(name="converted-auto")
    with patch.object(cli_main, "_convert", wraps=cli_main._convert) as convert:
        table = cli_main._LazyCommands({"auto": sentinel})
        assert table["auto"] is sentinel
        assert list(table)[0] == "auto"
        assert len(table) == len(cli_main._COMMANDS)
        assert convert.call_count == 0


def test_root_table_accepts_added_and_removed_commands() -> None:
    group = typer.main.get_command(cli_main.app)
    ctx = typer.Context(group, info_name="ouroboros")
    extra = MagicMock(name="extra-command")

    group.add_command(extra, "extra")
    assert group.get_command(ctx, "extra") is extra
    assert list(group.commands)[-1] == "extra"

    del group.commands["extra"]
    assert "extra" not in group.commands


def test_unknown_name_still_reaches_plugin_dispatch() -> None:
    group = typer.main.get_command(cli_main.app)
    ctx = typer.Context(group, info_name="ouroboros")
    plugin_command = MagicMock(name="plugin-command")
    with patch(
        "ouroboros.cli.commands.plugin_dispatch.build_plugin_dispatch_command",
        return_value=plugin_command,
    ) as build:
        assert group.get_command(ctx, "acme-plugin") is plugin_command

    build.assert_called_once_with("acme-plugin")


def test_import_failure_of_a_registered_command_is_not_masked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken command module surfaces its error; it never falls through to
    plugin dispatch or to "No such command"."""
    monkeypatch.setattr(
        cli_main,
        "_COMMANDS",
        (*cli_main._COMMANDS, cli_main._Group("broken", "ouroboros.cli.commands._missing_module")),
    )
    group = typer.main.get_command(cli_main.app)
    ctx = typer.Context(group, info_name="ouroboros")
    with (
        patch("ouroboros.cli.commands.plugin_dispatch.build_plugin_dispatch_command") as build,
        pytest.raises(ModuleNotFoundError, match="_missing_module"),
    ):
        group.get_command(ctx, "broken")

    build.assert_not_called()


_STARTUP_PROBE = """
import json, sys
sys.argv = ["ouroboros", *json.loads(sys.argv[1])]
try:
    from ouroboros.cli.entrypoint import main
    main()
except SystemExit:
    pass
print(json.dumps(sorted(sys.modules)), file=sys.stderr)
"""


def _startup_modules(args: list[str], tmp_path: Path) -> tuple[set[str], str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"NO_COLOR", "FORCE_COLOR", "COLORTERM"}
    }
    env.update(
        HOME=str(home),
        PYTHONPATH=str(REPO_ROOT / "src"),
        OUROBOROS_TELEMETRY="0",
        TERM="xterm",
    )
    result = subprocess.run(
        [sys.executable, "-c", _STARTUP_PROBE, json.dumps(args)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return set(json.loads(result.stderr.strip().splitlines()[-1])), result.stdout


# Each of these was imported by ``ouroboros --version`` while ``cli/main.py``
# imported every command module.
_HEAVY_MODULES = (
    "sqlalchemy",
    "prompt_toolkit",
    "ouroboros.orchestrator",
    "ouroboros.mcp.tools",
    "ouroboros.persistence.event_store",
    "ouroboros.cli.commands.plugin_dispatch",
)


def test_version_imports_no_command_module(tmp_path: Path) -> None:
    modules, stdout = _startup_modules(["--version"], tmp_path)

    assert "version" in stdout
    assert not [m for m in modules if m.startswith("ouroboros.cli.commands.")]
    assert not [m for m in _HEAVY_MODULES if m in modules]
    # The config loader (and with it the ``.env`` files) still loads at startup.
    assert "ouroboros.config.loader" in modules


def test_subcommand_imports_only_its_own_command_module(tmp_path: Path) -> None:
    modules, stdout = _startup_modules(["config", "--help"], tmp_path)

    assert "ouroboros config" in stdout
    assert "ouroboros.cli.commands.config" in modules
    loaded = {m for m in modules if m.startswith("ouroboros.cli.commands.")}
    for other in ("job", "mcp", "setup", "zcode", "run", "plugin_dispatch"):
        assert f"ouroboros.cli.commands.{other}" not in loaded


def test_project_env_file_still_applies_before_the_version_banner(tmp_path: Path) -> None:
    """``.env`` is read on startup, before the console exists, as before: a
    project ``NO_COLOR`` keeps the cyan colour code out of ``--version``."""
    _, coloured = _startup_modules(["--version"], tmp_path)
    (tmp_path / ".env").write_text("NO_COLOR=1\n", encoding="utf-8")
    _, plain = _startup_modules(["--version"], tmp_path)

    assert "\x1b[1;36m" in coloured
    assert "\x1b[1;36m" not in plain
    assert "Ouroboros" in plain
