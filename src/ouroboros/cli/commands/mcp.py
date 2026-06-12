"""MCP command group for Ouroboros.

Start and manage the MCP (Model Context Protocol) server.
"""

from __future__ import annotations

import asyncio
import contextlib
from enum import Enum
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Annotated, Any

from rich.console import Console
import typer

from ouroboros.cli.commands.mcp_doctor import register_doctor_command
from ouroboros.cli.formatters.panels import print_info, print_success
from ouroboros.orchestrator.heartbeat import (
    current_process_identity,
    is_process_identity_alive,
    process_start_time,
)

# Per-instance PID registry for stale-instance accounting. Many servers run
# concurrently (one per MCP client session), so a single-slot PID file is
# last-writer-wins and guards nothing: any exiting server used to delete the
# record of whichever server wrote last, and kill-advice built on it could
# target a healthy server owned by a live session. Each instance owns exactly
# one record keyed by its pid, stamped with the process start time so a
# recycled pid is never mistaken for a live server.
_PID_DIR = Path.home() / ".ouroboros"
_PID_REGISTRY_DIR = _PID_DIR / "mcp-servers"
# Single-slot file written by pre-registry versions; swept when stale.
_LEGACY_PID_FILE = _PID_DIR / "mcp-server.pid"

# Identity of the record this process wrote — compare-and-delete on cleanup.
_own_pid_file: Path | None = None
_own_pid_payload: str | None = None

# Shutdown pacing: how long to wait for the serve loop / background jobs to
# unwind before escalating (closing fd 0) or proceeding with store cleanup.
_SHUTDOWN_DRAIN_GRACE_SECONDS = 5.0
_JOB_DRAIN_GRACE_SECONDS = 5.0

# Idle WAL relief: long-lived idle servers pin the shared SQLite WAL (passive
# autocheckpoints cannot truncate while any reader is active).
_IDLE_CHECKPOINT_POLL_SECONDS = 300.0
_IDLE_CHECKPOINT_THRESHOLD_SECONDS = 600.0

# Separate stderr console for stdio transport (stdout is JSON-RPC channel)
_stderr_console = Console(stderr=True)


class AgentRuntimeBackend(str, Enum):  # noqa: UP042
    """Supported orchestrator runtime backends for MCP commands."""

    CLAUDE = "claude"
    CODEX = "codex"
    OPENCODE = "opencode"
    HERMES = "hermes"
    GEMINI = "gemini"
    KIRO = "kiro"
    COPILOT = "copilot"
    GOOSE = "goose"
    PI = "pi"
    GJC = "gjc"


class LLMBackend(str, Enum):  # noqa: UP042
    """Supported LLM-only backends for MCP commands."""

    CLAUDE_CODE = "claude_code"
    LITELLM = "litellm"
    CODEX = "codex"
    GOOSE = "goose"
    COPILOT = "copilot"
    OPENCODE = "opencode"
    GEMINI = "gemini"
    KIRO = "kiro"
    PI = "pi"


def _write_pid_file() -> bool:
    """Register this instance in the per-instance PID registry.

    Returns:
        True if the record was written successfully, False otherwise.
    """
    global _own_pid_file, _own_pid_payload
    pid, start_time = current_process_identity()
    payload = f"{pid} {start_time if start_time is not None else 'None'}"
    try:
        _PID_REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
        path = _PID_REGISTRY_DIR / f"{pid}.pid"
        path.write_text(payload, encoding="utf-8")
    except OSError:
        return False
    _own_pid_file = path
    _own_pid_payload = payload
    return True


def _cleanup_pid_file() -> None:
    """Remove only the registry record this process wrote (compare-and-delete).

    A blind unlink could delete a record that a pid-recycled successor wrote
    after a crash sweep; comparing the payload (pid + start time) guarantees
    each server only ever removes its own record.
    """
    global _own_pid_file, _own_pid_payload
    path, payload = _own_pid_file, _own_pid_payload
    _own_pid_file = None
    _own_pid_payload = None
    if path is None or payload is None:
        return
    try:
        if path.read_text(encoding="utf-8").strip() == payload.strip():
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _parse_pid_record(text: str) -> tuple[int, float | None] | None:
    """Parse a registry record of the form ``"<pid> <start_time|None>"``."""
    parts = text.strip().split()
    if not parts:
        return None
    try:
        pid = int(parts[0])
    except ValueError:
        return None
    start_time: float | None = None
    if len(parts) > 1 and parts[1] != "None":
        try:
            start_time = float(parts[1])
        except ValueError:
            return None
    return pid, start_time


