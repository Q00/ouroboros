"""Regression test: invalid transport errors go to stderr, not stdout.

In stdio mode stdout is the JSON-RPC channel.  If validation errors leak
to stdout they corrupt the protocol.  The fix in mcp.py routes all
human-readable output through ``_stderr_console`` (``Console(stderr=True)``).

These tests ensure the invariant holds so it cannot be accidentally broken.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
import typer

from ouroboros.cli.commands.mcp import _run_mcp_server, _stderr_console
from ouroboros.mcp.server.adapter import validate_transport

# ---------------------------------------------------------------------------
# Unit: validate_transport rejects bad values and accepts good ones
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_transport", ["http", "ws", "grpc", "invalid", "", "BOGUS"])
def test_validate_transport_rejects_invalid(bad_transport: str) -> None:
    """validate_transport must raise ValueError for unknown transports."""
    with pytest.raises(ValueError, match="Invalid transport"):
        validate_transport(bad_transport)


@pytest.mark.parametrize(
    "good_transport,expected",
    [("stdio", "stdio"), ("sse", "sse"), ("STDIO", "stdio"), ("SSE", "sse")],
)
def test_validate_transport_accepts_valid(good_transport: str, expected: str) -> None:
    """validate_transport must accept and lowercase known transports."""
    assert validate_transport(good_transport) == expected


# ---------------------------------------------------------------------------
# Configuration: _stderr_console must write to stderr, not stdout
# ---------------------------------------------------------------------------


def test_stderr_console_is_configured_for_stderr() -> None:
    """The module-level _stderr_console must write to stderr, not stdout.

    This is the critical invariant: in stdio mode, stdout is the JSON-RPC
    channel, so all human-readable diagnostics must go to stderr.
    """
    assert _stderr_console.stderr is True, (
        "_stderr_console must be created with stderr=True to avoid "
        "corrupting the JSON-RPC channel on stdout"
    )


# ---------------------------------------------------------------------------
# Integration: invalid transport writes diagnostic to stderr and exits non-zero
# ---------------------------------------------------------------------------


def test_invalid_transport_keeps_stdout_clean(capfd, monkeypatch) -> None:
    """stdout must stay empty when an invalid transport is passed.

    Uses pytest's ``capfd`` fixture to capture real file-descriptor output
    (what a subprocess or MCP client would actually see), without relying on
    an out-of-process test which is fragile in CI (env leakage, editable
    install path, shell profile loading, etc.).

    The definitive regression guard for JSON-RPC corruption prevention:
    in stdio mode, any byte on stdout corrupts the protocol, so invalid
    transport diagnostics must go to stderr only.
    """
    # Ensure the nested-guard sentinel is clear so we exercise the real
    # validate_transport path (not the early `typer.Exit(0)` shortcut).
    monkeypatch.delenv("_OUROBOROS_NESTED", raising=False)

    # Drain any pre-existing buffered output from prior test logging so the
    # capture below only reflects bytes emitted by _run_mcp_server.
    capfd.readouterr()

    # Patch _ensure_shell_env to a no-op: it can spawn a login shell which is
    # slow and irrelevant to this regression and may also emit diagnostics.
    with patch("ouroboros.cli.commands.mcp._ensure_shell_env", lambda **_: None):
        with pytest.raises(typer.Exit) as excinfo:
            asyncio.run(_run_mcp_server("localhost", 8080, "INVALID"))

    # The invalid-transport path must signal failure
    assert excinfo.value.exit_code == 1

    captured = capfd.readouterr()

    # stdout must be empty — any bytes here would corrupt JSON-RPC in stdio mode
    assert captured.out == "", (
        f"stdout must be empty to prevent JSON-RPC corruption but contained: {captured.out!r}"
    )

    # stderr must contain the diagnostic
    assert "Invalid transport" in captured.err, (
        f"Expected 'Invalid transport' in stderr but got: {captured.err!r}"
    )


def test_stderr_console_print_does_not_leak_to_stdout(capfd) -> None:
    """_stderr_console.print output must land on stderr, never stdout.

    Complements the integration test above: this directly exercises the
    console configuration so any regression in how ``_stderr_console`` is
    constructed will be caught even if the command-level flow changes.
    """
    capfd.readouterr()  # drain

    _stderr_console.print("[red]Invalid transport: http[/red]")

    captured = capfd.readouterr()
    assert captured.out == "", f"stdout must be clean but got: {captured.out!r}"
    assert "Invalid transport" in captured.err
