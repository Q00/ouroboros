"""The line ``ouroboros gui`` prints once the console is serving.

Ctrl+C only reaches a console whose output someone is watching in a terminal.
Started in the background (a login service, an installer), the line must say
how to stop it instead.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from typer.testing import CliRunner

from ouroboros.cli.commands import gui as gui_command
from ouroboros.cli.main import app
from ouroboros.gui import launcher

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_URL = "http://localhost:10399/"


def _startup_output(monkeypatch: pytest.MonkeyPatch, *, reused: bool = False) -> str:
    calls: list[dict[str, Any]] = []

    def fake_serve(**kwargs: Any) -> None:
        calls.append(kwargs)
        kwargs["on_ready"](_URL, reused)

    monkeypatch.setattr(launcher, "serve", fake_serve)
    result = CliRunner().invoke(app, ["gui", "--no-browser"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    # Drop colour codes and panel borders, and undo the panel's line wrapping.
    text = _ANSI.sub("", result.output)
    text = re.sub(r"[│╭╮╰╯─]", " ", text)
    return " ".join(text.split())


def test_background_console_does_not_offer_ctrl_c(monkeypatch: pytest.MonkeyPatch) -> None:
    # CliRunner captures stdout, so it is not a terminal: the background case.
    output = _startup_output(monkeypatch)
    assert _URL in output
    assert "Ctrl+C" not in output
    assert "running in the background; stop it from the console's sidebar." in output


def test_terminal_console_offers_ctrl_c(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gui_command, "_stdout_is_terminal", lambda: True)
    output = _startup_output(monkeypatch)
    assert "press Ctrl+C to stop." in output
    assert "running in the background" not in output


def test_reused_console_line_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    output = _startup_output(monkeypatch, reused=True)
    assert f"Ouroboros console is already running at {_URL}" in output
    assert "Ctrl+C" not in output


class _ClosedStream:
    def isatty(self) -> bool:
        raise ValueError("I/O operation on closed file")


@pytest.mark.parametrize("stream", [None, _ClosedStream()])
def test_missing_or_closed_stdout_counts_as_background(
    monkeypatch: pytest.MonkeyPatch, stream: object
) -> None:
    monkeypatch.setattr(gui_command.sys, "stdout", stream)
    assert gui_command._stdout_is_terminal() is False


def test_terminal_stdout_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(gui_command.sys, "stdout", _Tty())
    assert gui_command._stdout_is_terminal() is True
