"""Dispatch-matrix tests for the bare `ouroboros config` launcher (#1414)."""

from __future__ import annotations

import pytest

from ouroboros.config_tui import launcher


class _FakeStdout:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.mark.parametrize(
    ("claudecode", "tty", "expected_harness"),
    [
        ("1", True, True),  # harness env wins even on a TTY
        ("1", False, True),
        ("", True, False),  # interactive terminal
        ("", False, True),  # piped/captured stdout
    ],
)
def test_is_harness_context_matrix(monkeypatch, claudecode, tty, expected_harness) -> None:
    if claudecode:
        monkeypatch.setenv("CLAUDECODE", claudecode)
    else:
        monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr(launcher.sys, "stdout", _FakeStdout(tty))
    assert launcher.is_harness_context() is expected_harness


def test_launch_settings_routes_to_inline_on_tty(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr(launcher.sys, "stdout", _FakeStdout(True))
    monkeypatch.setattr(launcher, "_launch_inline", lambda: calls.append("inline"))
    monkeypatch.setattr(launcher, "_launch_web", lambda: calls.append("web"))
    launcher.launch_settings()
    assert calls == ["inline"]


def test_launch_settings_routes_to_web_in_harness(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr(launcher, "_launch_inline", lambda: calls.append("inline"))
    monkeypatch.setattr(launcher, "_launch_web", lambda: calls.append("web"))
    launcher.launch_settings()
    assert calls == ["web"]


def test_launch_web_serves_and_opens_browser(monkeypatch) -> None:
    served: dict[str, object] = {}

    class _FakeServer:
        def __init__(self, command, host="localhost", port=8000, title=None) -> None:
            served.update(command=command, host=host, port=port, title=title)

        def serve(self) -> None:
            served["serving"] = True

    opened: list[str] = []

    class _ImmediateTimer:
        def __init__(self, interval, function, args=()) -> None:
            self._function = function
            self._args = args

        def start(self) -> None:
            self._function(*self._args)

    monkeypatch.setattr(launcher, "_import_server", lambda: _FakeServer)
    monkeypatch.setattr(launcher, "_free_port", lambda: 50123)
    monkeypatch.setattr(launcher.threading, "Timer", _ImmediateTimer)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url))

    launcher._launch_web()

    assert served["serving"] is True
    assert served["port"] == 50123
    assert "ouroboros.config_tui" in str(served["command"])
    assert opened == ["http://localhost:50123"]


def test_launch_web_without_textual_serve_prints_hint(monkeypatch, capsys) -> None:
    monkeypatch.setattr(launcher, "_import_server", lambda: None)
    with pytest.raises(SystemExit):
        launcher._launch_web()
    # Rich panels add ANSI styling and box borders; strip both before matching.
    import re

    output = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    flattened = "".join(line.strip("│╭╮╰╯─ ") for line in output.splitlines())
    assert "ouroboros-ai[tui]" in flattened
