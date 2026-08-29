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
import shutil
import signal
import subprocess
import sys
import time
from typing import Annotated, Any

from rich.console import Console
from rich.text import Text
import structlog
import typer

from ouroboros.backends import resolve_runtime_backend_name
from ouroboros.cli.commands.mcp_doctor import register_doctor_command
from ouroboros.cli.formatters.panels import print_info, print_success
from ouroboros.config import (
    get_agent_runtime_backend,
    get_cli_path,
    get_codex_cli_path,
    get_opencode_cli_path,
)
from ouroboros.orchestrator.heartbeat import (
    current_process_identity,
    is_process_identity_alive,
    process_start_time,
)
from ouroboros.package_profiles import (
    CLAUDE_CLI_RUNTIME_BACKEND,
    SDK_RUNTIME_IN_MCP_SERVER_MESSAGE,
    UNSUPPORTED_CLAUDE_SDK_MCP_MESSAGE,
    has_unsupported_claude_sdk_mcp_mix,
    public_runtime_backend,
)
from ouroboros.package_profiles import (
    PublicAgentRuntimeBackend as AgentRuntimeBackend,
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
# How long the serve task must survive (or finish cleanly) before the daily
# `mcp_serve_started` attachment row is recorded — long enough for a network
# transport's bind/listen failure to surface, short enough to be irrelevant
# for a once-per-day metric.
_ATTACH_CONFIRM_SECONDS = 3.0
_JOB_DRAIN_GRACE_SECONDS = 5.0

# Idle WAL relief: long-lived idle servers pin the shared SQLite WAL (passive
# autocheckpoints cannot truncate while any reader is active).
_IDLE_CHECKPOINT_POLL_SECONDS = 300.0
_IDLE_CHECKPOINT_THRESHOLD_SECONDS = 600.0

# Separate stderr console for stdio transport (stdout is JSON-RPC channel)
_stderr_console = Console(stderr=True)
log = structlog.get_logger(__name__)


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
    OMP = "omp"
    ZCODE = "zcode"
    DSH = "dsh"


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

    The shared liveness probe uses Win32 process handles on Windows and
    preserves a lease when the OS cannot decide. The fallback still preserves
    the legacy behavior for unexpected errors escaping other platform probes.
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
    # Always merge the cached/login-shell whitelist. A single provider key is
    # not proof that every configured backend is ready: an MCP server can use
    # Anthropic for one stage and Codex/OpenAI for another, and detached workers
    # inherit only this merged environment. The cache keeps this path cheap.
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
# The shipped install path
# (`uvx --isolated --python >=3.12 --from ouroboros-ai[mcp] ouroboros mcp serve`)
# interposes a uv wrapper that blocks on waitpid() and survives the client's
# death, so the *direct* parent is not the process whose lifetime matters.
_WRAPPER_BASENAMES = frozenset(
    {"uv", "uvx", "uv.exe", "uvx.exe", "sh", "bash", "zsh", "dash", "fish", "env"}
)

# Slack for the watchdog's start-marker comparison. The Linux marker is exact,
# so this only absorbs the 1-second resolution of the Darwin `ps lstart` path.
_START_MARKER_TOLERANCE_SECONDS = 2.0


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


def _process_start_marker(pid: int) -> float | None:
    """Drift-free start marker for the watchdog's own liveness comparison.

    On Linux this is seconds since boot, taken straight from
    ``/proc/<pid>/stat`` field 22, which never moves while the process lives.
    It is deliberately *not* the epoch value from ``process_start_time()``:
    that adds ``/proc/stat``'s ``btime``, which WSL2 re-derives from a clock
    that resyncs, so it drifts upward for an unchanged pid and eventually
    trips the tolerance below — the false "client gone" of #1699.

    This marker is recorded and compared inside a single process and is never
    persisted, so a boot-relative value is sufficient here. The epoch form
    stays the cross-process identity used by lease payloads, the PID registry
    and detached-job ownership, and is left untouched.
    """
    if sys.platform == "darwin":
        # ps lstart does not drift; reuse the shared helper there.
        return process_start_time(pid)
    # Read bytes: comm carries the raw process name, which the kernel accepts
    # as arbitrary non-NUL bytes. read_text() would raise UnicodeDecodeError —
    # not an OSError — on a legal name like b"bad-\xff-name", and that would
    # escape _resolve_client_identity and abort server startup.
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return None
    # Field 2 (comm) is parenthesised and may itself contain spaces and ')',
    # so split only what follows the LAST ')' — field 3 (state) onwards.
    close = raw.rfind(b")")
    if close == -1:
        return None
    fields = raw[close + 1 :].split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[19]) / os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError, ZeroDivisionError):
        return None


