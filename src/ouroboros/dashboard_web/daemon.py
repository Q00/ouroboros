"""Singleton dashboard daemon — one web server, reused by every run.

The dashboard must never spawn a server per MCP session/run (that compounds the
process sprawl the user is already fighting). Instead:

- The FIRST run that wants a dashboard elects and spawns ONE detached daemon
  process (survives the ephemeral MCP session that started it).
- Every later run discovers it via a state file and REUSES it — same port, same
  URL, just a different ``?run=<execution_id>``.
- The daemon self-selects a free port (port is auto), records it in the state
  file, and SELF-EXITS after an idle period so it never lingers as a zombie.

Election is race-safe: an ``O_EXCL`` lock file serializes spawns; losers wait for
the winner's state file. Stale lock/state (crashed daemon) is detected via a
``/healthz`` probe and a lock-age timeout, then taken over.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from ouroboros.dashboard_web.reader import default_db_path

_HOME = Path.home() / ".ouroboros"
_STATE_PATH = _HOME / "dashboard.json"
_LOCK_PATH = _HOME / "dashboard.lock"

# A spawner holds the lock only while starting the daemon; if a lock outlives
# this it is stale (crashed spawner) and may be stolen.
_LOCK_STALE_SEC = 30.0
# How long a loser waits for the winner's daemon to come up before giving up.
_SPAWN_WAIT_SEC = 8.0
# Daemon self-exits after this long with no connected SSE client.
DEFAULT_IDLE_SHUTDOWN_SEC = 600.0


@dataclass(frozen=True)
class DashboardInfo:
    """A reachable dashboard: where it is and whether we reused an existing one."""

    url: str
    host: str
    port: int
    pid: int | None
    reused: bool

    def run_url(self, run_id: str) -> str:
        return f"{self.url}/?run={run_id}"


def _free_port(host: str = "127.0.0.1") -> int:
    with socket.socket() as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def healthz(host: str, port: int, *, timeout: float = 0.5) -> bool:
    """True iff a dashboard answers ``/healthz`` on host:port."""
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    try:
        conn = http.client.HTTPConnection(probe_host, port, timeout=timeout)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status == 200 and body.strip() == b"ok"
    except (OSError, http.client.HTTPException):
        return False


def read_state() -> dict | None:
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _normalize_db_path(db_path: str | os.PathLike[str] | None) -> str:
    return str(Path(db_path or default_db_path()).expanduser().resolve())


def write_state(*, host: str, port: int, pid: int, db_path: str) -> None:
    _HOME.mkdir(parents=True, exist_ok=True)
    payload = {
        "host": host,
        "port": port,
        "pid": pid,
        "db_path": _normalize_db_path(db_path),
        "started_at": datetime.now(UTC).isoformat(),
    }
    tmp = _STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, _STATE_PATH)  # atomic publish


def _state_alive(state: dict | None, *, db_path: str | None = None) -> bool:
    if not state:
        return False
    if db_path is not None and state.get("db_path") != _normalize_db_path(db_path):
        return False
    port = state.get("port")
    host = state.get("host", "127.0.0.1")
    return isinstance(port, int) and healthz(host, port)


def _info_from_state(state: dict, *, reused: bool) -> DashboardInfo:
    host = state.get("host", "127.0.0.1")
    port = int(state["port"])
    display = "localhost" if host in ("127.0.0.1", "0.0.0.0", "localhost") else host
    return DashboardInfo(
        url=f"http://{display}:{port}",
        host=host,
        port=port,
        pid=state.get("pid"),
        reused=reused,
    )


def _try_acquire_lock() -> int | None:
    """Atomically create the lock; return an fd on success, None if held & fresh."""
    _HOME.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        return fd
    except FileExistsError:
        # Steal if stale (crashed spawner left it behind).
        try:
            age = time.time() - _LOCK_PATH.stat().st_mtime
        except OSError:
            age = 0.0
        if age > _LOCK_STALE_SEC:
            try:
                _LOCK_PATH.unlink()
            except OSError:
                return None
            return _try_acquire_lock()
        return None


def _release_lock(fd: int) -> None:
    try:
        os.close(fd)
    finally:
        try:
            _LOCK_PATH.unlink()
        except OSError:
            pass


def _spawn_detached(*, db_path: str, host: str) -> None:
    """Launch the daemon as a detached process that outlives this MCP session."""
    subprocess.Popen(  # noqa: S603 - fixed argv, our own module
        [
            sys.executable,
            "-m",
            "ouroboros.dashboard_web",
            "--serve-daemon",
            "--host",
            host,
            "--db",
            db_path,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # detach: not killed when the MCP session exits
    )


def _wait_for_alive(deadline: float, *, db_path: str) -> DashboardInfo | None:
    while time.monotonic() < deadline:
        state = read_state()
        if _state_alive(state, db_path=db_path):
            return _info_from_state(state, reused=True)
        time.sleep(0.15)
    return None


def ensure_dashboard(
    *,
    db_path: str | None = None,
    host: str = "127.0.0.1",
) -> DashboardInfo | None:
    """Return a live dashboard, reusing the singleton or electing+spawning it.

    Returns ``None`` only if the daemon could not be brought up (the caller should
    degrade gracefully — observability must never block a run).
    """
    resolved_db = _normalize_db_path(db_path)

    # Fast path: an existing daemon is already serving this EventStore.
    state = read_state()
    if _state_alive(state, db_path=resolved_db):
        return _info_from_state(state, reused=True)

    # Elect a spawner. The lock serializes concurrent ensure() calls.
    fd = _try_acquire_lock()
    if fd is None:
        # Someone else is spawning — wait for their daemon, then reuse only if
        # it serves the same EventStore path this caller is about to link to.
        return _wait_for_alive(time.monotonic() + _SPAWN_WAIT_SEC, db_path=resolved_db)

    try:
        # Double-check inside the lock (a winner may have just published state).
        state = read_state()
        if _state_alive(state, db_path=resolved_db):
            return _info_from_state(state, reused=True)
        _spawn_detached(db_path=resolved_db, host=host)
        info = _wait_for_alive(time.monotonic() + _SPAWN_WAIT_SEC, db_path=resolved_db)
        if info is not None:
            return DashboardInfo(
                url=info.url, host=info.host, port=info.port, pid=info.pid, reused=False
            )
        return None
    finally:
        _release_lock(fd)


def is_enabled() -> bool:
    """Dashboard is ON by default; opt out via ``OUROBOROS_DASHBOARD=0|off|false|no``."""
    return os.environ.get("OUROBOROS_DASHBOARD", "").strip().lower() not in (
        "0",
        "off",
        "false",
        "no",
    )


def dashboard_url_for_run(
    run_id: str,
    *,
    db_path: str | None = None,
    host: str = "127.0.0.1",
) -> str | None:
    """Best-effort: ensure the singleton daemon and return this run's URL.

    Blocking (healthz probe / first-time spawn wait), so callers in an async
    context should offload with ``asyncio.to_thread``. Returns ``None`` when the
    dashboard is disabled or the daemon could not be brought up — observability
    must NEVER block or break a run.
    """
    if not is_enabled() or not run_id:
        return None
    try:
        info = ensure_dashboard(db_path=db_path, host=host)
    except Exception:  # noqa: BLE001 - observability is strictly best-effort
        return None
    return info.run_url(run_id) if info else None


def dashboard_base_url(
    *,
    db_path: str | None = None,
    host: str = "127.0.0.1",
) -> str | None:
    """Best-effort daemon base URL with no run pinned.

    For flows (e.g. ``auto``) whose execution id only appears later: the page
    auto-selects the latest active run, so the bare base URL is the right link.
    Returns ``None`` when disabled or unavailable.
    """
    if not is_enabled():
        return None
    try:
        info = ensure_dashboard(db_path=db_path, host=host)
    except Exception:  # noqa: BLE001 - best-effort observability
        return None
    return info.url if info else None


def run_daemon(*, db_path: str | None = None, host: str = "127.0.0.1") -> None:
    """Daemon entrypoint: self-select a port, publish state, serve until idle.

    Invoked as ``python -m ouroboros.dashboard_web --serve-daemon``.
    """
    from ouroboros.dashboard_web.server import serve_blocking

    resolved_db = _normalize_db_path(db_path)
    port = _free_port(host if host != "0.0.0.0" else "127.0.0.1")
    pid = os.getpid()
    write_state(host=host, port=port, pid=pid, db_path=resolved_db)
    try:
        serve_blocking(
            db_path=resolved_db,
            host=host,
            port=port,
            idle_shutdown_sec=DEFAULT_IDLE_SHUTDOWN_SEC,
        )
    finally:
        # Only clear state if it is still OURS (a newer daemon may have replaced it).
        current = read_state()
        if current and current.get("pid") == pid:
            try:
                _STATE_PATH.unlink()
            except OSError:
                pass


__all__ = [
    "DEFAULT_IDLE_SHUTDOWN_SEC",
    "DashboardInfo",
    "ensure_dashboard",
    "healthz",
    "read_state",
    "run_daemon",
    "write_state",
]
