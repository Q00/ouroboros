"""Start (or reopen) the console: ``ouroboros gui``.

One console per machine user: the running instance records its port and
token in ``~/.ouroboros/gui/server.json`` (mode 600). A second
``ouroboros gui`` finds it alive and only opens the browser.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import stat
import threading
import urllib.error
import urllib.request
import webbrowser

DEFAULT_PORT = 10300


def _state_path() -> Path:
    from ouroboros.config.models import get_config_dir

    return get_config_dir() / "gui" / "server.json"


def read_state() -> dict | None:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_state(*, host: str, port: int, token: str) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"host": host, "port": port, "token": token, "pid": os.getpid()}),
        encoding="utf-8",
    )
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(path)


def _clear_state() -> None:
    state = read_state()
    if state and state.get("pid") == os.getpid():
        with contextlib.suppress(OSError):
            _state_path().unlink()


def alive(port: int, host: str = "127.0.0.1") -> bool:
    probe = "127.0.0.1" if host in ("0.0.0.0", "localhost") else host
    try:
        with urllib.request.urlopen(f"http://{probe}:{port}/healthz", timeout=0.5) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return False
    return isinstance(data, dict) and data.get("service") == "ouroboros-gui"


def url_for(host: str, port: int, *, token: str | None = None, page: str = "") -> str:
    shown = "localhost" if host in ("127.0.0.1", "localhost", "0.0.0.0") else host
    query = f"?token={token}" if token else ""
    fragment = f"#{page}" if page else ""
    return f"http://{shown}:{port}/{query}{fragment}"


def serve(
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    open_browser: bool = True,
    page: str = "",
    on_ready: object = None,
    service: bool = False,
) -> None:
    """Run the console in this process until stopped (Ctrl+C or the sidebar lever).

    ``service=True`` is the login-service mode: instead of handing off to a
    console that is already running, wait for it to exit and take over.
    """
    import time

    from ouroboros.gui.server import make_server

    state = read_state()
    while service and state and alive(int(state.get("port", 0)), str(state.get("host", host))):
        time.sleep(2)
        state = read_state()
    if state and alive(int(state.get("port", 0)), str(state.get("host", host))):
        existing = url_for(str(state["host"]), int(state["port"]), page=page)
        if callable(on_ready):
            on_ready(existing, True)
        if open_browser:
            webbrowser.open(existing)
        return

    chosen = DEFAULT_PORT if port is None else port
    deadline = time.monotonic() + (60 if service else 0)
    while True:
        try:
            server = make_server(host=host, port=chosen)
            break
        except OSError:
            if time.monotonic() < deadline:
                time.sleep(1)  # the previous console is still releasing the port
                continue
            if port is not None:
                raise
            server = make_server(host=host, port=0)  # default port busy: take any
            break
    actual = int(server.server_port)
    _write_state(host=host, port=actual, token=server.token)
    public = host not in ("127.0.0.1", "localhost", "::1")
    address = url_for(host, actual, token=server.token if public else None, page=page)
    if callable(on_ready):
        on_ready(address, False)
    if open_browser:
        threading.Timer(0.6, webbrowser.open, args=(address,)).start()
    from ouroboros.gui.daemon import load_settings

    if load_settings()["shared_mcp"]:
        server.state.shared_mcp.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.state.shared_mcp.stop()
        server.server_close()
        _clear_state()
    if server.restart_requested:
        # Same port, fresh interpreter: picks up packages installed from the console.
        import sys

        argv = [
            sys.executable,
            "-m",
            "ouroboros",
            "gui",
            "--no-browser",
            "--port",
            str(actual),
            "--host",
            host,
        ]
        os.execv(sys.executable, argv)


__all__ = ["DEFAULT_PORT", "alive", "read_state", "serve", "url_for"]
