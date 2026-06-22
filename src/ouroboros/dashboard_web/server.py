"""Dependency-free HTTP + SSE server that streams runs' Kanbans to the browser.

ONE server (a singleton daemon) serves EVERY run — a run is selected per request
via ``?run=<execution_id>``, never baked into the server. This is deliberate: the
dashboard must NOT spawn a web server per MCP session/run (that would worsen the
process sprawl). The daemon is db-scoped only; :mod:`daemon` handles its single
lifecycle.

Each ``GET /events?run=<id>`` connection is one cursor tail of that run. Built on
stdlib ``http.server`` (threaded), read-only against the EventStore.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from ouroboros.dashboard_web.kanban import reduce_board
from ouroboros.dashboard_web.page import INDEX_HTML, static_html
from ouroboros.dashboard_web.reader import EventTail, list_recent_executions

# SSE poll cadence. Fast enough to feel live, slow enough that tailing a shared
# multi-hundred-MB SQLite file stays negligible.
_POLL_INTERVAL_SEC = 0.7
# Heartbeat comment cadence so idle connections stay open through proxies/tunnels.
_HEARTBEAT_SEC = 15.0


class _DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], db_path: str) -> None:
        super().__init__(addr, _Handler)
        self.db_path = db_path
        # Liveness signal for the daemon's idle-shutdown watchdog: monotonic time
        # of the last SSE client activity, and the current open-stream count.
        self.last_activity: float = time.monotonic()
        self.open_streams: int = 0
        self._lock = threading.Lock()

    def touch(self) -> None:
        with self._lock:
            self.last_activity = time.monotonic()

    def stream_opened(self) -> None:
        with self._lock:
            self.open_streams += 1
            self.last_activity = time.monotonic()

    def stream_closed(self) -> None:
        with self._lock:
            self.open_streams = max(0, self.open_streams - 1)
            self.last_activity = time.monotonic()

    def idle_seconds(self) -> float:
        with self._lock:
            if self.open_streams > 0:
                return 0.0
            return time.monotonic() - self.last_activity


class _Handler(BaseHTTPRequestHandler):
    server: _DashboardServer  # type: ignore[assignment]

    def log_message(self, *_args: Any) -> None:  # noqa: D401 (silence stderr spam)
        return

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            self._send_bytes(INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/healthz":
            self._send_bytes(b"ok", "text/plain")
        elif path == "/api/runs":
            self._send_json({"runs": list_recent_executions(self.server.db_path)})
        elif path == "/snapshot":
            self._send_snapshot((query.get("run") or [""])[0])
        elif path == "/events":
            run = (query.get("run") or [""])[0]
            self._stream_events(run)
        else:
            self.send_error(404)

    def _send_snapshot(self, run_id: str) -> None:
        """Serve a frozen, SSE-free HTML snapshot of one run (shareable / capturable)."""
        if not run_id:
            self.send_error(400, "missing ?run=<execution_id>")
            return
        tail = EventTail(self.server.db_path, run_id)
        events = tail.fetch_new(limit=100000)
        board = reduce_board(events, execution_id=run_id)
        self._send_bytes(
            static_html(board, run_id=run_id).encode("utf-8"), "text/html; charset=utf-8"
        )

    def _send_bytes(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj: Any) -> None:
        self._send_bytes(json.dumps(obj, default=str).encode("utf-8"), "application/json")

    def _stream_events(self, run_id: str) -> None:
        if not run_id:
            self.send_error(400, "missing ?run=<execution_id>")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        self.server.stream_opened()
        tail = EventTail(self.server.db_path, run_id)
        accumulated: list[dict[str, Any]] = []
        last_emit = 0.0
        last_payload: str | None = None
        try:
            while True:
                new = tail.fetch_new()
                now = time.monotonic()
                if new:
                    accumulated.extend(new)
                    board = reduce_board(accumulated, execution_id=run_id)
                    payload = json.dumps(board, default=str)
                    if payload != last_payload:
                        self._sse_send(payload)
                        last_payload = payload
                        last_emit = now
                        self.server.touch()
                elif now - last_emit >= _HEARTBEAT_SEC:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    last_emit = now
                    self.server.touch()
                time.sleep(_POLL_INTERVAL_SEC)
        except (BrokenPipeError, ConnectionResetError):
            return  # client navigated away
        except OSError:
            return
        finally:
            self.server.stream_closed()

    def _sse_send(self, data: str) -> None:
        # One SSE event; data may not contain bare newlines (json.dumps escapes them).
        self.wfile.write(f"data: {data}\n\n".encode())
        self.wfile.flush()


def make_server(*, db_path: str, host: str, port: int) -> _DashboardServer:
    """Build (but do not start) the dashboard HTTP server (db-scoped, multi-run)."""
    return _DashboardServer((host, port), db_path)


def serve_blocking(
    *,
    db_path: str,
    host: str,
    port: int,
    idle_shutdown_sec: float | None = None,
) -> None:
    """Run the server in the current thread until interrupted or idle.

    When ``idle_shutdown_sec`` is set, a watchdog stops the server once no SSE
    client has been connected for that long — so the daemon never lingers as a
    zombie after the runs it was watching finish.
    """
    server = make_server(db_path=db_path, host=host, port=port)
    stop = threading.Event()

    def _watchdog() -> None:
        assert idle_shutdown_sec is not None
        while not stop.wait(min(idle_shutdown_sec, 30.0)):
            if server.idle_seconds() >= idle_shutdown_sec:
                threading.Thread(target=server.shutdown, daemon=True).start()
                return

    if idle_shutdown_sec is not None:
        threading.Thread(target=_watchdog, name="ooo-dash-idle", daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


def serve_background(
    *, db_path: str, host: str, port: int
) -> tuple[_DashboardServer, threading.Thread]:
    """Start the server on a daemon thread; return (server, thread). Testing/dev."""
    server = make_server(db_path=db_path, host=host, port=port)
    thread = threading.Thread(target=server.serve_forever, name="ooo-dashboard", daemon=True)
    thread.start()
    return server, thread


__all__ = ["make_server", "serve_background", "serve_blocking"]
