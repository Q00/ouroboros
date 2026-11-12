"""Standalone QA's configured default store is explicit and queryable."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import typer
from typer.testing import CliRunner

from ouroboros.cli.commands.qa import qa_command
from ouroboros.core.types import Result
from ouroboros.mcp.tools.query_handlers import SessionStatusHandler
from ouroboros.providers.base import CompletionResponse, UsageInfo


@pytest.mark.parametrize("configured", [True, False])
def test_standalone_cli_summary_is_queryable_from_default_store(configured, tmp_path, monkeypatch):
    config_dir = Path.home() / ".ouroboros"
    config_dir.mkdir()
    target = tmp_path / "configured.sqlite" if configured else config_dir / "ouroboros.db"
    if configured:
        (config_dir / "config.yaml").write_text(
            f"persistence:\n  database_path: {target}\n", encoding="utf-8"
        )
    inference = AsyncMock()
    inference.complete.return_value = Result.ok(
        CompletionResponse(content='{"score": 0.9}', model="fake", usage=UsageInfo(0, 0, 0))
    )
    monkeypatch.setattr("ouroboros.mcp.tools.qa.create_llm_adapter", lambda **_kw: inference)
    # Shell recovery is unrelated to the standalone persistence contract.
    monkeypatch.setattr("ouroboros.cli.commands.mcp._ensure_shell_env", lambda: None)
    app = typer.Typer()
    app.command()(qa_command)
    result = CliRunner().invoke(app, ["synthetic", "--qa-session-id", "qa-standalone"])
    assert result.exit_code == 0, result.output
    assert "Session: qa-standalone" in result.output
    assert target.is_file()

    async def observe():
        return await SessionStatusHandler().handle({"session_id": "qa-standalone"})

    status = asyncio.run(observe())
    assert status.is_ok
    assert status.value.meta["session_kind"] == "qa"
    assert status.value.meta["score"] == 0.9
    if configured:
        assert not (config_dir / "ouroboros.db").exists()


def test_standalone_cli_cannot_report_success_if_default_store_is_unwritable(tmp_path, monkeypatch):
    config_dir = Path.home() / ".ouroboros"
    config_dir.mkdir()
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    (config_dir / "config.yaml").write_text(
        f"persistence:\n  database_path: {blocked / 'events.sqlite'}\n", encoding="utf-8"
    )
    inference = AsyncMock()
    inference.complete.return_value = Result.ok(
        CompletionResponse(content='{"score": 0.9}', model="fake", usage=UsageInfo(0, 0, 0))
    )
    monkeypatch.setattr("ouroboros.mcp.tools.qa.create_llm_adapter", lambda **_kw: inference)
    monkeypatch.setattr("ouroboros.cli.commands.mcp._ensure_shell_env", lambda: None)
    app = typer.Typer()
    app.command()(qa_command)
    result = CliRunner().invoke(app, ["synthetic", "--qa-session-id", "qa-unpublished"])
    assert result.exit_code == 1, result.output
    assert "QA failed" in result.output
    assert "Session: qa-unpublished" not in result.output
    inference.complete.assert_awaited_once()
    assert not (config_dir / "ouroboros.db").exists()
