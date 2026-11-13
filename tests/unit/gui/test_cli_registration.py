"""``gui`` and ``service`` stay registered on the root CLI.

The console's restart (``gui.launcher``) and the login-service units
(``gui.service``) re-run ``python -m ouroboros gui ...``. Losing either
registration raises no import error, so check it through the root app.
"""

from __future__ import annotations

import re

from typer.testing import CliRunner

from ouroboros.cli.main import app

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _help(*args: str) -> str:
    result = CliRunner().invoke(app, [*args, "--help"])
    output = _ANSI.sub("", result.output)
    assert result.exit_code == 0, output
    return output


def test_gui_is_a_root_command() -> None:
    output = _help("gui")
    for option in ("--port", "--host", "--no-browser", "--page"):
        assert option in output


def test_service_is_a_root_command_with_its_subcommands() -> None:
    output = _help("service")
    for subcommand in ("install", "uninstall", "status"):
        assert subcommand in output
