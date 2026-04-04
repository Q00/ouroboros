"""Tests for the TUI command."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import typer

from ouroboros.cli.commands.tui import monitor_command


def test_monitor_command_reports_optional_tui_dependency() -> None:
    real_import = __import__

    def fake_import(name: str, *args, **kwargs):
        if name == "ouroboros.tui":
            raise ImportError("missing textual")
        return real_import(name, *args, **kwargs)

    with (
        patch("ouroboros.cli.commands.tui.print_error") as print_error,
        patch("builtins.__import__", side_effect=fake_import),
    ):
        with pytest.raises(typer.Exit):
            monitor_command()

    print_error.assert_called_once()
    assert "ouroboros-ai[tui]" in print_error.call_args.args[0]
