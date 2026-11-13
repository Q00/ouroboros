"""One shared Ouroboros MCP server, supervised by the console.

Each MCP host normally launches its own stdio ``ouroboros mcp serve`` — one
process pair per Claude/Codex session, more with every sub-agent. The console
can instead run a single ``streamable-http`` server on loopback and point hosts
at its URL. The console owns that child: it restarts it when it exits, stops it
with its whole process group, and records it in ``~/.ouroboros/gui/daemon.json``
so a restarted console adopts the setting.
"""

from __future__ import annotations

from collections import deque
import contextlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any

DEFAULT_MCP_PORT = 8765
_MAX_BACKOFF_SEC = 60.0
_LOG_LINES = 300


def _gui_dir() -> Path:
    from ouroboros.config.models import get_config_dir

    path = get_config_dir() / "gui"
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_path() -> Path:
    return _gui_dir() / "daemon.json"


def load_settings() -> dict[str, Any]:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    port = data.get("mcp_port")
    return {
        "shared_mcp": bool(data.get("shared_mcp", False)),
        "mcp_port": port if isinstance(port, int) and 1024 <= port <= 65535 else DEFAULT_MCP_PORT,
    }


def save_settings(**changes: Any) -> dict[str, Any]:
    current = load_settings()
    current.update({k: v for k, v in changes.items() if k in current})
    tmp = settings_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(current, indent=1), encoding="utf-8")
    tmp.replace(settings_path())
    return current


def mcp_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/mcp"


def _mcp_runtime_args() -> list[str]:
    """The MCP 2 profile cannot hold the Claude SDK, so Claude work uses the CLI worker."""
    try:
        from ouroboros.config.loader import get_agent_runtime_backend

        runtime = get_agent_runtime_backend().strip().lower()
    except Exception:
        return []
    if runtime in ("claude", "claude_code", "claude-sdk"):
        return ["--runtime", "claude-cli"]
    return []


def server_command(port: int) -> list[str] | None:
    """How to start the shared server, or ``None`` when no MCP 2 launcher exists."""
    serve_args = [
        "mcp",
        "serve",
        "--transport",
        "streamable-http",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--idle-timeout",
        "0",
        *_mcp_runtime_args(),
    ]
    from ouroboros.gui.components import _mcp_major

    if _mcp_major() == 2:
        return [sys.executable, "-m", "ouroboros", *serve_args]
    from ouroboros import __version__

    if shutil.which("uvx") and "dev" not in __version__ and "+" not in __version__:
        spec = f"ouroboros-ai[mcp]=={__version__}"
        return ["uvx", "--isolated", "--python", ">=3.12", "--from", spec, "ouroboros", *serve_args]
    return None


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) == 0


class SharedMcp:
    """Supervisor for the shared streamable-http MCP server child."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._log: deque[str] = deque(maxlen=_LOG_LINES)
        self.port = load_settings()["mcp_port"]
        self.started_at: float | None = None
        self.restarts = 0
        self.last_exit: int | None = None
        self.error: str | None = None

    # lifecycle -----------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self.port = load_settings()["mcp_port"]
            self._stopping.clear()
            self.error = None
            self._thread = threading.Thread(target=self._supervise, name="ouro-mcp", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 8.0) -> None:
        self._stopping.set()
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            _signal_group(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _signal_group(proc, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=2)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)

    def _supervise(self) -> None:
        backoff = 1.0
        while not self._stopping.is_set():
            if port_open(self.port):
                self.error = f"Port {self.port} is already in use by another process."
                if self._stopping.wait(5):
                    return
                continue
            command = server_command(self.port)
            if command is None:
                self.error = (
                    "No MCP 2 launcher: install uv (for uvx) or the [mcp] component, "
                    "then start the shared server again."
                )
                return
            log_path = _gui_dir() / "mcp-shared.log"
            try:
                with log_path.open("a", encoding="utf-8") as log_file:
                    proc = subprocess.Popen(  # noqa: S603 - fixed argv
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        start_new_session=True,
                        env={**os.environ, "NO_COLOR": "1", "PYTHONUNBUFFERED": "1"},
                    )
                    with self._lock:
                        self._proc = proc
                    self.started_at = time.time()
                    self._log.append(f"$ {' '.join(command)}")
                    assert proc.stdout is not None
                    for line in proc.stdout:
                        line = line.rstrip("\n")
                        self._log.append(line)
                        log_file.write(line + "\n")
                    self.last_exit = proc.wait()
            except OSError as exc:
                self.error = f"Could not start the MCP server: {exc}"
                self.last_exit = -1
            with self._lock:
                self._proc = None
            self.started_at = None
            if self._stopping.is_set():
                return
            self.restarts += 1
            if self._stopping.wait(backoff):
                return
            backoff = min(backoff * 2, _MAX_BACKOFF_SEC)

    # state ---------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            proc = self._proc
        running = proc is not None and proc.poll() is None
        return {
            "enabled": load_settings()["shared_mcp"],
            "running": running,
            "listening": running and port_open(self.port),
            "pid": proc.pid if running and proc is not None else None,
            "port": self.port,
            "url": mcp_url(self.port),
            "command": " ".join(server_command(self.port) or []),
            "started_at": self.started_at,
            "restarts": self.restarts,
            "last_exit": self.last_exit,
            "error": self.error,
            "log": list(self._log)[-40:],
        }


def _signal_group(proc: subprocess.Popen[str], sig: int) -> None:
    """Signal the child's whole process group (uvx wraps a Python grandchild)."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.send_signal(sig)


def count_stdio_servers() -> int:
    """How many per-session ``ouroboros mcp serve`` processes are alive (POSIX only)."""
    if os.name == "nt":
        return 0
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv
            ["ps", "-eo", "command"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return 0
    return sum(
        1
        for line in out.splitlines()
        if "ouroboros mcp serve" in line
        and "streamable-http" not in line
        and not line.lstrip().startswith(("uv ", "uvx ", "/bin/sh"))
        and "uv tool uvx" not in line
    )


__all__ = [
    "DEFAULT_MCP_PORT",
    "SharedMcp",
    "count_stdio_servers",
    "load_settings",
    "mcp_url",
    "save_settings",
    "server_command",
]