def _record_is_stale(pid: int, start_time: float | None) -> bool:
    """True when a record's process identity is provably not running.

    Windows cannot probe liveness via signal 0 (``os.kill(pid, 0)`` raises
    ``OSError`` WinError 87) — treat as stale, preserving the degradation the
    legacy single-slot check used.
    """
    try:
        return not is_process_identity_alive(pid, start_time)
    except OSError:
        return True


def _sweep_stale_instances() -> int:
    """Drop registry records (and the legacy single-slot file) of dead servers.

    Returns the number of stale records removed.
    """
    removed = 0
    try:
        if _LEGACY_PID_FILE.exists():
            record = _parse_pid_record(_LEGACY_PID_FILE.read_text(encoding="utf-8"))
            if record is None or _record_is_stale(record[0], record[1]):
                _LEGACY_PID_FILE.unlink(missing_ok=True)
                removed += 1
    except OSError:
        pass
    try:
        entries = list(_PID_REGISTRY_DIR.iterdir())
    except OSError:
        return removed
    for entry in entries:
        try:
            record = _parse_pid_record(entry.read_text(encoding="utf-8"))
        except OSError:
            continue
        if record is None or _record_is_stale(record[0], record[1]):
            try:
                entry.unlink(missing_ok=True)
            except OSError:
                continue
            removed += 1
    return removed


def _live_instances() -> list[int]:
    """PIDs of registered, provably-live MCP server instances."""
    alive: list[int] = []
    try:
        entries = list(_PID_REGISTRY_DIR.iterdir())
    except OSError:
        return alive
    for entry in entries:
        try:
            record = _parse_pid_record(entry.read_text(encoding="utf-8"))
        except OSError:
            continue
        if record is not None and not _record_is_stale(record[0], record[1]):
            alive.append(record[0])
    return sorted(alive)


# Login-shell env import: cache + whitelist. Without the cache, every server
# start for subscription-auth users (no ANTHROPIC_API_KEY, so the fast path
# never engages) pays a full login shell sourcing ~/.zshrc — up to the 10s
# timeout — per instance. Without the whitelist, arbitrary login vars
# (PYTHONPATH, VIRTUAL_ENV, other vendors' secrets) leak into the server and
# every runtime it spawns.
_SHELL_ENV_CACHE_FILE = _PID_DIR / "shell-env.json"
_SHELL_ENV_CACHE_TTL_SECONDS = 3600.0


# Network plumbing the spawned runtimes need even though it is neither an API
# key nor ouroboros config: corporate proxies, custom CA bundles, gh auth.
_SHELL_ENV_EXACT_ALLOWED = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
    }
)


def _shell_env_key_allowed(key: str) -> bool:
    """Whitelist for login-shell env import (PATH is merged separately)."""
    return (
        key in _SHELL_ENV_EXACT_ALLOWED
        or key.endswith(("_API_KEY", "_BASE_URL", "_API_BASE"))
        or key.startswith("OUROBOROS_")
    )


