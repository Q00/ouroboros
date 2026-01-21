"""Integration tests for entry point execution."""

from contextlib import suppress
from io import StringIO
import runpy
import sys
from unittest.mock import patch


def test_main_module_entry_point():
    """Test that the __main__ module entry point executes correctly.

    With Typer CLI, the entry point shows the help message when no args provided.
    The help message includes the app description.
    """
    # Mock sys.argv to simulate running as a module without args
    original_argv = sys.argv
    sys.argv = ["ouroboros"]

    # Mock stdout to capture output
    with patch("sys.stdout", new=StringIO()) as fake_out, suppress(SystemExit):
        # Run the __main__ module
        runpy.run_module("ouroboros", run_name="__main__", alter_sys=True)

    output = fake_out.getvalue()
    # With Typer CLI, the help shows the app description
    assert "Ouroboros" in output or "Usage" in output

    # Restore original argv
    sys.argv = original_argv
