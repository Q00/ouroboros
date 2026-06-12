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
    monkeypatch.setattr(launcher, "_launch_web", lambda **_kwargs: calls.append("web"))
    launcher.launch_settings()
    assert calls == ["inline"]


def test_launch_settings_routes_to_web_in_harness(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr(launcher, "_launch_inline", lambda: calls.append("inline"))
    monkeypatch.setattr(launcher, "_launch_web", lambda **_kwargs: calls.append("web"))
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


@pytest.mark.parametrize(
    ("env_vars", "expected_remote"),
    [
        ({"SSH_CONNECTION": "10.0.0.1 22 10.0.0.2 22"}, True),
        ({"SSH_TTY": "/dev/pts/1"}, True),
        ({}, False),
    ],
)
def test_is_remote_session(monkeypatch, env_vars, expected_remote) -> None:
    for name in ("SSH_CONNECTION", "SSH_TTY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env_vars.items():
        monkeypatch.setenv(name, value)
    assert launcher.is_remote_session() is expected_remote


def test_remote_web_mode_prints_url_instead_of_opening_browser(monkeypatch, capsys) -> None:
    """On a remote host (hermes box driven from Discord/SSH), a browser
    opened locally is useless — print a reachable URL + tunnel hint."""
    served: dict[str, object] = {}

    class _FakeServer:
        def __init__(self, command, host="localhost", port=8000, title=None) -> None:
            served.update(host=host, port=port)

        def serve(self) -> None:
            served["serving"] = True

    opened: list[str] = []
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 22 10.0.0.2 22")
    monkeypatch.setattr(launcher, "_import_server", lambda: _FakeServer)
    monkeypatch.setattr(launcher, "_free_port", lambda: 50124)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url))

    launcher.launch_settings()

    assert served["serving"] is True
    assert opened == []  # never opened on the wrong machine
    import re

    output = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    assert "ssh -L 50124:localhost:50124" in output


def test_force_web_with_host_and_port(monkeypatch) -> None:
    served: dict[str, object] = {}

    class _FakeServer:
        def __init__(self, command, host="localhost", port=8000, title=None) -> None:
            served.update(host=host, port=port)

        def serve(self) -> None:
            served["serving"] = True

    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    # force_web bypasses TTY detection entirely — no stdout fake needed
    # (rich's console must keep a real write()able stream).
    monkeypatch.setattr(launcher, "_import_server", lambda: _FakeServer)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda _url: None)

    class _NoopTimer:
        def __init__(self, *_args, **_kwargs) -> None: ...

        def start(self) -> None: ...

    monkeypatch.setattr(launcher.threading, "Timer", _NoopTimer)

    launcher.launch_settings(force_web=True, host="0.0.0.0", port=8765, open_browser=False)

    assert served == {"host": "0.0.0.0", "port": 8765, "serving": True}
