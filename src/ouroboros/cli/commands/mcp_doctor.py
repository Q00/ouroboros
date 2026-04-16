"""MCP doctor subcommand — fast, read-only environment diagnostics.

Run ``ouroboros mcp doctor`` to check whether your environment is set up
correctly for the MCP server.  Each check returns a :class:`CheckResult`
with a pass/warn/fail status and an optional remediation hint.

The ``--json`` flag emits a machine-readable JSON array suitable for
inclusion in bug reports or CI pipelines.  Exit code 1 is returned if any
check has status ``fail``; 0 otherwise.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys
from typing import Annotated, Literal

from rich.console import Console
import typer

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
_EVENT_STORE_PATH = Path.home() / ".ouroboros" / "ouroboros.db"
_EVENT_STORE_WARN_BYTES = 500 * 1024 * 1024  # 500 MB


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
            remediation="pip install ouroboros-ai  or  uv add ouroboros-ai",
        )


def check_mcp_import() -> CheckResult:
    """Check that the ``mcp`` extra is installed."""
    try:
        import mcp  # noqa: F401
    except ImportError:
        return CheckResult(
            name="mcp_import",
            status="fail",
            message="mcp package not importable",
            remediation="pip install 'ouroboros-ai[mcp]'  or  uv add 'ouroboros-ai[mcp]'",
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
    return CheckResult(
        name="mcp_import",
        status="pass",
        message=f"mcp {version}",
    )


_CLAUDE_RUNTIME_BACKENDS = frozenset({"claude", "claude_code"})


def _get_runtime_backend() -> str:
    """Return the configured agent runtime backend, with a safe fallback."""
    try:
        from ouroboros.config.loader import get_agent_runtime_backend

        return get_agent_runtime_backend()
    except Exception:
        return "claude"


def check_claude_agent_sdk_import() -> CheckResult:
    """Check that the ``claude`` extra (claude-agent-sdk) is installed.

    The check is backend-aware: when the configured runtime is *not*
    Claude-based (e.g. ``codex`` or ``opencode``), a missing
    ``claude-agent-sdk`` is downgraded to **warn** instead of **fail**
    because the package is not required for that backend.
    """
    runtime = _get_runtime_backend()
    needs_claude = runtime in _CLAUDE_RUNTIME_BACKENDS

    try:
        import claude_agent_sdk  # noqa: F401

        try:
            version = importlib.metadata.version("claude-agent-sdk")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
        return CheckResult(
            name="claude_agent_sdk_import",
            status="pass",
            message=f"claude-agent-sdk {version}",
        )
    except ImportError:
        if needs_claude:
            return CheckResult(
                name="claude_agent_sdk_import",
                status="fail",
                message="claude-agent-sdk not importable",
                remediation="pip install 'ouroboros-ai[claude]'  or  uv add 'ouroboros-ai[claude]'",
            )
        return CheckResult(
            name="claude_agent_sdk_import",
            status="warn",
            message=f"claude-agent-sdk not installed (not required for {runtime} runtime)",
            remediation="Install if switching to Claude runtime: pip install 'ouroboros-ai[claude]'",
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
        return CheckResult(
            name="litellm_import",
            status="warn",
            message="litellm not installed (optional)",
            remediation="pip install 'ouroboros-ai[litellm]'  or  uv add 'ouroboros-ai[litellm]'",
        )


def check_event_store() -> CheckResult:
    """Check EventStore path existence and warn if it exceeds 500 MB."""
    if not _EVENT_STORE_PATH.exists():
        return CheckResult(
            name="event_store",
            status="pass",
            message=f"{_EVENT_STORE_PATH} not found (will be created on first use)",
        )
    try:
        size_bytes = _EVENT_STORE_PATH.stat().st_size
    except OSError as exc:
        return CheckResult(
            name="event_store",
            status="warn",
            message=f"Cannot stat {_EVENT_STORE_PATH}: {exc}",
        )

    size_mb = size_bytes / (1024 * 1024)
    if size_bytes > _EVENT_STORE_WARN_BYTES:
        return CheckResult(
            name="event_store",
            status="warn",
            message=f"{_EVENT_STORE_PATH} is {size_mb:.1f} MB (>500 MB)",
            remediation=(
                "Consider archiving or pruning old sessions. "
                "The DB can be vacuumed with: sqlite3 ~/.ouroboros/ouroboros.db VACUUM;"
            ),
        )
    return CheckResult(
        name="event_store",
        status="pass",
        message=f"{_EVENT_STORE_PATH} ({size_mb:.1f} MB)",
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


def check_pid_file() -> CheckResult:
    """Check the MCP server PID file for liveness."""
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
    ) -> None:
        """Run environment diagnostics for the MCP server.

        Checks Python version, installed extras (mcp, claude-agent-sdk,
        litellm), EventStore health, and PID file liveness.  Backend-specific
        extras are validated against the configured runtime so that non-Claude
        setups (codex, opencode) do not produce false failures.  Exit code 1
        if any check fails.

        Examples:

            # Human-readable output
            ouroboros mcp doctor

            # Machine-readable (for bug reports)
            ouroboros mcp doctor --json
        """
        console = Console()
        results: list[CheckResult] = [fn() for fn in _ALL_CHECKS]

        if as_json:
            payload = [asdict(r) for r in results]
            print(json.dumps(payload, indent=2))
        else:
            console.print()
            console.print("[bold]Ouroboros MCP Doctor[/bold]")
            console.print()
            for result in results:
                symbol = _SYMBOLS[result.status]
                console.print(f"  {symbol}  [bold]{result.name}[/bold]: {result.message}")
                if result.remediation:
                    console.print(f"      [dim]hint: {result.remediation}[/dim]")
            console.print()

        has_failure = any(r.status == "fail" for r in results)
        if has_failure:
            raise typer.Exit(code=1)


__all__ = [
    "CheckResult",
    "Status",
    "_CLAUDE_RUNTIME_BACKENDS",
    "check_python_version",
    "check_platform",
    "check_ouroboros_version",
    "check_mcp_import",
    "check_claude_agent_sdk_import",
    "check_litellm_import",
    "check_event_store",
    "check_pid_file",
    "register_doctor_command",
]
