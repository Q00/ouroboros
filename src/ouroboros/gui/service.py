"""Run the console (and its shared MCP server) at login: ``ouroboros service``.

macOS registers a launchd agent, Linux a systemd user unit. The service runs
``ouroboros gui --no-browser --service`` with the PATH captured at install time,
so agent CLIs in ``~/.local/bin`` or Homebrew stay reachable. It restarts after
a crash but not after "Stop console", and it waits for a foreground console on
the same port to exit instead of failing.
"""

from __future__ import annotations

import os
from pathlib import Path
import plistlib
import subprocess
import sys
from typing import Any

LABEL = "ai.ouroboros.console"
SYSTEMD_UNIT = "ouroboros-console.service"


class ServiceError(Exception):
    """The service manager refused or is unavailable."""


def _manager() -> str | None:
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    return None


def _log_path() -> Path:
    from ouroboros.config.models import get_config_dir

    path = get_config_dir() / "gui" / "service.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def unit_path() -> Path | None:
    manager = _manager()
    if manager == "launchd":
        return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    if manager == "systemd":
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        return base / "systemd" / "user" / SYSTEMD_UNIT
    return None


def program_arguments(port: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "ouroboros",
        "gui",
        "--no-browser",
        "--service",
        "--port",
        str(port),
    ]


_RUN_TIMEOUT_SECONDS = 20


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a service-manager command; a timeout or a missing tool is a ``ServiceError``."""
    try:
        return subprocess.run(  # noqa: S603
            args, capture_output=True, text=True, timeout=_RUN_TIMEOUT_SECONDS, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise ServiceError(
            f"{' '.join(args[:2])} did not finish within {_RUN_TIMEOUT_SECONDS} seconds."
        ) from exc
    except OSError as exc:
        raise ServiceError(f"Could not run {args[0]}: {exc.strerror or exc}") from exc


def _launchd_target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def render_launchd(port: int) -> bytes:
    log = str(_log_path())
    plist = {
        "Label": LABEL,
        "ProgramArguments": program_arguments(port),
        "RunAtLoad": True,
        # Restart after a crash, not after a deliberate stop (exit 0).
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "OUROBOROS_GUI_SERVICE": "1",
        },
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "ProcessType": "Background",
    }
    return plistlib.dumps(plist)


def render_systemd(port: int) -> str:
    args = " ".join(_quote(a) for a in program_arguments(port))
    path = os.environ.get("PATH", "/usr/bin:/bin")
    return (
        "[Unit]\n"
        "Description=Ouroboros console and shared MCP server\n"
        "After=network.target\n\n"
        "[Service]\n"
        f"ExecStart={args}\n"
        f"Environment=PATH={path}\n"
        "Environment=OUROBOROS_GUI_SERVICE=1\n"
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _quote(arg: str) -> str:
    return f'"{arg}"' if any(c.isspace() for c in arg) else arg


def install(port: int) -> dict[str, Any]:
    manager = _manager()
    path = unit_path()
    if manager is None or path is None:
        raise ServiceError("Running at login is supported on macOS (launchd) and Linux (systemd).")
    path.parent.mkdir(parents=True, exist_ok=True)
    if manager == "launchd":
        path.write_bytes(render_launchd(port))
        _run(["launchctl", "bootout", _launchd_target()])
        result = _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)])
        if result.returncode != 0:
            raise ServiceError(
                f"launchctl bootstrap failed: {result.stderr.strip() or result.stdout.strip()}"
            )
    else:
        path.write_text(render_systemd(port), encoding="utf-8")
        for args in (["daemon-reload"], ["enable", "--now", SYSTEMD_UNIT]):
            result = _run(["systemctl", "--user", *args])
            if result.returncode != 0:
                raise ServiceError(
                    f"systemctl --user {' '.join(args)} failed: {result.stderr.strip()}"
                )
    return status()


def uninstall() -> dict[str, Any]:
    manager = _manager()
    path = unit_path()
    if manager is None or path is None:
        raise ServiceError("No service manager on this platform.")
    if manager == "launchd":
        _run(["launchctl", "bootout", _launchd_target()])
    else:
        _run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT])
    if path.exists():
        path.unlink()
    if manager == "systemd":
        _run(["systemctl", "--user", "daemon-reload"])
    return status()


def status() -> dict[str, Any]:
    manager = _manager()
    path = unit_path()
    installed = bool(path and path.exists())
    loaded = False
    pid: int | None = None
    probe_error: str | None = None
    try:
        if manager == "launchd" and installed:
            result = _run(["launchctl", "print", _launchd_target()])
            loaded = result.returncode == 0
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.startswith("pid = "):
                    try:
                        pid = int(line.split("=", 1)[1])
                    except ValueError:
                        pid = None
        elif manager == "systemd" and installed:
            result = _run(["systemctl", "--user", "is-active", SYSTEMD_UNIT])
            loaded = result.stdout.strip() == "active"
    except ServiceError as exc:
        # Status is read on every console snapshot: report the failed probe
        # instead of failing the whole snapshot.
        loaded, pid, probe_error = False, None, str(exc)
    return {
        "supported": manager is not None,
        "manager": manager,
        "installed": installed,
        "loaded": loaded,
        "pid": pid,
        "unit_path": str(path) if path else None,
        "log_path": str(_log_path()),
        "this_process_is_service": os.environ.get("OUROBOROS_GUI_SERVICE") == "1",
        "probe_error": probe_error,
    }


__all__ = [
    "LABEL",
    "ServiceError",
    "install",
    "render_launchd",
    "render_systemd",
    "status",
    "uninstall",
]
