"""MCP doctor subcommand — fast, read-only environment diagnostics.

Run ``ouroboros mcp doctor`` to check whether your environment is set up
correctly for the MCP server.  Each check returns a :class:`CheckResult`
with a pass/warn/fail status and an optional remediation hint.

The ``--json`` flag emits a machine-readable JSON array suitable for
inclusion in bug reports or CI pipelines.  Exit code 1 is returned if any
check has status ``fail``; 0 otherwise.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shlex
import sys
import tempfile
from typing import Annotated, Literal

from rich.console import Console
from rich.markup import escape
import typer

from ouroboros.config.models import resolve_event_store_path
from ouroboros.mcp.client.adapter import MCPClientAdapter
from ouroboros.mcp.types import MCPServerConfig, TransportType
from ouroboros.package_profiles import (
    UNSUPPORTED_CLAUDE_SDK_MCP_MESSAGE,
    has_unsupported_claude_sdk_mcp_mix,
)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

Status = Literal["pass", "warn", "fail"]

_SYMBOLS: dict[Status, str] = {
    "pass": "[green]✓[/green]",
    "warn": "[yellow]⚠[/yellow]",
    "fail": "[red]✗[/red]",
}


@dataclass
class CheckResult:
    """Result of a single diagnostic check."""

    name: str
    status: Status
    message: str
    remediation: str = ""


# ---------------------------------------------------------------------------
# Individual check functions
# ---------------------------------------------------------------------------

_PID_FILE = Path.home() / ".ouroboros" / "mcp-server.pid"
_PID_REGISTRY_DIR = Path.home() / ".ouroboros" / "mcp-servers"
_EVENT_STORE_PATH: Path | None = None
_EVENT_STORE_WARN_BYTES = 500 * 1024 * 1024  # 500 MB

_LOCAL_STDIO_STAGE_STARTUP = "local_stdio_startup_transport"
_LOCAL_STDIO_STAGE_PROTOCOL = "local_stdio_protocol_discovery"
_LOCAL_STDIO_STAGE_TOOLS = "local_stdio_tool_recognition"


def _local_stdio_probe_environment(home: Path) -> dict[str, str]:
    """Return a minimal child environment isolated from user configuration."""
    passthrough = (
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    )
    environment = {key: os.environ[key] for key in passthrough if key in os.environ}
    environment.update(
        {
            "DO_NOT_TRACK": "1",
            "HOME": str(home),
            "OUROBOROS_LOG_LEVEL": "ERROR",
            "OUROBOROS_TELEMETRY": "0",
            "USERPROFILE": str(home),
            "APPDATA": str(home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(home / "AppData" / "Local"),
            "HOMEDRIVE": home.drive,
            "HOMEPATH": str(home)[len(home.drive) :],
            "XDG_CONFIG_HOME": str(home / ".config"),
            "TEMP": str(home),
            "TMP": str(home),
            "TMPDIR": str(home),
            "OUROBOROS_AGENT_RUNTIME": "host",
            "OUROBOROS_RUNTIME": "host",
            # Prevent mcp serve's detached-host hydration from sourcing a real
            # user shell profile and reintroducing configured bridges or keys.
            "SHELL": (
                os.environ.get("COMSPEC", "cmd.exe")
                if sys.platform == "win32"
                else "/usr/bin/false"
            ),
        }
    )
    return environment


def _expected_ouroboros_tool_names() -> frozenset[str]:
    """Return the canonical built-in tool surface used by server composition."""
    from ouroboros.mcp.tool_manifest import BUILTIN_TOOL_NAMES

    return BUILTIN_TOOL_NAMES


def _not_run_result(name: str, blocked_by: str) -> CheckResult:
    return CheckResult(
        name=name,
        status="fail",
        message=f"Not run because {blocked_by} failed",
        remediation="Resolve the preceding local stdio probe stage, then rerun the doctor.",
    )


def _local_stdio_probe_config(home: Path) -> MCPServerConfig:
    # -I ignores caller PYTHONPATH and cwd imports. Only this already-imported
    # installation is added, and cwd changes before importing its entrypoint.
    source_root = str(Path(__file__).resolve().parents[3])
    bootstrap = (
        "import os,sys; "
        f"os.chdir({str(home)!r}); "
        f"sys.path.insert(0, {source_root!r}); "
        "from ouroboros.cli.commands.mcp_doctor import _serve_local_stdio_probe; "
        "import asyncio; asyncio.run(_serve_local_stdio_probe())"
    )
    return MCPServerConfig(
        name="ouroboros-doctor-local",
        transport=TransportType.STDIO,
        command=sys.executable,
        args=("-I", "-B", "-c", bootstrap),
        env=_local_stdio_probe_environment(home),
        timeout=15.0,
    )


async def _serve_local_stdio_probe() -> None:
    """Serve the production composition offline, only inside the probe child.

    Bypass CLI dotenv/login-shell hydration, update refresh, and PID registry.
    All handlers still come from the ordinary production composition root.
    """

    def block_network(event: str, _args: tuple[object, ...]) -> None:
        if event.startswith("socket.") or event in {
            "subprocess.Popen",
            "os.system",
            "os.exec",
            "os.posix_spawn",
        }:
            raise RuntimeError(
                "Network and command execution are disabled for the local doctor probe"
            )

    sys.addaudithook(block_network)
    from ouroboros.mcp.server.adapter import create_ouroboros_server
    from ouroboros.persistence.event_store import EventStore, sqlite_database_url

    home = Path.home()
    store = EventStore(sqlite_database_url(home / "probe.db"))
    server = None
    try:
        await store.initialize()
        server = create_ouroboros_server(
            event_store=store, project_dir=home, state_dir=home, runtime_backend="host"
        )
        await server.serve(transport="stdio")
    finally:
        try:
            if server is not None:
                await server.shutdown()
        finally:
            await store.close()


async def _collect_local_stdio_results(
    adapter: MCPClientAdapter, config: MCPServerConfig
) -> list[CheckResult]:
    connected = await adapter.connect(config)
    if connected.is_err:
        if adapter.transport_entered:
            return [
                CheckResult(_LOCAL_STDIO_STAGE_STARTUP, "pass", "Local stdio streams established"),
                CheckResult(
                    _LOCAL_STDIO_STAGE_PROTOCOL,
                    "fail",
                    f"MCP protocol discovery failed: {connected.error}",
                    "Check protocol compatibility in the isolated MCP 2 installation.",
                ),
                _not_run_result(_LOCAL_STDIO_STAGE_TOOLS, "protocol discovery"),
            ]
        return [
            CheckResult(
                _LOCAL_STDIO_STAGE_STARTUP,
                "fail",
                f"Local server startup or stdio transport failed: {connected.error}",
                "Resolve package checks, then rerun --probe-local-stdio.",
            ),
            _not_run_result(_LOCAL_STDIO_STAGE_PROTOCOL, "startup/transport"),
            _not_run_result(_LOCAL_STDIO_STAGE_TOOLS, "startup/transport"),
        ]
    results = [CheckResult(_LOCAL_STDIO_STAGE_STARTUP, "pass", "Local stdio streams established")]
    if adapter.server_snapshot is None or not adapter.protocol_version:
        results.extend(
            [
                CheckResult(
                    _LOCAL_STDIO_STAGE_PROTOCOL,
                    "fail",
                    "Connected, but MCP protocol discovery returned no snapshot",
                    "Check protocol compatibility in the isolated MCP 2 installation.",
                ),
                _not_run_result(_LOCAL_STDIO_STAGE_TOOLS, "protocol discovery"),
            ]
        )
        return results
    results.append(
        CheckResult(
            _LOCAL_STDIO_STAGE_PROTOCOL,
            "pass",
            f"Negotiated MCP protocol {adapter.protocol_version}",
        )
    )
    tools = await adapter.list_tools()
    if tools.is_err:
        results.append(
            CheckResult(
                _LOCAL_STDIO_STAGE_TOOLS,
                "fail",
                f"MCP tools/list failed: {tools.error}",
                "Inspect server stderr and reinstall the isolated MCP 2 profile.",
            )
        )
    else:
        names = frozenset(tool.name for tool in tools.value)
        missing = sorted(_expected_ouroboros_tool_names() - names)
        results.append(
            CheckResult(
                _LOCAL_STDIO_STAGE_TOOLS,
                "fail" if missing else "pass",
                "Missing expected Ouroboros tools: " + ", ".join(missing)
                if missing
                else f"Recognized {len(names)} Ouroboros tools (none called)",
                "Reinstall matching client and server versions." if missing else "",
            )
        )
    return results


async def _probe_local_stdio() -> list[CheckResult]:
    """Own the isolated offline probe lifecycle and observe teardown results."""
    with tempfile.TemporaryDirectory(prefix="ouroboros-doctor-") as temporary_home:
        config = _local_stdio_probe_config(Path(temporary_home))
        adapter = MCPClientAdapter(max_retries=1)
        try:
            results = await _collect_local_stdio_results(adapter, config)
        except Exception as exc:
            results = [
                CheckResult(
                    _LOCAL_STDIO_STAGE_STARTUP,
                    "fail",
                    f"Local probe failed: {exc}",
                    "Inspect the isolated MCP installation and rerun the doctor.",
                ),
                _not_run_result(_LOCAL_STDIO_STAGE_PROTOCOL, "local probe"),
                _not_run_result(_LOCAL_STDIO_STAGE_TOOLS, "local probe"),
            ]
        finally:
            # Explicit Result inspection also catches a failed cleanup after
            # partial connect: no passing report escapes before child teardown.
            cleanup = await adapter.disconnect()
        if cleanup.is_err:
            for result in results:
                result.status = "fail"
                result.remediation = "Resolve local stdio teardown before rerunning the probe."
            results[0].message = f"Local stdio teardown failed: {cleanup.error}"
        return results


def check_python_version() -> CheckResult:
    """Require Python >= 3.12."""
    major, minor, micro = sys.version_info[0], sys.version_info[1], sys.version_info[2]
    version_str = f"{major}.{minor}.{micro}"
    if (major, minor) >= (3, 12):
        return CheckResult(
            name="python_version",
            status="pass",
            message=f"Python {version_str}",
        )
    return CheckResult(
        name="python_version",
        status="fail",
        message=f"Python {version_str} (need >= 3.12)",
        remediation="Upgrade to Python 3.12 or newer: https://www.python.org/downloads/",
    )


def check_platform() -> CheckResult:
    """Report platform/OS information (always passes)."""
    info = f"{platform.system()} {platform.release()} ({platform.machine()})"
    return CheckResult(
        name="platform",
        status="pass",
        message=info,
    )


def check_ouroboros_version() -> CheckResult:
    """Check that ouroboros-ai is installed and report its version."""
    try:
        version = importlib.metadata.version("ouroboros-ai")
        return CheckResult(
            name="ouroboros_version",
            status="pass",
            message=f"ouroboros-ai {version}",
        )
    except importlib.metadata.PackageNotFoundError:
        return CheckResult(
            name="ouroboros_version",
            status="fail",
            message="ouroboros-ai not found in installed packages",
            remediation="pip install ouroboros-ai  or  uv tool install ouroboros-ai",
        )


def check_mcp_import() -> CheckResult:
    """Check that the isolated modern ``mcp`` extra is installed."""
    try:
        import mcp  # noqa: F401
    except ImportError:
        return CheckResult(
            name="mcp_import",
            status="fail",
            message="mcp package not importable",
            remediation=(
                "Run the server in its isolated profile: "
                "uvx --isolated --python '>=3.12' --from 'ouroboros-ai[mcp]' "
                "ouroboros mcp serve"
            ),
        )

    try:
        version = importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        # importable but no dist-info — treat as pass
        return CheckResult(
            name="mcp_import",
            status="pass",
            message="mcp (version unknown)",
        )
    try:
        major = int(version.split(".", maxsplit=1)[0])
    except ValueError:
        major = 0
    if major != 2:
        return CheckResult(
            name="mcp_import",
            status="fail",
            message=f"mcp {version} is not the required MCP 2 runtime",
            remediation=(
                "Do not add MCP 2 to the Claude SDK environment. Launch the "
                "separate server process with: uvx --isolated --python '>=3.12' --from "
                "'ouroboros-ai[mcp]' ouroboros mcp serve"
            ),
        )
    return CheckResult(
        name="mcp_import",
        status="pass",
        message=f"mcp {version}",
    )


_CLAUDE_SDK_RUNTIME_BACKENDS = frozenset({"claude", "claude_code"})
_GOOSE_RUNTIME_BACKENDS = frozenset({"goose", "goose_cli"})


def _get_runtime_backend() -> str:
    """Return the configured agent runtime backend, with a safe fallback."""
    try:
        from ouroboros.config.loader import get_agent_runtime_backend

        return get_agent_runtime_backend()
    except Exception:
        return "claude"


def _get_llm_backend() -> str:
    """Return the configured LLM backend, with a safe fallback."""
    try:
        from ouroboros.config.loader import get_llm_backend

        return get_llm_backend()
    except Exception:
        return "claude_code"


def check_claude_agent_sdk_import() -> CheckResult:
    """Diagnose the default Claude SDK/MCP 1 profile."""
    runtime = _get_runtime_backend()
    needs_sdk = runtime in _CLAUDE_SDK_RUNTIME_BACKENDS

    if has_unsupported_claude_sdk_mcp_mix():
        return CheckResult(
            name="claude_agent_sdk_import",
            status="fail",
            message=UNSUPPORTED_CLAUDE_SDK_MCP_MESSAGE,
            remediation=(
                "Recreate this environment with 'ouroboros-ai[claude]' for the SDK/MCP 1 "
                "runtime, or use 'ouroboros-ai[mcp,claude-cli]' for the MCP 2 CLI worker."
            ),
        )

    try:
        import claude_agent_sdk  # noqa: F401

        try:
            version = importlib.metadata.version("claude-agent-sdk")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
        return CheckResult(
            name="claude_agent_sdk_import",
            status="pass",
            message=f"claude-agent-sdk {version} (isolated [claude]/[claude-sdk] profile)",
        )
    except ImportError:
        if needs_sdk:
            return CheckResult(
                name="claude_agent_sdk_import",
                status="fail",
                message="Claude SDK runtime is configured but [claude] is not installed",
                remediation=(
                    "Install 'ouroboros-ai[claude]' alone for SDK/MCP 1, or use "
                    "`ouroboros setup --runtime claude-cli` from an MCP 2 environment. "
                    "Never combine [claude] or [claude-sdk] with [mcp]."
                ),
            )
        return CheckResult(
            name="claude_agent_sdk_import",
            status="pass",
            message=f"claude-agent-sdk not installed (expected for {runtime} CLI/MCP profile)",
            remediation="Use 'ouroboros-ai[claude]' only in a separate MCP 1 SDK environment.",
        )


def check_litellm_import() -> CheckResult:
    """Check that litellm is installed (warn if missing, not fail)."""
    try:
        import litellm  # noqa: F401

        try:
            version = importlib.metadata.version("litellm")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
        return CheckResult(
            name="litellm_import",
            status="pass",
            message=f"litellm {version}",
        )
    except ImportError:
        if sys.version_info >= (3, 14):
            from ouroboros.providers.factory import litellm_missing_dependency_message

            remediation = (
                litellm_missing_dependency_message("LiteLLM is optional but not installed.")
                + " For uv tool: uv tool install --python 3.13 --force "
                "'ouroboros-ai[litellm]'."
            )
        else:
            remediation = (
                "pip install 'ouroboros-ai[litellm]'  or  uv tool install 'ouroboros-ai[litellm]'"
            )

        return CheckResult(
            name="litellm_import",
            status="warn",
            message="litellm not installed (optional)",
            remediation=remediation,
        )


_CODEX_BACKENDS = frozenset({"codex", "codex_cli"})


def _codex_home_from_env() -> Path:
    """Return the Codex home directory implied by the current environment."""
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if codex_home:
        return Path(codex_home).expanduser()
    return Path.home() / ".codex"


def _codex_backend_active() -> bool:
    """Return whether any configured Ouroboros backend relies on Codex auth."""
    return _get_runtime_backend() in _CODEX_BACKENDS or _get_llm_backend() in _CODEX_BACKENDS


def check_codex_oauth_auth() -> CheckResult:
    """Check Codex OAuth files when Codex is the selected runtime/LLM backend.

    ``ouroboros_interview`` and ``ouroboros_auto`` can launch nested ``codex
    exec`` calls from MCP server processes.  In Hermes/Discord deployments, an
    opaque 401 from ``api.openai.com/v1/responses`` often means the nested
    process did not see the expected Codex OAuth home, not that the user should
    blindly add an OpenAI API key.
    """
    codex_home = _codex_home_from_env()
    auth_json = codex_home / "auth.json"
    config_toml = codex_home / "config.toml"
    openai_key_present = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    codex_active = _codex_backend_active()

    if auth_json.exists():
        config_note = "config.toml found" if config_toml.exists() else "config.toml missing"
        key_note = "OPENAI_API_KEY present" if openai_key_present else "OPENAI_API_KEY not required"
        return CheckResult(
            name="codex_oauth_auth",
            status="pass",
            message=f"{auth_json} found ({config_note}; {key_note})",
        )

    if codex_active and openai_key_present:
        return CheckResult(
            name="codex_oauth_auth",
            status="pass",
            message=(
                f"{auth_json} not found, but OPENAI_API_KEY is present for an "
                "API-key-backed Codex profile"
            ),
            remediation=(
                "If this deployment is intended to use Codex OAuth instead, run `codex login` "
                "for the same user/environment or set CODEX_HOME/HOME so nested MCP/Codex "
                "processes can read the existing auth.json."
            ),
        )

    if codex_active:
        return CheckResult(
            name="codex_oauth_auth",
            status="fail",
            message=f"Codex backend active but {auth_json} not found",
            remediation=(
                "Run `codex login` for the same user/environment, or set CODEX_HOME/HOME "
                "so nested MCP/Codex processes can read the existing Codex OAuth auth.json. "
                "Do not add OPENAI_API_KEY unless you intentionally use an API-key Codex profile."
            ),
        )

    return CheckResult(
        name="codex_oauth_auth",
        status="warn",
        message=f"{auth_json} not found (Codex backend not active)",
        remediation="Required only when runtime_backend or llm.backend is codex.",
    )


def check_event_store() -> CheckResult:
    """Check EventStore path existence and warn if it exceeds 500 MB."""
    try:
        event_store_path = _EVENT_STORE_PATH or resolve_event_store_path()
    except ValueError:
        return CheckResult(
            name="event_store",
            status="fail",
            message="Invalid EventStore configuration.",
            remediation="Fix persistence.database_path in the Ouroboros configuration.",
        )
    if not event_store_path.exists():
        return CheckResult(
            name="event_store",
            status="pass",
            message=f"{event_store_path} not found (will be created on first use)",
        )
    try:
        size_bytes = event_store_path.stat().st_size
    except OSError as exc:
        return CheckResult(
            name="event_store",
            status="warn",
            message=f"Cannot stat {event_store_path}: {exc}",
        )

    size_mb = size_bytes / (1024 * 1024)
    if size_bytes > _EVENT_STORE_WARN_BYTES:
        return CheckResult(
            name="event_store",
            status="warn",
            message=f"{event_store_path} is {size_mb:.1f} MB (>500 MB)",
            remediation=(
                "Consider archiving or pruning old sessions. "
                f"The DB can be vacuumed with: sqlite3 {shlex.quote(str(event_store_path))} VACUUM;"
            ),
        )
    return CheckResult(
        name="event_store",
        status="pass",
        message=f"{event_store_path} ({size_mb:.1f} MB)",
    )


def _pid_is_alive(pid: int) -> bool:
    """Return True if *pid* refers to a running process.

    Handles Windows (where ``os.kill(pid, 0)`` raises ``OSError`` with
    ``WinError 87`` instead of ``ProcessLookupError``) and POSIX.
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we don't have permission to signal it.
        return True
    except OSError:
        # Windows: signal 0 unsupported — fall back to a tasklist check.
        if sys.platform == "win32":
            try:
                import subprocess

                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                return str(pid) in result.stdout
            except Exception:
                return False
        # Non-Windows, unknown OSError — assume stale.
        return False