def _client_is_alive(pid: int, start_marker: float | None) -> bool:
    """Client liveness = identity-alive AND not a defunct (zombie) entry.

    A SIGKILLed client whose parent never reaps it keeps a signalable
    process-table entry — ``os.kill(pid, 0)`` succeeds — while its file
    descriptors are long gone. stdin EOF covers that case for stdio
    transports, but streamable-http has no EOF to fall back on, so the
    watchdog must treat a Z-state client as dead.

    ``start_marker`` comes from ``_process_start_marker`` and guards against
    pid recycling.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    if start_marker is not None:
        current = _process_start_marker(pid)
        if current is not None and abs(current - start_marker) > _START_MARKER_TOLERANCE_SECONDS:
            return False
    stat = _ps_value(pid, "stat")
    return stat is None or not stat.startswith("Z")


def _resolve_client_identity(orig_ppid: int) -> tuple[int, float | None] | None:
    """Resolve the real MCP client's process identity (pid, start marker).

    Walks the ancestor chain from the direct parent, skipping known wrapper
    binaries (uv/uvx/shells), and returns the first non-wrapper ancestor —
    the process whose death means this server is orphaned. The recorded
    start marker (see ``_process_start_marker``) guards the later liveness
    polls against pid recycling.

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
            return override_pid, _process_start_marker(override_pid)
    pid = orig_ppid
    for _ in range(16):
        if pid <= 1:
            return None
        comm = _ps_value(pid, "comm")
        if comm is None:
            return None
        if Path(comm).name.lower() not in _WRAPPER_BASENAMES:
            return pid, _process_start_marker(pid)
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


def _effective_mcp_server_runtime(runtime: AgentRuntimeBackend | None) -> str:
    """Resolve the runtime the MCP 2 composition root would actually select."""
    requested = runtime.value if runtime is not None else get_agent_runtime_backend()
    normalized = public_runtime_backend(requested)
    if normalized is None:  # Defensive: the configured/default resolver always returns text.
        normalized = "claude"
    return resolve_runtime_backend_name(normalized)


# Executable stand-ins for the in-process ``claude`` SDK runtime, in preference
# order. Each row carries the canonical backend, public spelling, configured
# path resolver, and bare command used by the runtime factory.
_SDK_RUNTIME_STANDINS = (
    (CLAUDE_CLI_RUNTIME_BACKEND, AgentRuntimeBackend.CLAUDE_CLI.value, get_cli_path, "claude"),
    ("codex", AgentRuntimeBackend.CODEX.value, get_codex_cli_path, "codex"),
    ("opencode", AgentRuntimeBackend.OPENCODE.value, get_opencode_cli_path, "opencode"),
)


def _runtime_profile_controls_stage_backends() -> bool:
    """Return whether stage routing can override the top-level fallback."""
    try:
        from ouroboros.config.loader import load_config

        profile = load_config().orchestrator.runtime_profile
    except Exception:
        return False
    return profile is not None and bool(profile.default or profile.stages)


def _sdk_runtime_standin(runtime: AgentRuntimeBackend | None) -> tuple[str, str] | None:
    """Return an executable runtime to use in place of an inherited SDK default.

    Explicit CLI or environment selections remain authoritative. For inherited
    config/default selection, hydrate the detached host's login-shell PATH and
    reuse the backend-specific executable path resolvers used by runtime
    construction instead of maintaining a narrower availability definition.
    """
    if runtime is not None:
        return None
    if any(
        os.environ.get(key, "").strip() for key in ("OUROBOROS_AGENT_RUNTIME", "OUROBOROS_RUNTIME")
    ):
        return None
    if _runtime_profile_controls_stage_backends():
        return None
    for backend, public_name, configured_path, command in _SDK_RUNTIME_STANDINS:
        executable = configured_path() or command
        if shutil.which(executable):
            return backend, public_name
    return None


