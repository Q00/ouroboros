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


def _capture_console(monkeypatch, *, url: str = "http://localhost:10300/#models", reused=False):
    served: dict[str, object] = {}

    def fake_serve(**kwargs: object) -> None:
        served.update(kwargs)
        on_ready = kwargs.get("on_ready")
        if callable(on_ready):
            on_ready(url, reused)

    monkeypatch.setattr(launcher, "_console_serve", fake_serve)
    return served


def test_launch_web_opens_console_settings_page(monkeypatch, capsys) -> None:
    served = _capture_console(monkeypatch)

    launcher._launch_web()

    assert served["page"] == "models"
    assert served["open_browser"] is True
    assert served["host"] == "127.0.0.1"
    assert served["port"] is None
    assert "Ouroboros Settings" in capsys.readouterr().out


def test_launch_web_treats_zero_port_as_default(monkeypatch) -> None:
    served = _capture_console(monkeypatch)

    launcher._launch_web(port=0, open_browser=False)

    assert served["port"] is None
    assert served["open_browser"] is False


def test_launch_web_reports_reused_console(monkeypatch, capsys) -> None:
    _capture_console(monkeypatch, reused=True)

    launcher._launch_web()

    assert "open in the console" in capsys.readouterr().out


def test_bootstrap_uses_published_mcp_v2_and_tui_profile(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.delenv("OUROBOROS_CONFIG_TUI_BOOTSTRAPPED", raising=False)
    monkeypatch.setattr(
        launcher.shutil, "which", lambda name: "/usr/bin/uvx" if name == "uvx" else None
    )
    monkeypatch.setattr(launcher.sys, "argv", ["ouroboros", "config", "--no-browser"])
    monkeypatch.setattr(
        launcher.os,
        "execvpe",
        lambda executable, args, env: captured.update(
            executable=executable,
            args=args,
            env=env,
        ),
    )

    assert launcher._relaunch_with_tui_profile() is True
    args = captured["args"]
    assert isinstance(args, list)
    assert args[:4] == ["/usr/bin/uvx", "--isolated", "--python", ">=3.12"]
    assert args[4:6] == ["--from", "ouroboros-ai[mcp,tui]"]
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["OUROBOROS_CONFIG_TUI_BOOTSTRAPPED"] == "1"


def test_bootstrap_guard_prevents_recursive_relaunch(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_CONFIG_TUI_BOOTSTRAPPED", "1")
    monkeypatch.setattr(launcher.shutil, "which", lambda _name: "/usr/bin/uvx")

    assert launcher._relaunch_with_tui_profile() is False


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
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 22 10.0.0.2 22")
    served = _capture_console(monkeypatch, url="http://localhost:50124/#models")

    launcher.launch_settings()

    assert served["open_browser"] is False  # never opened on the wrong machine
    import re

    output = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    assert "ssh -L 50124:localhost:50124" in output


def test_force_web_with_host_and_port(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    served = _capture_console(monkeypatch)

    launcher.launch_settings(force_web=True, host="0.0.0.0", port=8765, open_browser=False)

    assert served["host"] == "0.0.0.0"
    assert served["port"] == 8765
    assert served["open_browser"] is False