def _scan_instance_registry() -> tuple[list[int], list[Path]]:
    """Split per-instance registry records into live PIDs and stale files.

    Records live at ``~/.ouroboros/mcp-servers/<pid>.pid`` containing
    ``"<pid> <start_time|None>"`` — one per concurrently running server
    (one server per connected MCP client is the normal steady state).
    """
    live: list[int] = []
    stale: list[Path] = []
    try:
        entries = sorted(_PID_REGISTRY_DIR.iterdir())
    except OSError:
        return live, stale
    for entry in entries:
        try:
            pid = int(entry.read_text(encoding="utf-8").strip().split()[0])
        except (ValueError, IndexError, OSError):
            stale.append(entry)
            continue
        if _pid_is_alive(pid):
            live.append(pid)
        else:
            stale.append(entry)
    return live, stale


def _check_legacy_pid_file() -> CheckResult:
    """Check the legacy single-slot PID file (pre-registry server versions)."""
    if not _PID_FILE.exists():
        return CheckResult(
            name="pid_file",
            status="pass",
            message="No PID file (server not running or cleanly stopped)",
        )

    _rm_cmd = "del" if platform.system() == "Windows" else "rm"

    try:
        raw = _PID_FILE.read_text(encoding="utf-8").strip()
        pid = int(raw)
    except (ValueError, OSError) as exc:
        return CheckResult(
            name="pid_file",
            status="warn",
            message=f"PID file unreadable: {exc}",
            remediation=f"Remove the stale file: {_rm_cmd} {_PID_FILE}",
        )

    if _pid_is_alive(pid):
        return CheckResult(
            name="pid_file",
            status="pass",
            message=f"MCP server running (PID {pid})",
        )
    return CheckResult(
        name="pid_file",
        status="warn",
        message=f"Stale PID file: process {pid} is not running",
        remediation=f"Remove the stale file: {_rm_cmd} {_PID_FILE}",
    )


