"""Regression test: invalid transport errors go to stderr, not stdout.

In stdio mode stdout is the JSON-RPC channel.  If validation errors leak
to stdout they corrupt the protocol.  The fix in mcp.py routes all
human-readable output through ``_stderr_console`` (``Console(stderr=True)``).

These tests ensure the invariant holds so it cannot be accidentally broken.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ouroboros.mcp.server.adapter import validate_transport


# ---------------------------------------------------------------------------
# Unit: validate_transport rejects bad values and accepts good ones
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_transport", ["http", "ws", "grpc", "invalid", "", "BOGUS"])
def test_validate_transport_rejects_invalid(bad_transport: str) -> None:
    """validate_transport must raise ValueError for unknown transports."""
    with pytest.raises(ValueError, match="Invalid transport"):
        validate_transport(bad_transport)


@pytest.mark.parametrize("good_transport,expected", [("stdio", "stdio"), ("sse", "sse"), ("STDIO", "stdio"), ("SSE", "sse")])
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
    from ouroboros.cli.commands.mcp import _stderr_console

    assert _stderr_console.stderr is True, (
        "_stderr_console must be created with stderr=True to avoid "
        "corrupting the JSON-RPC channel on stdout"
    )


# ---------------------------------------------------------------------------
# Integration: subprocess test ensuring stdout stays clean
# ---------------------------------------------------------------------------

# Locate the ``src/`` directory for the current source tree so the subprocess
# picks up the same code the test suite is running against (important when
# the repo is checked out in a worktree separate from the editable install).
_SRC_DIR = str(Path(__file__).resolve().parents[3] / "src")


def test_invalid_transport_keeps_stdout_clean_subprocess() -> None:
    """stdout must stay empty when an invalid transport is passed.

    Uses a real subprocess so stdout and stderr are truly separate,
    unlike typer's CliRunner which mixes the streams.  This is the
    definitive regression test for JSON-RPC corruption prevention.
    """
    env = os.environ.copy()
    # Ensure the subprocess loads the source tree under test.
    env["PYTHONPATH"] = _SRC_DIR + os.pathsep + env.get("PYTHONPATH", "")

    result = subprocess.run(
        [
            sys.executable, "-c",
            "import sys; sys.argv = ['ouroboros', 'mcp', 'serve', '--transport', 'INVALID']; "
            "from ouroboros.cli.main import app; app()",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )

    # Command must exit with non-zero code
    assert result.returncode != 0, (
        f"Expected non-zero exit code for invalid transport, got {result.returncode}"
    )

    # stdout must be empty -- any bytes here would corrupt JSON-RPC in stdio mode
    assert result.stdout.strip() == "", (
        f"stdout must be empty to prevent JSON-RPC corruption but contained: "
        f"{result.stdout!r}"
    )

    # stderr must contain the diagnostic
    assert "Invalid transport" in result.stderr, (
        f"Expected 'Invalid transport' in stderr but got: {result.stderr!r}"
    )