def _load_cached_shell_env() -> dict[str, str] | None:
    """Return the cached login-shell env dump, or None when absent/stale."""
    try:
        stat = _SHELL_ENV_CACHE_FILE.stat()
        if time.time() - stat.st_mtime > _SHELL_ENV_CACHE_TTL_SECONDS:
            return None
        data = json.loads(_SHELL_ENV_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _store_shell_env_cache(env: dict[str, str]) -> None:
    """Persist the (already whitelisted) shell env dump; 0600 — it holds keys."""
    try:
        _PID_DIR.mkdir(parents=True, exist_ok=True)
        _SHELL_ENV_CACHE_FILE.write_text(json.dumps(env), encoding="utf-8")
        _SHELL_ENV_CACHE_FILE.chmod(0o600)
    except OSError:
        pass


def _ensure_shell_env(*, timeout: float = 10.0) -> None:
    """Load login-shell environment when launched outside a login shell.

    When an agent host process spawns ``ouroboros mcp serve``,
    the child inherits only a minimal environment. This sources the user's
    shell profile to recover PATH, ANTHROPIC_API_KEY, etc.

    Uses JSON serialization to avoid multiline env value parsing issues.
    Avoids the ``-i`` (interactive) flag which hangs on oh-my-zsh/p10k.
    """
    # Fast path: if key indicators are already present, skip
    if os.environ.get("ANTHROPIC_API_KEY"):
        return

    env = _load_cached_shell_env()
    if env is None:
        shell = os.environ.get("SHELL", "/bin/zsh" if sys.platform == "darwin" else "/bin/bash")
        shell_name = Path(shell).name

        # Dump env as JSON — unambiguous, handles multiline values
        dump_cmd = 'python3 -c "import os,json,sys; json.dump(dict(os.environ), sys.stdout)"'

        if shell_name == "zsh":
            cmd = [
                shell,
                "-l",
                "-c",
                f"[[ -f ~/.zshrc ]] && source ~/.zshrc 2>/dev/null; {dump_cmd}",
            ]
        elif shell_name == "bash":
            cmd = [
                shell,
                "-l",
                "-c",
                f"[[ -f ~/.bashrc ]] && source ~/.bashrc 2>/dev/null; {dump_cmd}",
            ]
        else:
            cmd = [shell, "-l", "-c", dump_cmd]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                timeout=timeout,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
            _stderr_console.print(f"[yellow]Warning: shell env load failed: {e}[/yellow]")
            return

        if result.returncode != 0:
            return

        try:
            env = json.loads(result.stdout)
        except json.JSONDecodeError:
            _stderr_console.print("[yellow]Warning: could not parse shell env output[/yellow]")
            return
        if not isinstance(env, dict):
            return
        # Cache only what the merge below may use — keeps the secret surface
        # of the on-disk cache as small as the merge itself.
        _store_shell_env_cache(
            {
                k: v
                for k, v in env.items()
                if isinstance(v, str) and (k == "PATH" or _shell_env_key_allowed(k))
            }
        )

    current_path_dirs = set(os.environ.get("PATH", "").split(os.pathsep))
    for key, val in env.items():
        if key == "PATH":
            new_dirs = [d for d in val.split(os.pathsep) if d and d not in current_path_dirs]
            if new_dirs:
                os.environ["PATH"] = (
                    os.pathsep.join(new_dirs) + os.pathsep + os.environ.get("PATH", "")
                )
        elif key not in os.environ and _shell_env_key_allowed(key):
            os.environ[key] = val


# Process-tree wrappers that sit between the real MCP client and this server.
# The shipped install path (`uvx --from ouroboros-ai ... ouroboros mcp serve`)
# interposes a uv wrapper that blocks on waitpid() and survives the client's
# death, so the *direct* parent is not the process whose lifetime matters.
_WRAPPER_BASENAMES = frozenset(
    {"uv", "uvx", "uv.exe", "uvx.exe", "sh", "bash", "zsh", "dash", "fish", "env"}
)


def _ps_value(pid: int, column: str) -> str | None:
    """Best-effort single-column ``ps`` lookup (POSIX only)."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", f"{column}="],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _client_is_alive(pid: int, start_time: float | None) -> bool:
    """Client liveness = identity-alive AND not a defunct (zombie) entry.

    A SIGKILLed client whose parent never reaps it keeps a signalable
    process-table entry — ``os.kill(pid, 0)`` succeeds — while its file
    descriptors are long gone. stdin EOF covers that case for stdio
    transports, but streamable-http has no EOF to fall back on, so the
    watchdog must treat a Z-state client as dead.
    """
    try:
        if not is_process_identity_alive(pid, start_time):
            return False
    except OSError:
        return False
    stat = _ps_value(pid, "stat")
    return stat is None or not stat.startswith("Z")


def _resolve_client_identity(orig_ppid: int) -> tuple[int, float | None] | None:
    """Resolve the real MCP client's process identity (pid, start time).

    Walks the ancestor chain from the direct parent, skipping known wrapper
    binaries (uv/uvx/shells), and returns the first non-wrapper ancestor —
    the process whose death means this server is orphaned. The recorded
    start time guards the later liveness polls against pid recycling.

    ``OUROBOROS_CLIENT_PID`` overrides the walk for spawners that want to
    pin the watched process explicitly. Returns None when the client cannot
    be resolved (Windows, ps failures, chain dead-ends at pid 1) — callers
    fall back to the plain getppid() watchdog.
    """
    if sys.platform == "win32":
        return None
    override = os.environ.get("OUROBOROS_CLIENT_PID")
    if override:
        try:
            override_pid = int(override)
        except ValueError:
            override_pid = 0
        if override_pid > 1:
            return override_pid, process_start_time(override_pid)
    pid = orig_ppid
    for _ in range(16):
        if pid <= 1:
            return None
        comm = _ps_value(pid, "comm")
        if comm is None:
            return None
        if Path(comm).name.lower() not in _WRAPPER_BASENAMES:
            return pid, process_start_time(pid)
        ppid_raw = _ps_value(pid, "ppid")
        if ppid_raw is None:
            return None
        try:
            pid = int(ppid_raw)
        except ValueError:
            return None
    return None


app = typer.Typer(
    name="mcp",
    help="MCP (Model Context Protocol) server commands.",
    no_args_is_help=True,
)

register_doctor_command(app)


async def _run_mcp_server(
    host: str,
    port: int,
    transport: str,
    db_path: str | None = None,
    runtime_backend: str | None = None,
    llm_backend: str | None = None,
) -> None:
    """Run the MCP server.

    Args:
        host: Host to bind to.
        port: Port to bind to.
        transport: Transport type (stdio, sse, or streamable-http).
        db_path: Optional path to EventStore database.
        runtime_backend: Optional orchestrator runtime backend override.
        llm_backend: Optional LLM-only backend override.
    """
    # Ensure login-shell environment is available (critical for gateway-spawned processes)
    _ensure_shell_env()

    from ouroboros.mcp.server.adapter import create_ouroboros_server, validate_transport
    from ouroboros.orchestrator.session import SessionRepository
    from ouroboros.persistence.brownfield import BrownfieldStore
    from ouroboros.persistence.event_store import EventStore

    # Validate transport early, before any expensive startup work
    try:
        transport = validate_transport(transport)
    except ValueError:
        _stderr_console.print(
            "[red]Invalid transport "
            f"{transport!r}. Must be 'stdio', 'sse', or 'streamable-http'.[/red]"
        )
        raise typer.Exit(code=1)

    _console_out = _stderr_console if transport == "stdio" else Console()

    # Create EventStore with custom path if provided
    if db_path:
        event_store = EventStore(f"sqlite+aiosqlite:///{db_path}")
        brownfield_store = BrownfieldStore(f"sqlite+aiosqlite:///{db_path}")
    else:
        event_store = EventStore()
        brownfield_store = BrownfieldStore()

    cleanup_task: asyncio.Task[None] | None = None

    # Initialize the persistent stores up front. The MCP server uses both for
    # request handling, so a partial init must surface as a clean startup
    # failure rather than a server that runs with a half-initialized store.
    await event_store.initialize()

    server: Any | None = None
    mcp_bridge: Any = None
    serve_task: asyncio.Task[None] | None = None
    stop_task: asyncio.Task[bool] | None = None
    watchdog_task: asyncio.Task[None] | None = None
    idle_checkpoint_task: asyncio.Task[None] | None = None
    serve_exc: BaseException | None = None

    # The protective try spans store init -> composition -> serve: a failure
    # anywhere after a store initialized (bridge discovery, backend validation
    # in create_ouroboros_server, transport setup) must still release the
    # stores — and run the WAL TRUNCATE checkpoint — instead of escaping with
    # them dangling.
    try:
        await brownfield_store.initialize()

        # Orphan cleanup is intentionally deferred into the background so large
        # SQLite histories do not block the initial MCP handshake on startup (#304).
        repo = SessionRepository(event_store)

        async def _run_startup_cleanup() -> None:
            try:
                cancelled = await repo.cancel_orphaned_sessions()
                if cancelled:
                    _console_out.print(
                        f"[yellow]Auto-cancelled {len(cancelled)} orphaned session(s)[/yellow]"
                    )
            except Exception as e:
                # Auto-cleanup is best-effort — don't prevent server startup
                _console_out.print(f"[yellow]Warning: auto-cleanup failed: {e}[/yellow]")

        cleanup_task = asyncio.create_task(
            _run_startup_cleanup(),
            name="ouroboros-mcp-startup-cleanup",
        )

        # Auto-discover and connect MCP bridge for server-to-server communication
        from ouroboros.mcp.bridge import create_bridge_from_env

        mcp_bridge = create_bridge_from_env(cwd=Path.cwd())
        if mcp_bridge is not None:
            try:
                results = await mcp_bridge.connect()
                connected = sum(1 for r in results.values() if r.is_ok)
                _console_out.print(
                    f"[blue]MCP Bridge: {connected}/{len(results)} upstream server(s) "
                    "connected[/blue]"
                )
            except Exception as e:
                _console_out.print(f"[yellow]MCP Bridge connection failed: {e}[/yellow]")
                mcp_bridge = None

        # Create server with all tools pre-registered via dependency injection.
        # Do NOT re-register OUROBOROS_TOOLS here — create_ouroboros_server already
        # registers handlers with proper dependencies (event_store, llm_adapter, etc.).
        server = create_ouroboros_server(
            name="ouroboros-mcp",
            version="1.0.0",
            event_store=event_store,
            brownfield_store=brownfield_store,
            runtime_backend=runtime_backend,
            llm_backend=llm_backend,
            mcp_bridge=mcp_bridge,
        )

        tool_count = len(server.info.tools)

        # Detect Codex seatbelt sandbox and warn about network restrictions.
        _sandbox_network_disabled = os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1"

        if transport == "stdio":
            # In stdio mode, stdout is the JSON-RPC channel.
            # All human-readable output must go to stderr.
            _stderr_console.print(f"[green]MCP Server starting on {transport}...[/green]")
            _stderr_console.print(f"[blue]Registered {tool_count} tools[/blue]")
            _stderr_console.print("[blue]Reading from stdin, writing to stdout[/blue]")
            _stderr_console.print("[blue]Press Ctrl+C to stop[/blue]")
        else:
            print_success(f"MCP Server starting on {transport}...")
            print_info(f"Registered {tool_count} tools")
            if transport == "streamable-http":
                print_info(f"Listening on http://{host}:{port}/mcp")
            else:
                print_info(f"Listening on {host}:{port}")
            print_info("Press Ctrl+C to stop")

        if _sandbox_network_disabled:
            _console_out.print(
                "[dim]Note: CODEX_SANDBOX_NETWORK_DISABLED=1 detected. "
                "MCP-spawned runtimes usually retain network access. "
                "If agent tasks fail with network errors, try: "
                "--sandbox danger-full-access[/dim]"
            )

        # Register this instance and sweep records of dead peers.
        swept = _sweep_stale_instances()
        if swept:
            msg = f"Cleaned up {swept} stale MCP server PID record(s)"
            if transport == "stdio":
                _stderr_console.print(f"[yellow]{msg}[/yellow]")
            else:
                print_info(msg)

        _write_pid_file()

        # Start serving with graceful shutdown + orphan reaping.
        #
        # asyncio.run() only translates SIGINT into KeyboardInterrupt; an unhandled
        # SIGTERM would terminate the process immediately and skip the finally block
        # below — leaking the PID record and, more importantly, skipping the
        # EventStore.close() WAL TRUNCATE checkpoint (the -wal file then grows
        # unbounded across many concurrent sessions). Install explicit handlers and
        # race the serve task against a stop Event so every shutdown path is clean.
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _request_stop(signame: str) -> None:
            _console_out.print(f"[blue]Received {signame}, shutting down...[/blue]")
            stop.set()

        # Resolve signals by name: SIGHUP is POSIX-only, so referencing
        # ``signal.SIGHUP`` directly would raise AttributeError while *building* the
        # loop iterable on Windows — before the suppress() below could catch it.
        # getattr keeps SIGHUP on POSIX and skips it where the constant is absent.
        for _signame in ("SIGTERM", "SIGINT", "SIGHUP"):
            _sig = getattr(signal, _signame, None)
            if _sig is None:
                continue
            # add_signal_handler is unavailable on some event loops (e.g. the
            # Windows Proactor loop); fall back silently — KeyboardInterrupt still
            # covers SIGINT there.
            with contextlib.suppress(NotImplementedError, ValueError, RuntimeError):
                loop.add_signal_handler(_sig, _request_stop, _sig.name)

        # Client-death watchdog: when the MCP client that spawned us dies, exit
        # instead of pinning the SQLite database forever (streamable-http has no
        # stdin EOF to rely on; for stdio, EOF stays the primary defense). Two
        # complementary checks, polled every 5s:
        #  - getppid() vs the original parent: catches death of whatever spawned
        #    us directly. Under the shipped `client -> uvx -> python` topology the
        #    direct parent is the uv wrapper, which blocks on waitpid() and
        #    survives the client's death — this check alone can never fire there
        #    (the orphaned wrapper is reparented; our own ppid never changes).
        #  - the resolved *client* identity (nearest non-wrapper ancestor at
        #    startup, pid + start time): catches the real client dying behind the
        #    wrapper. Polling an absolute pid identity is immune to subreapers
        #    (systemd --user, tini) and to pid recycling. OUROBOROS_CLIENT_PID
        #    overrides the ancestor walk for spawners that want to pin the
        #    watched process explicitly.
        # Skipped when launched already-detached on purpose (orig_ppid == 1,
        # e.g. a real launchd/systemd service — such servers must never
        # self-terminate). Not effective on Windows (no POSIX ps, no
        # reparent-on-death model); SIGINT/stdin EOF cover the common cases
        # there.
        orig_ppid = os.getppid()
        client_identity: tuple[int, float | None] | None = None
        if orig_ppid != 1:
            # ps lookups can block up to their subprocess timeout — resolve off
            # the event loop so a slow ps never stalls the MCP handshake.
            client_identity = await asyncio.to_thread(_resolve_client_identity, orig_ppid)

        async def _orphan_watchdog() -> None:
            if orig_ppid == 1:
                return
            while not stop.is_set():
                if os.getppid() != orig_ppid:
                    _console_out.print("[yellow]Parent client gone — orphan exit[/yellow]")
                    stop.set()
                    return
                if client_identity is not None:
                    client_pid, client_start = client_identity
                    alive = await asyncio.to_thread(_client_is_alive, client_pid, client_start)
                    if not alive:
                        _console_out.print(
                            f"[yellow]MCP client (pid {client_pid}) gone — orphan exit[/yellow]"
                        )
                        stop.set()
                        return
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=5.0)

        async def _idle_wal_checkpoint() -> None:
            # Long-lived idle servers pin the shared WAL: passive autocheckpoints
            # cannot truncate while any reader is active, so N concurrent idle
            # sessions let the -wal file grow unbounded. Best-effort TRUNCATE
            # when no tool call has arrived for a while; deliberately never on
            # the startup path (#304) and silent on contention.
            while not stop.is_set():
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=_IDLE_CHECKPOINT_POLL_SECONDS)
                if stop.is_set():
                    return
                idle_for = getattr(server, "seconds_since_last_tool_call", None)
                if not isinstance(idle_for, int | float):
                    return
                if idle_for < _IDLE_CHECKPOINT_THRESHOLD_SECONDS:
                    continue
                with contextlib.suppress(Exception):
                    await event_store.checkpoint_wal()

        serve_task = asyncio.create_task(
            server.serve(transport=transport, host=host, port=port),
            name="ouroboros-mcp-serve",
        )
        stop_task = asyncio.create_task(stop.wait(), name="ouroboros-mcp-stop")
        watchdog_task = asyncio.create_task(_orphan_watchdog(), name="ouroboros-mcp-watchdog")
        idle_checkpoint_task = asyncio.create_task(
            _idle_wal_checkpoint(), name="ouroboros-mcp-idle-checkpoint"
        )

        await asyncio.wait(
            {serve_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        # Runs for SIGTERM, orphan-exit and KeyboardInterrupt too, so
        # EventStore.close() always gets to collapse the WAL.
        helper_tasks = [
            t for t in (watchdog_task, stop_task, idle_checkpoint_task) if t is not None
        ]
        for _task in helper_tasks:
            if not _task.done():
                _task.cancel()
        if serve_task is not None and not serve_task.done():
            serve_task.cancel()
        # Bound the serve drain: the MCP SDK's stdio session reads stdin via a
        # shielded worker thread (anyio readline, abandon_on_cancel=False), so an
        # unbounded ``await serve_task`` hangs forever when shutdown was requested
        # by a signal or the watchdog while the client is alive but quiescent —
        # the exact "server survives kill" symptom. After the grace, closing fd 0
        # EOFs the blocked readline (verified empirically on macOS, the primary
        # fleet; best-effort elsewhere — a second bounded wait below means a
        # non-waking platform still proceeds to cleanup). os._exit is
        # deliberately NOT used anywhere here: every exit must run the store
        # cleanup below.
        pending: set[asyncio.Task[Any]] = {
            t for t in (serve_task, *helper_tasks) if t is not None and not t.done()
        }
        if pending:
            _, pending = await asyncio.wait(pending, timeout=_SHUTDOWN_DRAIN_GRACE_SECONDS)
            if serve_task is not None and serve_task in pending and transport == "stdio":
                with contextlib.suppress(OSError):
                    os.close(0)
                _, pending = await asyncio.wait(pending, timeout=_SHUTDOWN_DRAIN_GRACE_SECONDS)
            if pending:
                _console_out.print(
                    "[yellow]Serve loop did not stop within the shutdown grace; "
                    "continuing cleanup[/yellow]"
                )
        # Retrieve parked results so completed tasks never log
        # "exception was never retrieved" during interpreter teardown.
        for _task in (serve_task, *helper_tasks):
            if _task is not None and _task.done():
                with contextlib.suppress(BaseException):
                    _task.exception()
        if cleanup_task is not None and not cleanup_task.done():
            cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup_task
        if server is not None:
            # Drain background jobs BEFORE the stores close: job tasks killed by
            # asyncio.run teardown after EventStore.close() fail their terminal
            # appends with PersistenceError and leave RUNNING zombie rows that
            # the dead-owner reconciler must repair on a later read.
            from ouroboros.mcp.job_manager import JobManager

            job_manager = getattr(server, "job_manager", None)
            if isinstance(job_manager, JobManager):
                with contextlib.suppress(Exception):
                    await job_manager.drain(grace_seconds=_JOB_DRAIN_GRACE_SECONDS)
            # Route teardown through the adapter so its owned resources close in
            # the documented order: the ControlBus reactive surface is drained
            # first (cancelling subscriber tasks), then the EventStore (whose
            # close() collapses the WAL) and BrownfieldStore, then the MCP
            # bridge. Closing the stores directly here would bypass that
            # contract and leave control-bus tasks and upstream bridge
            # connections dangling. Best effort — a drain/close failure is
            # already logged inside shutdown(); never let it escape the cleanup
            # path.
            with contextlib.suppress(Exception):
                await server.shutdown()
        else:
            # Composition failed before the adapter existed: release what this
            # function owns directly, in reverse init order, so an early failure
            # cannot leak initialized stores (and their WAL).
            if mcp_bridge is not None:
                with contextlib.suppress(Exception):
                    await mcp_bridge.close()
            with contextlib.suppress(Exception):
                await brownfield_store.close()
            with contextlib.suppress(Exception):
                await event_store.close()
        _cleanup_pid_file()
        # Single error-propagation point: preserve a serve-loop failure (bind/
        # listen/runtime errors — asyncio.wait() leaves the exception parked on
        # the task) but raise it only after cleanup, from OUTSIDE this finally.
        # A raise inside finally can mask an in-flight CancelledError and bypass
        # the KeyboardInterrupt clean-exit handler on the signal-fallback path.
        # A cancelled serve task is the intended shutdown path
        # (SIGTERM/orphan-exit/stdin EOF) and propagates nothing.
        if serve_task is not None and serve_task.done() and not serve_task.cancelled():
            serve_exc = serve_task.exception()

    # Surface a serve-loop failure only after cleanup has collapsed the WAL and
    # released the stores. This preserves the error-propagation contract of the
    # prior ``await server.serve(...)`` so ``ouroboros mcp serve`` exits non-zero
    # on startup/runtime failures instead of reporting a clean stop.
    if serve_exc is not None:
        raise serve_exc


@app.command()
def serve(
    host: Annotated[
        str,
        typer.Option(
            "--host",
            "-h",
            help="Host to bind to.",
        ),
    ] = "localhost",
    port: Annotated[
        int,
        typer.Option(
            "--port",
            "-p",
            help="Port to bind to.",
        ),
    ] = 8080,
    transport: Annotated[
        str,
        typer.Option(
            "--transport",
            "-t",
            help="Transport type: stdio, sse, or streamable-http.",
        ),
    ] = "stdio",
    db: Annotated[
        str,
        typer.Option(
            "--db",
            help="Path to EventStore database (default: ~/.ouroboros/ouroboros.db)",
        ),
    ] = "",
    runtime: Annotated[
        AgentRuntimeBackend | None,
        typer.Option(
            "--runtime",
            help="Agent runtime backend for orchestrator-driven tools (claude, codex, opencode, hermes, gemini, copilot, goose, kiro, or pi).",
            case_sensitive=False,
        ),
    ] = None,
    llm_backend: Annotated[
        LLMBackend | None,
        typer.Option(
            "--llm-backend",
            help=(
                "LLM backend for interview/seed/evaluation tools (claude_code, litellm, codex, copilot, opencode, gemini, goose, kiro, or pi)."
            ),
            case_sensitive=False,
        ),
    ] = None,
) -> None:
    """Start the MCP server.

    Exposes Ouroboros functionality via Model Context Protocol,
    allowing Claude Desktop and other MCP clients to interact
    with Ouroboros.

    Available tools:
    - ouroboros_execute_seed: Execute a seed specification
    - ouroboros_session_status: Get session status
    - ouroboros_query_events: Query event history

    Examples:

        # Start with stdio transport (for Claude Desktop)
        ouroboros mcp serve

        # Start with SSE transport on custom port
        ouroboros mcp serve --transport sse --port 9000

        # Start with streamable HTTP transport for Codex CLI --url clients
        ouroboros mcp serve --transport streamable-http --port 9000

        # Start with OpenCode runtime
        ouroboros mcp serve --runtime opencode

        # Use Codex CLI for LLM-only tools as well
        ouroboros mcp serve --runtime codex --llm-backend codex

    """
    # Guard: prevent recursive MCP server spawning.
    # When ouroboros spawns a runtime (Codex/Claude/OpenCode), the child process
    # inherits this env var. If that runtime's MCP config tries to spawn another
    # ouroboros server, the nested instance exits cleanly instead of creating a
    # process tree explosion.
    if os.environ.get("_OUROBOROS_NESTED"):
        _stderr_console.print("[dim]Nested ouroboros MCP server detected — exiting cleanly[/dim]")
        raise typer.Exit(0)
    os.environ["_OUROBOROS_NESTED"] = "1"

    try:
        db_path = db if db else None
        asyncio.run(
            _run_mcp_server(
                host,
                port,
                transport,
                db_path,
                runtime.value if runtime else None,
                llm_backend.value if llm_backend else None,
            )
        )
    except KeyboardInterrupt:
        _stderr_console.print("[blue]MCP Server stopped[/blue]")
    except ImportError as e:
        _stderr_console.print(f"[red]MCP dependencies not installed: {e}[/red]")
        _stderr_console.print("[blue]Install with: uv add mcp[/blue]")
        raise typer.Exit(1) from e
    except OSError as e:
        _stderr_console.print(f"[red]MCP Server failed to start: {e}[/red]")
        live = _live_instances()
        if live:
            _stderr_console.print(
                "[blue]Live MCP server instances (one per connected client): "
                f"{', '.join(str(pid) for pid in live)}. These are normally owned "
                "by running agent sessions — do not kill them blindly; stop the "
                "owning client instead.[/blue]"
            )
        _stderr_console.print(
            "[blue]If this keeps happening, try:\n"
            f"  1. Inspect registered instances: ls {_PID_REGISTRY_DIR}\n"
            "  2. Run diagnostics: ouroboros mcp doctor\n"
            "  3. Restart your MCP client[/blue]"
        )
        raise typer.Exit(1) from e


@app.command()
def info(
    runtime: Annotated[
        AgentRuntimeBackend | None,
        typer.Option(
            "--runtime",
            help="Agent runtime backend for orchestrator-driven tools (claude, codex, opencode, hermes, gemini, copilot, goose, kiro, or pi).",
            case_sensitive=False,
        ),
    ] = None,
    llm_backend: Annotated[
        LLMBackend | None,
        typer.Option(
            "--llm-backend",
            help=(
                "LLM backend for interview/seed/evaluation tools (claude_code, litellm, codex, copilot, opencode, gemini, goose, kiro, or pi)."
            ),
            case_sensitive=False,
        ),
    ] = None,
) -> None:
    """Show MCP server information and available tools."""
    from ouroboros.cli.formatters import console
    from ouroboros.mcp.server.adapter import create_ouroboros_server

    # Create server with all tools pre-registered
    server = create_ouroboros_server(
        name="ouroboros-mcp",
        version="1.0.0",
        runtime_backend=runtime.value if runtime else None,
        llm_backend=llm_backend.value if llm_backend else None,
    )

    server_info = server.info

    console.print()
    console.print("[bold]MCP Server Information[/bold]")
    console.print(f"  Name: {server_info.name}")
    console.print(f"  Version: {server_info.version}")
    console.print()

    console.print("[bold]Capabilities[/bold]")
    console.print(f"  Tools: {server_info.capabilities.tools}")
    console.print(f"  Resources: {server_info.capabilities.resources}")
    console.print(f"  Prompts: {server_info.capabilities.prompts}")
    console.print()

    console.print("[bold]Available Tools[/bold]")
    for tool in server_info.tools:
        console.print(f"  [green]{tool.name}[/green]")
        console.print(f"    {tool.description}")
        if tool.parameters:
            console.print("    Parameters:")
            for param in tool.parameters:
                required = "[red]*[/red]" if param.required else ""
                console.print(f"      - {param.name}{required}: {param.description}")
        console.print()


__all__ = ["app"]
