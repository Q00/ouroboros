"""CLI/MCP parity for ``ouroboros status project``."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from ouroboros.cli.main import app
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPToolError
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult

runner = CliRunner(env={"COLUMNS": "240"})

_PROJECT_RECORD = {
    "schema_version": 1,
    "project_id": "project_0123456789abcdef0123456789abcdef",
    "project_root": "/work/project",
    "workspace_path": "packages/app",
    "run_count": 0,
    "runs": [],
    "gated_seed_count": 0,
    "forced_seed_count": 0,
    "unknown_seed_count": 0,
    "override_rate": None,
}


class _RecordingHandler:
    arguments: dict | None = None


def _patch_handler(result: Result):
    async def fake_handle(_self, arguments):
        _RecordingHandler.arguments = dict(arguments)
        return result

    return patch(
        "ouroboros.cli.commands.status.ProjectStatusHandler.handle",
        fake_handle,
    )


def test_json_is_exact_mcp_structured_content(tmp_path: Path) -> None:
    tool_result = MCPToolResult(
        content=(MCPContentItem(type=ContentType.TEXT, text="Project Status\n"),),
        structured_content=_PROJECT_RECORD,
    )
    with _patch_handler(Result.ok(tool_result)):
        result = runner.invoke(
            app,
            [
                "status",
                "project",
                str(tmp_path),
                "--workspace",
                "packages/app",
                "--limit",
                "7",
                "--json",
            ],
        )

    assert result.exit_code == 0
    assert result.output.rstrip("\n") == json.dumps(_PROJECT_RECORD, indent=2, sort_keys=True)
    assert json.loads(result.output) == tool_result.structured_content
    assert _RecordingHandler.arguments == {
        "project_dir": str(tmp_path),
        "workspace": "packages/app",
        "limit": 7,
    }


def test_default_project_directory_is_forwarded(monkeypatch, tmp_path: Path) -> None:
    tool_result = MCPToolResult(structured_content=_PROJECT_RECORD)
    monkeypatch.chdir(tmp_path)
    with _patch_handler(Result.ok(tool_result)):
        result = runner.invoke(app, ["status", "project", "--json"])

    assert result.exit_code == 0
    assert _RecordingHandler.arguments == {
        "project_dir": str(tmp_path),
        "limit": 100,
    }


def test_invalid_limit_fails_before_handler_dispatch(tmp_path: Path) -> None:
    _RecordingHandler.arguments = None
    tool_result = MCPToolResult(structured_content=_PROJECT_RECORD)
    with _patch_handler(Result.ok(tool_result)):
        result = runner.invoke(
            app,
            ["status", "project", str(tmp_path), "--limit", "0", "--json"],
        )

    assert result.exit_code == 64
    assert "positive integer" in result.output
    assert _RecordingHandler.arguments is None


def test_handler_failure_returns_no_json_record(tmp_path: Path) -> None:
    with _patch_handler(Result.err(MCPToolError("identity conflict"))):
        result = runner.invoke(
            app,
            ["status", "project", str(tmp_path), "--json"],
        )

    assert result.exit_code == 1
    assert "identity conflict" in result.output
    assert "project_id" not in result.output
