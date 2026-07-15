"""Unit tests for scripts/ralph-rewind.py — argument parsing."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

# Load ralph-rewind.py as a module without requiring its dependencies at import time.
_RALPH_REWIND_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ralph-rewind.py"
_spec = importlib.util.spec_from_file_location("ralph_rewind", _RALPH_REWIND_PATH)
assert _spec and _spec.loader
_ralph_rewind = importlib.util.module_from_spec(_spec)
sys.modules["ralph_rewind"] = _ralph_rewind
_spec.loader.exec_module(_ralph_rewind)

build_parser = _ralph_rewind.build_parser
call_rewind = _ralph_rewind._call_rewind


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
        args = parser.parse_args(
            [
                "--lineage-id",
                "lin_test",
                "--to-generation",
                "2",
                "--git-checkout",
            ]
        )
        assert args.git_checkout is True

    def test_custom_server_command(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "--lineage-id",
                "lin_test",
                "--to-generation",
                "1",
                "--server-command",
                "/usr/local/bin/ouroboros",
                "--server-args",
                "mcp",
                "serve",
            ]
        )
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


def _args(*, checkout: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        lineage_id="lin_test",
        to_generation=2,
        git_checkout=checkout,
    )


def _tool_result(text: str | None, *, is_error: bool = False) -> SimpleNamespace:
    content = [] if text is None else [SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(content=content, isError=is_error)


class TestRalphRewindResponseContract:
    @pytest.mark.asyncio
    async def test_calls_rewind_once_with_exact_arguments(self) -> None:
        session = SimpleNamespace(call_tool=AsyncMock(return_value=_tool_result("ok")))

        result = await call_rewind(session, _args())

        session.call_tool.assert_called_once_with(
            "ouroboros_evolve_rewind",
            {"lineage_id": "lin_test", "to_generation": 2},
        )
        assert result["error"] is None
        assert result["git_checkout"] is False

    @pytest.mark.asyncio
    async def test_checkout_uses_generation_tag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = SimpleNamespace(call_tool=AsyncMock(return_value=_tool_result("ok")))
        run = Mock()
        monkeypatch.setattr(_ralph_rewind.subprocess, "run", run)

        result = await call_rewind(session, _args(checkout=True))

        run.assert_called_once_with(
            ["git", "checkout", "ooo/lin_test/gen_2"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert result["git_checkout"] is True

    @pytest.mark.asyncio
    async def test_checkout_failure_does_not_retry_rewind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = SimpleNamespace(call_tool=AsyncMock(return_value=_tool_result("ok")))
        monkeypatch.setattr(
            _ralph_rewind.subprocess,
            "run",
            Mock(
                side_effect=_ralph_rewind.subprocess.CalledProcessError(
                    1,
                    ["git", "checkout"],
                    stderr="missing tag",
                )
            ),
        )

        result = await call_rewind(session, _args(checkout=True))

        session.call_tool.assert_called_once()
        assert result["git_checkout"] is False
        assert result["error"] == "git checkout failed: missing tag"

    @pytest.mark.asyncio
    async def test_tool_error_preserves_error_shape(self) -> None:
        session = SimpleNamespace(
            call_tool=AsyncMock(return_value=_tool_result("rewind failed", is_error=True))
        )

        result = await call_rewind(session, _args())

        assert result == {
            "lineage_id": "lin_test",
            "from_generation": None,
            "to_generation": 2,
            "git_checkout": False,
            "error": "rewind failed",
        }

    @pytest.mark.asyncio
    async def test_missing_text_preserves_error_shape(self) -> None:
        session = SimpleNamespace(call_tool=AsyncMock(return_value=_tool_result(None)))

        result = await call_rewind(session, _args())

        assert result["error"] == "No text content in evolve_rewind response"