def _require_mcp_dependency() -> None:
    """Fail before MCP server composition when the MCP v2 API is unavailable.

    ``create_ouroboros_server()`` can build its internal tool catalogue without
    importing the MCP SDK v2 server surface. Deferring that import until
    ``server.serve()`` makes a missing or incompatible dependency appear as a
    successful stdio startup followed by a disconnected server. Validate the
    exact API consumed by the adapter at the command boundary instead.
    """
    try:
        from mcp.server import MCPServer as _sdk_mcp_server

        if _sdk_mcp_server is None:  # pragma: no cover - defensive import contract.
            raise ImportError
    except ImportError as exc:
        raise ImportError(
            "MCP SDK v2 server API unavailable. Install with: pip install 'ouroboros-ai[mcp]'"
        ) from exc


async def _run_mcp_server(
    host: str,
    port: int,
    transport: str,
    db_path: str | None = None,
    runtime_backend: str | None = None,
    llm_backend: str | None = None,
    *,
    auth_token: str = "",
    allowed_hosts: tuple[str, ...] = (),
    allowed_origins: tuple[str, ...] = (),
    workspace_roots: tuple[str, ...] = (),
) -> None:
    """Run the MCP server.

    Args:
        host: Host to bind to.
        port: Port to bind to.
        transport: Transport type (stdio, sse, or streamable-http).
        db_path: Optional path to EventStore database.
        runtime_backend: Optional orchestrator runtime backend override.
        llm_backend: Optional LLM-only backend override.
        auth_token: Shared secret network clients must present. Empty leaves
            the server credential-free, which serving only permits on loopback.
        allowed_hosts: ``Host`` header allowlist for network transports.
        allowed_origins: ``Origin`` header allowlist for network transports.
        workspace_roots: Directories seed execution is confined to. Empty
            leaves execution unrestricted, the historical local behaviour.
    """
    from ouroboros.mcp.server.adapter import validate_transport

    # Validate transport early, before any expensive startup work
    try:
        transport = validate_transport(transport)
    except ValueError:
        _stderr_console.print(
            "[red]Invalid transport "
            f"{transport!r}. Must be 'stdio', 'sse', or 'streamable-http'.[/red]"
        )
        raise typer.Exit(code=1)

    _require_mcp_dependency()

    # Ensure login-shell environment is available (critical for gateway-spawned processes)
    _ensure_shell_env()

    from ouroboros.config.models import resolve_event_store_path
    from ouroboros.mcp.server.adapter import create_ouroboros_server
    from ouroboros.orchestrator.session import SessionRepository
    from ouroboros.persistence.brownfield import BrownfieldStore
    from ouroboros.persistence.event_store import EventStore, sqlite_database_url

    _console_out = _stderr_console if transport == "stdio" else Console()

    # Resolve once so both stores share one durable authority even if config
    # changes while the long-lived MCP process is starting.
    try:
        if db_path:
            resolved_db_path = Path(db_path).expanduser()
        else:
            resolved_db_path = resolve_event_store_path()
        resolved_db_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_exists = resolved_db_path.exists() or resolved_db_path.is_symlink()
        if resolved_exists and not resolved_db_path.is_file():
            raise ValueError("invalid EventStore target")
        database_url = sqlite_database_url(resolved_db_path)
    except (OSError, RuntimeError, ValueError):
        _console_out.print("[red]Invalid EventStore configuration.[/red]")
        raise typer.Exit(1) from None
    event_store = EventStore(database_url)
    brownfield_store = BrownfieldStore(database_url)

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

        mcp_bridge = create_bridge_from_env()
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
        # Install before any tool can run: the policy is what keeps a caller
        # from naming an arbitrary directory as an agent's working tree.
        if workspace_roots:
            from ouroboros.mcp.server.workspace import WorkspacePolicy, set_workspace_policy

            set_workspace_policy(WorkspacePolicy.from_paths(workspace_roots))

        # A token turns on the SDK's bearer-auth middleware; without one the
        # server stays credential-free, which serve() only allows on loopback.
        auth_config = None
        if auth_token:
            from ouroboros.mcp.server.security import AuthConfig, AuthMethod

            auth_config = AuthConfig(
                method=AuthMethod.API_KEY,
                api_keys=frozenset({auth_token}),
                required=True,
            )

        server = create_ouroboros_server(
            name="ouroboros-mcp",
            auth_config=auth_config,
            event_store=event_store,
            brownfield_store=brownfield_store,
            runtime_backend=runtime_backend,
            llm_backend=llm_backend,
            mcp_bridge=mcp_bridge,
        )

        # Serve startup is the one place that may prime the update-notice
        # cache (#2066): non-serving server constructions stay network-free.
        from ouroboros.mcp.update_notice import maybe_schedule_cache_refresh

        maybe_schedule_cache_refresh()
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

            # State the posture plainly: these two lines are what an operator
            # needs to see to know whether this port is safe where it sits.
            if auth_token:
                print_info("Auth: bearer token required")
            else:
                print_info("Auth: none — reachable only from this machine")
            if workspace_roots:
                print_info(f"Seed execution confined to: {', '.join(workspace_roots)}")
            else:
                _console_out.print(
                    "[yellow]Seed execution is not confined: a caller may name any "
                    "existing directory on this machine as an agent working tree. "
                    "Pass --workspace-root to restrict it.[/yellow]"
                )
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
            server.serve(
                transport=transport,
                host=host,
                port=port,
                allowed_hosts=allowed_hosts,
                allowed_origins=allowed_origins,
            ),
            name="ouroboros-mcp-serve",
        )
        stop_task = asyncio.create_task(stop.wait(), name="ouroboros-mcp-stop")
        watchdog_task = asyncio.create_task(_orphan_watchdog(), name="ouroboros-mcp-watchdog")
        idle_checkpoint_task = asyncio.create_task(
            _idle_wal_checkpoint(), name="ouroboros-mcp-idle-checkpoint"
        )

        # One daily-deduplicated "MCP attached" row per user — the denominator
        # of the attached → used activation funnel. The row must reflect a
        # server that actually reached its serve loop: a transport that fails
        # to bind/listen (e.g. "address already in use") completes serve_task
        # immediately with an exception and must not count. Wait out a short
        # confirmation window; a serve task still running afterwards — or one
        # that already finished cleanly (a real, short session) — is an
        # authoritative attachment.
        done, _pending = await asyncio.wait(
            {serve_task, stop_task},
            timeout=_ATTACH_CONFIRM_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        serve_failed_before_ready = (
            serve_task.done() and not serve_task.cancelled() and serve_task.exception() is not None
        )
        if not serve_failed_before_ready:
            from ouroboros import telemetry as usage_telemetry

            usage_telemetry.capture_mcp_serve_started(transport)
        if not done:
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
                    log.info(
                        "mcp.command.job_drain_start",
                        live_job_count=len(getattr(job_manager, "_tasks", {})),
                        grace_seconds=_JOB_DRAIN_GRACE_SECONDS,
                    )
                    drained = await job_manager.drain(grace_seconds=_JOB_DRAIN_GRACE_SECONDS)
                    log.info("mcp.command.job_drain_complete", drained=drained)
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


def _resolve_network_security(
    *,
    transport: str,
    host: str,
    port: int,
    auth_token: str,
    allow_remote: bool,
    allowed_hosts: tuple[str, ...],
) -> str:
    """Validate the requested exposure and return the token to enforce.

    ``MCPServerAdapter.serve`` holds the same boundary for embedders. This runs
    first so an operator gets an actionable message rather than a traceback,
    and so the reason a bind was refused names the flag that would allow it.

    Args:
        transport: The already-validated transport name.
        host: The requested bind address.
        port: The requested bind port, quoted back in the guidance.
        auth_token: The shared secret, from the flag or the environment.
        allow_remote: Whether the operator acknowledged remote exposure.
        allowed_hosts: The ``Host`` header allowlist.

    Returns:
        The token to enforce, or "" when the bind requires no credential.

    Raises:
        typer.Exit: If the requested bind would expose seed execution.
    """
    from ouroboros.mcp.server.auth import (
        NETWORK_TRANSPORTS,
        is_loopback_host,
        is_wildcard_host,
    )

    if transport not in NETWORK_TRANSPORTS:
        if auth_token:
            _stderr_console.print(
                "[yellow]Ignoring --auth-token: stdio has no request headers to "
                "carry it, and the client already owns this process.[/yellow]"
            )
        return ""

    if is_loopback_host(host):
        return auth_token

    problems: list[str] = []
    if not auth_token:
        problems.append(
            "  --auth-token <secret>   (or set OUROBOROS_MCP_AUTH_TOKEN)\n"
            "      Without it, anyone who can reach this port can execute seeds\n"
            "      through your local agent runtime."
        )
    if not allow_remote:
        problems.append(
            "  --allow-remote\n      Confirms you intend to expose this server beyond this machine."
        )
    if is_wildcard_host(host) and not allowed_hosts:
        problems.append(
            f"  --allowed-host <name:{port}>\n"
            f"      A {host} bind is reached under a name this process cannot see,\n"
            "      so the Host allowlist that blocks DNS rebinding must be given."
        )

    if not problems:
        return auth_token

    _stderr_console.print(
        f"[red]Refusing to serve {transport} on non-loopback host {host!r}.[/red]\n"
        "[red]Missing:[/red]\n" + "\n".join(problems)
    )
    _stderr_console.print(
        "\n[blue]To serve locally instead, drop --host (it defaults to localhost).[/blue]"
    )
    raise typer.Exit(code=1)


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
    auth_token: Annotated[
        str,
        typer.Option(
            "--auth-token",
            envvar="OUROBOROS_MCP_AUTH_TOKEN",
            help=(
                "Shared secret clients must present as 'Authorization: Bearer <token>'. "
                "Required for network transports on a non-loopback host. Prefer the "
                "OUROBOROS_MCP_AUTH_TOKEN environment variable: a token on the command "
                "line is visible to every process on this machine via 'ps'."
            ),
        ),
    ] = "",
    allow_remote: Annotated[
        bool,
        typer.Option(
            "--allow-remote",
            help=(
                "Acknowledge that a non-loopback bind exposes seed execution to "
                "everyone who can reach the port. Required alongside --auth-token "
                "to serve on a routable address."
            ),
        ),
    ] = False,
    allowed_host: Annotated[
        list[str] | None,
        typer.Option(
            "--allowed-host",
            help=(
                "Host header value clients will use, e.g. 'ouroboros.internal:8080'. "
                "Repeatable. Required for wildcard binds (--host 0.0.0.0), whose "
                "reachable name cannot be inferred. A ':*' suffix allows any port."
            ),
        ),
    ] = None,
    allowed_origin: Annotated[
        list[str] | None,
        typer.Option(
            "--allowed-origin",
            help=(
                "Origin header value to permit. Repeatable. Empty by default, which "
                "rejects every browser-originated request."
            ),
        ),
    ] = None,
    workspace_root: Annotated[
        list[str] | None,
        typer.Option(
            "--workspace-root",
            help=(
                "Restrict seed execution to directories under this path. Repeatable. "
                "Strongly recommended for network binds; unset means any existing "
                "directory on this machine may be used as a working directory."
            ),
        ),
    ] = None,
    db: Annotated[
        str,
        typer.Option(
            "--db",
            help=(
                "Override the shared EventStore path "
                "(default: persistence.database_path with legacy fallback)."
            ),
        ),
    ] = "",
    runtime: Annotated[
        AgentRuntimeBackend | None,
        typer.Option(
            "--runtime",
            help=(
                "Agent runtime backend for orchestrator-driven tools (claude, claude-sdk, "
                "claude-cli, codex, "
                "opencode, hermes, gemini, copilot, goose, kiro, pi, gjc, "
                "antigravity, grok, zcode, or host)."
            ),
            case_sensitive=False,
        ),
    ] = None,
    llm_backend: Annotated[
        LLMBackend | None,
        typer.Option(
            "--llm-backend",
            help=(
                "LLM backend for interview/seed/evaluation tools (claude_code, "
                "litellm, codex, copilot, opencode, gemini, goose, kiro, pi, zcode, or dsh)."
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
        ouroboros mcp serve --runtime claude-cli

        # Start with SSE transport on custom port
        ouroboros mcp serve --runtime claude-cli --transport sse --port 9000

        # Start with streamable HTTP transport for Codex CLI --url clients
        ouroboros mcp serve --runtime claude-cli --transport streamable-http --port 9000

        # Start with OpenCode runtime
        ouroboros mcp serve --runtime opencode

        # Use Codex CLI for LLM-only tools as well
        ouroboros mcp serve --runtime codex --llm-backend codex

    """
    # Reject recursive server launches before shell hydration or any persistent
    # cache/environment mutation. The parent runtime already owns the MCP edge.
    if os.environ.get("_OUROBOROS_NESTED"):
        _stderr_console.print("[dim]Nested ouroboros MCP server detected — exiting cleanly[/dim]")
        raise typer.Exit(0)
    # Detached MCP hosts often inherit a minimal environment. Hydrate before
    # resolving selector provenance so login-shell/cache OUROBOROS_* choices
    # remain authoritative rather than being mistaken for the shipped default.
    _ensure_shell_env()
    # Resolve the exact backend the composition root would use before touching
    # nested-process state, shell state, persistence, or runtime adapters. A
    # missing option inherits config and ultimately defaults to the SDK-backed
    # ``claude`` runtime, which is not executable inside this MCP 2 process.
    selected_runtime = _effective_mcp_server_runtime(runtime)
    # Two different failures used to share one string. An environment that mixes
    # MCP 2 with the Claude SDK really is a package-profile problem and the user
    # must reinstall. Inheriting the ``claude`` default is a runtime-selection
    # failure, and the fix is ``--runtime``. Telling that user to reinstall sent
    # them to change extras that were never relevant to the selected backend.
    if has_unsupported_claude_sdk_mcp_mix():
        _stderr_console.print(Text(UNSUPPORTED_CLAUDE_SDK_MCP_MESSAGE, style="red"))
        raise typer.Exit(1)
    if selected_runtime == "claude":
        standin = _sdk_runtime_standin(runtime)
        if standin is None:
            _stderr_console.print(Text(SDK_RUNTIME_IN_MCP_SERVER_MESSAGE, style="red"))
            raise typer.Exit(1)
        # Say which runtime is actually serving. A host that swallows stderr
        # would otherwise show a working tool list backed by a runtime the user
        # never picked.
        standin_backend, standin_name = standin
        _stderr_console.print(
            Text(
                "Inherited the 'claude' SDK runtime, which cannot run inside the MCP "
                f"server; serving with '{standin_name}' instead. Pass --runtime, set "
                "OUROBOROS_AGENT_RUNTIME, or set OUROBOROS_RUNTIME to choose.",
                style="yellow",
            )
        )
        selected_runtime = standin_backend

    os.environ["_OUROBOROS_NESTED"] = "1"

    # Transport is re-validated inside _run_mcp_server; normalize here first so
    # the exposure check below classifies 'SSE' the same way serving will.
    from ouroboros.mcp.server.adapter import validate_transport

    try:
        normalized_transport = validate_transport(transport)
    except ValueError:
        _stderr_console.print(
            "[red]Invalid transport "
            f"{transport!r}. Must be 'stdio', 'sse', or 'streamable-http'.[/red]"
        )
        raise typer.Exit(code=1) from None

    allowed_hosts = tuple(allowed_host or ())
    allowed_origins = tuple(allowed_origin or ())
    workspace_roots = tuple(workspace_root or ())
    enforced_token = _resolve_network_security(
        transport=normalized_transport,
        host=host,
        port=port,
        auth_token=auth_token,
        allow_remote=allow_remote,
        allowed_hosts=allowed_hosts,
    )

    try:
        db_path = db if db else None
        asyncio.run(
            _run_mcp_server(
                host,
                port,
                normalized_transport,
                db_path,
                selected_runtime,
                llm_backend.value if llm_backend else None,
                auth_token=enforced_token,
                allowed_hosts=allowed_hosts,
                allowed_origins=allowed_origins,
                workspace_roots=workspace_roots,
            )
        )
    except KeyboardInterrupt:
        _stderr_console.print("[blue]MCP Server stopped[/blue]")
    except ImportError as e:
        _stderr_console.print(Text(f"MCP dependencies not installed: {e}", style="red"))
        _stderr_console.print(
            "[blue]Run MCP 2 in an isolated profile:\n"
            "  uvx --isolated --python '>=3.12' --from 'ouroboros-ai\\[mcp]' "
            "ouroboros mcp serve "
            "--runtime claude-cli\n"
            "or:\n"
            "  pipx run --spec 'ouroboros-ai\\[mcp]' ouroboros mcp serve "
            "--runtime claude-cli\n"
            "Do not combine it with the MCP 1.x-based \\[claude] or "
            "\\[claude-sdk] extras; use \\[claude-cli] for the CLI path.[/blue]"
        )
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
            help=(
                "Agent runtime backend for orchestrator-driven tools (claude, claude-sdk, "
                "claude-cli, codex, "
                "opencode, hermes, gemini, copilot, goose, kiro, pi, gjc, "
                "antigravity, grok, zcode, or host)."
            ),
            case_sensitive=False,
        ),
    ] = None,
    llm_backend: Annotated[
        LLMBackend | None,
        typer.Option(
            "--llm-backend",
            help=(
                "LLM backend for interview/seed/evaluation tools (claude_code, "
                "litellm, codex, copilot, opencode, gemini, goose, kiro, pi, zcode, or dsh)."
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
        runtime_backend=public_runtime_backend(runtime.value if runtime else None),
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
