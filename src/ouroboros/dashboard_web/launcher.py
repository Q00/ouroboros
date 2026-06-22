"""In-process server launcher (tests/dev). The PRODUCTION path is the singleton
:func:`daemon.ensure_dashboard`; run handlers should call that so a run reuses the
one shared daemon rather than spawning a server of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
import socket
import threading

from ouroboros.dashboard_web.reader import default_db_path
from ouroboros.dashboard_web.server import serve_background


def _free_port(host: str = "127.0.0.1") -> int:
    with socket.socket() as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


@dataclass
class DashboardHandle:
    """A running in-process dashboard: its base URL and a way to stop it."""

    url: str
    host: str
    port: int
    _server: object
    _thread: threading.Thread

    def run_url(self, run_id: str) -> str:
        return f"{self.url}/?run={run_id}"

    def stop(self) -> None:
        server = self._server
        shutdown = getattr(server, "shutdown", None)
        close = getattr(server, "server_close", None)
        if callable(shutdown):
            shutdown()
        if callable(close):
            close()


def serve_dashboard(
    *,
    db_path: str | None = None,
    host: str = "127.0.0.1",
    port: int | None = None,
) -> DashboardHandle:
    """Start a background (in-process) multi-run dashboard and return its handle.

    Mainly for tests/dev. Production runs share one daemon via ``ensure_dashboard``.
    """
    resolved_db = db_path or str(default_db_path())
    resolved_port = (
        port if port not in (None, 0) else _free_port(host if host != "0.0.0.0" else "127.0.0.1")
    )
    server, thread = serve_background(db_path=resolved_db, host=host, port=resolved_port)
    display_host = "localhost" if host in ("127.0.0.1", "0.0.0.0", "localhost") else host
    return DashboardHandle(
        url=f"http://{display_host}:{resolved_port}",
        host=host,
        port=resolved_port,
        _server=server,
        _thread=thread,
    )


__all__ = ["DashboardHandle", "serve_dashboard"]
