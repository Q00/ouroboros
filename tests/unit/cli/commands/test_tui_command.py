"""Tests for the TUI command."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import typer

from ouroboros.cli.commands.tui import build_tui_open_launch, monitor_command, open_command


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
    error_message = print_error.call_args.args[0]
    assert "ouroboros-ai[tui]" in error_message
    assert "uvx --from 'ouroboros-ai[tui]' ouroboros tui monitor" in error_message


def test_tui_open_ghostty_uses_argv_and_working_directory(tmp_path: Path) -> None:
    db_path = tmp_path / "ouroboros.db"

    with patch("ouroboros.cli.commands.tui.shutil.which", return_value="/usr/local/bin/ouroboros"):
        launch = build_tui_open_launch(
            db_path=db_path,
            cwd=tmp_path,
            env={"TERM_PROGRAM": "ghostty", "DISPLAY": ":0"},
        )

    assert launch.argv is not None
    assert launch.argv[:4] == ["open", "-na", "Ghostty.app", "--args"]
    assert f"--working-directory={tmp_path}" in launch.argv
    assert "-e" in launch.argv
    assert "/usr/local/bin/ouroboros" in launch.argv
    assert "tui" in launch.argv
    assert "monitor" in launch.argv
    assert str(db_path) in launch.argv
    assert launch.manual_command.startswith(f"cd {tmp_path}")


def test_tui_open_iterm_uses_osascript_with_cd(tmp_path: Path) -> None:
    db_path = tmp_path / "ouroboros.db"

    with patch("ouroboros.cli.commands.tui.shutil.which", return_value="/usr/local/bin/ouroboros"):
        launch = build_tui_open_launch(
            db_path=db_path,
            cwd=tmp_path,
            env={"TERM_PROGRAM": "iTerm.app", "DISPLAY": ":0"},
        )

    assert launch.argv is not None
    assert launch.argv[:2] == ["osascript", "-e"]
    assert 'tell application "iTerm"' in launch.argv[2]
    assert f"cd {tmp_path}" in launch.argv[2]
    assert "ouroboros tui monitor" in launch.argv[2]


def test_tui_open_headless_prints_manual_command(tmp_path: Path) -> None:
    db_path = tmp_path / "ouroboros.db"

    with patch("ouroboros.cli.commands.tui.shutil.which", return_value=None):
        launch = build_tui_open_launch(
            db_path=db_path,
            cwd=tmp_path,
            env={"TERM_PROGRAM": "ghostty", "SSH_TTY": "/dev/ttys001"},
        )

    assert launch.argv is None
    assert "SSH/headless" in launch.message
    assert "uvx --from 'ouroboros-ai[tui]' ouroboros tui monitor" in launch.manual_command
    assert str(db_path) in launch.manual_command


def test_tui_open_invokes_subprocess_when_supported(tmp_path: Path) -> None:
    db_path = tmp_path / "ouroboros.db"

    with (
        patch("ouroboros.cli.commands.tui.shutil.which", return_value="/usr/local/bin/ouroboros"),
        patch("ouroboros.cli.commands.tui.subprocess.Popen") as popen,
        patch("ouroboros.cli.commands.tui.print_success") as print_success,
        patch("ouroboros.cli.commands.tui.print_info"),
        patch.dict(
            "ouroboros.cli.commands.tui.os.environ",
            {"TERM_PROGRAM": "ghostty", "DISPLAY": ":0"},
            clear=True,
        ),
    ):
        open_command(db_path=db_path, cwd=tmp_path)

    popen.assert_called_once()
    assert popen.call_args.kwargs["cwd"] == tmp_path.resolve()
    print_success.assert_called_once()


def test_tui_open_unknown_terminal_exits_zero_with_manual_command(tmp_path: Path) -> None:
    db_path = tmp_path / "ouroboros.db"

    with (
        patch("ouroboros.cli.commands.tui.subprocess.Popen") as popen,
        patch("ouroboros.cli.commands.tui.print_info") as print_info,
        patch.dict(
            "ouroboros.cli.commands.tui.os.environ",
            {"TERM_PROGRAM": "unknown", "DISPLAY": ":0"},
            clear=True,
        ),
    ):
        open_command(db_path=db_path, cwd=tmp_path)

    popen.assert_not_called()
    output = "\n".join(call.args[0] for call in print_info.call_args_list)
    assert "Unsupported terminal" in output
    assert "Run this in another terminal" in output
