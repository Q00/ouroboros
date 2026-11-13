"""Login-service units and the shared MCP server command."""

from __future__ import annotations

import plistlib
import subprocess

import pytest

from ouroboros.gui import daemon, service


def test_launchd_plist_restarts_only_after_a_crash() -> None:
    plist = plistlib.loads(service.render_launchd(10300))
    assert plist["Label"] == service.LABEL
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["ProgramArguments"][-4:] == ["--no-browser", "--service", "--port", "10300"]
    assert plist["EnvironmentVariables"]["OUROBOROS_GUI_SERVICE"] == "1"
    assert "PATH" in plist["EnvironmentVariables"]


def test_systemd_unit_restarts_on_failure() -> None:
    unit = service.render_systemd(10300)
    assert "Restart=on-failure" in unit
    assert "--service" in unit
    assert "WantedBy=default.target" in unit


def test_shared_server_command_uses_streamable_http_without_idle_exit(monkeypatch) -> None:
    monkeypatch.setattr("ouroboros.gui.components._mcp_major", lambda: 2)
    command = daemon.server_command(8765)
    assert command is not None
    assert command[command.index("--transport") + 1] == "streamable-http"
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--idle-timeout") + 1] == "0"


def test_settings_round_trip(isolated_home) -> None:
    assert daemon.load_settings() == {"shared_mcp": False, "mcp_port": 8765}
    daemon.save_settings(shared_mcp=True, mcp_port=9001, ignored="x")
    assert daemon.load_settings() == {"shared_mcp": True, "mcp_port": 9001}


def test_mcp_url_is_loopback() -> None:
    assert daemon.mcp_url(8765) == "http://127.0.0.1:8765/mcp"


def _raise(exc: BaseException):
    def run(*_args, **_kwargs):
        raise exc

    return run


@pytest.mark.parametrize(
    "exc",
    [
        subprocess.TimeoutExpired(cmd=["launchctl"], timeout=20),
        FileNotFoundError(2, "No such file or directory"),
    ],
    ids=["timeout", "missing-tool"],
)
def test_service_manager_failures_become_service_errors(monkeypatch, tmp_path, exc) -> None:
    """A hung or missing launchctl/systemctl is a ServiceError, not a crash."""
    from ouroboros.gui import service

    unit = tmp_path / "unit"
    monkeypatch.setattr(service, "_manager", lambda: "launchd")
    monkeypatch.setattr(service, "unit_path", lambda: unit)
    monkeypatch.setattr(service.subprocess, "run", _raise(exc))

    with pytest.raises(service.ServiceError):
        service.install(10300)
    with pytest.raises(service.ServiceError):
        service.uninstall()

    unit.write_text("installed", encoding="utf-8")
    state = service.status()
    assert state["installed"] is True
    assert state["loaded"] is False
    assert state["pid"] is None
    assert state["probe_error"]


def test_status_reports_no_probe_error_when_the_probe_runs(monkeypatch, tmp_path) -> None:
    from ouroboros.gui import service

    unit = tmp_path / "unit"
    unit.write_text("installed", encoding="utf-8")
    monkeypatch.setattr(service, "_manager", lambda: "systemd")
    monkeypatch.setattr(service, "unit_path", lambda: unit)
    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda *a, **_k: subprocess.CompletedProcess(a[0], 0, stdout="active\n", stderr=""),
    )
    state = service.status()
    assert state["loaded"] is True
    assert state["probe_error"] is None