def check_pid_file() -> CheckResult:
    """Check MCP server instance liveness (per-instance registry + legacy file)."""
    live, stale = _scan_instance_registry()
    if not live and not stale:
        return _check_legacy_pid_file()

    parts: list[str] = []
    if live:
        parts.append(
            f"{len(live)} MCP server instance(s) running "
            f"(PIDs {', '.join(str(pid) for pid in live)})"
        )
    if stale:
        parts.append(f"{len(stale)} stale instance record(s)")
        return CheckResult(
            name="pid_file",
            status="warn",
            message="; ".join(parts),
            remediation=(
                "Stale records are swept automatically by the next "
                "`ouroboros mcp serve`; live instances are owned by running "
                "agent sessions — do not kill them blindly."
            ),
        )
    return CheckResult(
        name="pid_file",
        status="pass",
        message=parts[0],
    )


# ---------------------------------------------------------------------------
# Ordered list of all checks
# ---------------------------------------------------------------------------

_ALL_CHECKS = [
    check_python_version,
    check_platform,
    check_ouroboros_version,
    check_mcp_import,
    check_claude_agent_sdk_import,
    check_litellm_import,
    check_codex_oauth_auth,
    check_event_store,
    check_pid_file,
]


# ---------------------------------------------------------------------------
# Typer command
# ---------------------------------------------------------------------------


