"""HTTP server for the Ouroboros console (``ouroboros gui``).

Stdlib only, like the run dashboard: a threaded ``http.server`` that serves the
built page from ``gui/static`` and a small JSON API. Every ``/api`` call needs
the per-launch token the page receives in its HTML, and every request must
name a loopback host, so another web page in the same browser cannot drive
the console (no CSRF, no DNS rebinding).
"""

from __future__ import annotations

from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from ouroboros.gui import api

STATIC_DIR = Path(__file__).parent / "static"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
_MAX_BODY = 1024 * 1024
_TOKEN_HEADER = "X-Ouroboros-Token"

Route = tuple[str, re.Pattern[str], Callable[..., Any]]


class ApiError(Exception):
    """An error the page should show as-is."""

    def __init__(self, status: int, message: str, code: str = "error") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


class GuiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], *, token: str, public: bool) -> None:
        super().__init__(addr, _Handler)
        self.token = token
        # Bound beyond loopback: the page itself also needs the token.
        self.public = public
        self.started = time.monotonic()
        self.restart_requested = False
        self.state = api.ConsoleState(stop=self._stop_soon)

    def _stop_soon(self, *, restart: bool = False) -> None:
        self.restart_requested = restart
        threading.Timer(0.3, self.shutdown).start()

    def allowed_host(self, host_header: str | None) -> bool:
        if self.public:
            return True
        if not host_header:
            return False
        host = host_header.strip().lower()
        if host.startswith("["):
            host = host.split("]", 1)[0] + "]"
        else:
            host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return host in _LOOPBACK_HOSTS


class _Handler(BaseHTTPRequestHandler):
    server: GuiServer  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:
        return

    # ── dispatch ─────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        if not self.server.allowed_host(self.headers.get("Host")):
            self._send_json({"error": "Host not allowed", "code": "bad_host"}, status=403)
            return
        parsed = urlparse(self.path)
        path = parsed.path
        query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        if path == "/healthz":
            self._send_json(self.server.state.health(self.server.started, self.server.server_port))
            return
        if path.startswith("/api/"):
            self._api(method, path, query)
            return
        if method != "GET":
            self._send_json({"error": "Not found"}, status=404)
            return
        self._static(path, query)

    def _authorized(self, query: dict[str, str]) -> bool:
        supplied = self.headers.get(_TOKEN_HEADER) or query.get("token") or ""
        if not secrets.compare_digest(supplied, self.server.token):
            return False
        origin = self.headers.get("Origin")
        if origin and not self.server.public:
            host = urlparse(origin).hostname or ""
            if host not in _LOOPBACK_HOSTS:
                return False
        return True

    def _api(self, method: str, path: str, query: dict[str, str]) -> None:
        if not self._authorized(query):
            self._send_json(
                {"error": "Missing or wrong console token", "code": "unauthorized"}, 401
            )
            return
        body: Any = None
        if method in ("POST", "PUT"):
            try:
                body = self._read_json()
            except ApiError as exc:
                self._send_json({"error": exc.message, "code": exc.code}, exc.status)
                return
        for route_method, pattern, handler in api.ROUTES:
            if route_method != method:
                continue
            match = pattern.fullmatch(path)
            if not match:
                continue
            try:
                result = handler(self.server.state, *match.groups(), query=query, body=body)
            except api.ApiError as exc:
                self._send_json({"error": exc.message, "code": exc.code, **exc.extra}, exc.status)
                return
            except Exception as exc:  # surface, never kill the server thread
                self._send_json({"error": str(exc) or type(exc).__name__, "code": "internal"}, 500)
                return
            if isinstance(result, api.EventStream):
                self._stream(result)
                return
            self._send_json(result)
            return
        self._send_json({"error": "Not found", "code": "not_found"}, status=404)

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_BODY:
            raise ApiError(413, "Request too large", "too_large")
        if length == 0:
            return {}
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise ApiError(415, "Send JSON", "bad_content_type")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "Malformed JSON", "bad_json") from None

    # ── static page ──────────────────────────────────────────────────

    def _static(self, path: str, query: dict[str, str]) -> None:
        if path in ("/", "/index.html"):
            if self.server.public and not secrets.compare_digest(
                query.get("token", ""), self.server.token
            ):
                self._send_bytes(
                    b"Open the URL printed by `ouroboros gui` (it carries the access token).",
                    "text/plain; charset=utf-8",
                    status=401,
                )
                return
            self._send_index()
            return
        rel = path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve()) + os.sep) or not target.is_file():
            # Unknown paths fall back to the app: routing is hash-based.
            self._send_index()
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        cache = "public, max-age=31536000, immutable" if rel.startswith("assets/") else "no-cache"
        self._send_bytes(target.read_bytes(), ctype, headers={"Cache-Control": cache})

    def _send_index(self) -> None:
        index = STATIC_DIR / "index.html"
        if not index.is_file():
            self._send_bytes(
                b"The console page is not built. Run `bun run build` in gui/.",
                "text/plain; charset=utf-8",
                status=503,
            )
            return
        html = index.read_text(encoding="utf-8")
        meta = f'<meta name="ouroboros-token" content="{self.server.token}" />'
        html = html.replace("<head>", "<head>\n    " + meta, 1)
        self._send_bytes(
            html.encode("utf-8"),
            "text/html; charset=utf-8",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                    "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
                    "frame-ancestors 'none'; base-uri 'none'"
                ),
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )

    # ── responses ────────────────────────────────────────────────────

    def _send_bytes(
        self,
        body: bytes,
        content_type: str,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, obj: Any, status: int = 200) -> None:
        self._send_bytes(
            json.dumps(obj, default=str).encode("utf-8"),
            "application/json",
            status=status,
            headers={"Cache-Control": "no-store"},
        )

    def _stream(self, stream: api.EventStream) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            for event in stream.events():
                if event is None:
                    self.wfile.write(b": keep-alive\n\n")
                else:
                    self.wfile.write(f"data: {json.dumps(event, default=str)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            stream.close()


def make_server(*, host: str = "127.0.0.1", port: int = 0, token: str | None = None) -> GuiServer:
    """Build (not start) a console server. ``port=0`` picks a free port."""
    public = host not in ("127.0.0.1", "localhost", "::1")
    return GuiServer((host, port), token=token or secrets.token_urlsafe(24), public=public)


def serve_background(**kwargs: Any) -> tuple[GuiServer, threading.Thread]:
    """Start a server on a daemon thread (tests and embedding)."""
    server = make_server(**kwargs)
    thread = threading.Thread(target=server.serve_forever, name="ouroboros-gui", daemon=True)
    thread.start()
    return server, thread


__all__ = ["GuiServer", "make_server", "serve_background"]
