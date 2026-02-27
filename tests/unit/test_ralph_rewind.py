"""Unit tests for scripts/ralph-rewind.py — argument parsing."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Load ralph-rewind.py as a module without requiring its dependencies at import time.
_RALPH_REWIND_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ralph-rewind.py"
_spec = importlib.util.spec_from_file_location("ralph_rewind", _RALPH_REWIND_PATH)
assert _spec and _spec.loader
_ralph_rewind = importlib.util.module_from_spec(_spec)
sys.modules["ralph_rewind"] = _ralph_rewind
_spec.loader.exec_module(_ralph_rewind)

build_parser = _ralph_rewind.build_parser


class TestRalphRewindParser:
    """Test the CLI argument parser."""

    def test_required_args(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--lineage-id", "lin_test", "--to-generation", "3"])
        assert args.lineage_id == "lin_test"
        assert args.to_generation == 3
        assert args.git_checkout is False
        assert args.server_command == "ouroboros"
        assert args.server_args == ["mcp"]

    def test_git_checkout_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args([
            "--lineage-id", "lin_test",
            "--to-generation", "2",
            "--git-checkout",
        ])
        assert args.git_checkout is True

    def test_custom_server_command(self) -> None:
        parser = build_parser()
        args = parser.parse_args([
            "--lineage-id", "lin_test",
            "--to-generation", "1",
            "--server-command", "/usr/local/bin/ouroboros",
            "--server-args", "mcp", "serve",
        ])
        assert args.server_command == "/usr/local/bin/ouroboros"
        assert args.server_args == ["mcp", "serve"]

    def test_missing_lineage_id(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--to-generation", "1"])

    def test_missing_to_generation(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--lineage-id", "lin_test"])

    def test_to_generation_must_be_int(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--lineage-id", "lin_test", "--to-generation", "abc"])