def register_doctor_command(app: typer.Typer) -> None:
    """Register the ``doctor`` subcommand onto *app* (the ``mcp`` Typer app)."""

    @app.command()
    def doctor(
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Emit machine-readable JSON to stdout."),
        ] = False,
        probe_local_stdio: Annotated[
            bool,
            typer.Option(
                "--probe-local-stdio",
                help="Start this Ouroboros installation and probe MCP discovery over stdio.",
            ),
        ] = False,
    ) -> None:
        """Run environment diagnostics for the MCP server.

        Checks Python version, the isolated MCP 2 profile, backend-specific
        dependencies, Codex OAuth readiness, EventStore health, and PID file
        liveness. Claude Agent SDK and MCP 2 are intentionally diagnosed as
        separate process profiles. Exit code 1 if any check fails.

        Examples:

            # Human-readable output
            ouroboros mcp doctor

            # Machine-readable (for bug reports)
            ouroboros mcp doctor --json

            # Opt-in local server startup and discovery probe
            ouroboros mcp doctor --probe-local-stdio
        """
        console = Console()
        results: list[CheckResult] = [fn() for fn in _ALL_CHECKS]
        if probe_local_stdio:
            results.extend(asyncio.run(_probe_local_stdio()))

        if as_json:
            payload = [asdict(r) for r in results]
            print(json.dumps(payload, indent=2))
        else:
            console.print()
            console.print("[bold]Ouroboros MCP Doctor[/bold]")
            console.print()
            for result in results:
                symbol = _SYMBOLS[result.status]
                console.print(
                    f"  {symbol}  [bold]{escape(result.name)}[/bold]: {escape(result.message)}"
                )
                if result.remediation:
                    console.print(f"      [dim]hint: {escape(result.remediation)}[/dim]")
            console.print()

        has_failure = any(r.status == "fail" for r in results)
        if has_failure:
            raise typer.Exit(code=1)


__all__ = [
    "CheckResult",
    "Status",
    "_CLAUDE_SDK_RUNTIME_BACKENDS",
    "_probe_local_stdio",
    "check_python_version",
    "check_platform",
    "check_ouroboros_version",
    "check_mcp_import",
    "check_claude_agent_sdk_import",
    "check_litellm_import",
    "check_codex_oauth_auth",
    "check_event_store",
    "check_pid_file",
    "register_doctor_command",
]
