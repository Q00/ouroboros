from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from ouroboros.cli.commands import auto, init, mcp, run
from ouroboros.cli.main import app

runner = CliRunner(env={"COLUMNS": "240"})


def _option_block(output: str, option: str) -> str:
    lines = output.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if f"│ {option}" in line)
    except StopIteration:
        pytest.fail(f"missing {option} option in help output:\n{output}")
    block: list[str] = []
    for line in lines[start:]:
        if block and "│ --" in line:
            break
        block.append(line)
    return "\n".join(block)


def test_zcode_is_exposed_by_terminal_backend_enums() -> None:
    """Terminal commands should accept zcode anywhere they expose backend choice."""
    assert run.AgentRuntimeBackend.ZCODE.value == "zcode"
    assert init.AgentRuntimeBackend.ZCODE.value == "zcode"
    assert init.LLMBackend.ZCODE.value == "zcode"
    assert mcp.AgentRuntimeBackend.ZCODE.value == "zcode"
    assert mcp.LLMBackend.ZCODE.value == "zcode"
    assert auto.AgentRuntimeBackend.ZCODE.value == "zcode"


@pytest.mark.parametrize(
    ("args", "options"),
    [
        (["run", "workflow", "--help"], ["--runtime"]),
        (["init", "start", "--help"], ["--runtime", "--llm-backend"]),
        (["mcp", "serve", "--help"], ["--runtime", "--llm-backend"]),
        (["mcp", "info", "--help"], ["--runtime", "--llm-backend"]),
        (["auto", "--help"], ["--runtime"]),
    ],
)
def test_zcode_is_visible_in_terminal_help_options(args: list[str], options: list[str]) -> None:
    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    for option in options:
        assert "zcode" in _option_block(result.output, option)


def test_init_cli_accepts_zcode_runtime_and_llm_values() -> None:
    mock_run_interview = AsyncMock()

    with patch("ouroboros.cli.commands.init._run_interview", new=mock_run_interview):
        result = runner.invoke(
            app,
            [
                "init",
                "start",
                "Build a REST API",
                "--runtime",
                "zcode",
                "--llm-backend",
                "zcode",
            ],
        )

    assert result.exit_code == 0, result.output
    assert mock_run_interview.await_args.args[5] == "zcode"
    assert mock_run_interview.await_args.args[6] == "zcode"


def test_terminal_backend_enums_accept_zcode_value() -> None:
    assert run.AgentRuntimeBackend("zcode") is run.AgentRuntimeBackend.ZCODE
    assert init.AgentRuntimeBackend("zcode") is init.AgentRuntimeBackend.ZCODE
    assert init.LLMBackend("zcode") is init.LLMBackend.ZCODE
    assert mcp.AgentRuntimeBackend("zcode") is mcp.AgentRuntimeBackend.ZCODE
    assert mcp.LLMBackend("zcode") is mcp.LLMBackend.ZCODE
    assert auto.AgentRuntimeBackend("zcode") is auto.AgentRuntimeBackend.ZCODE


def test_zcode_terminal_surface_keeps_opt_in_live_smoke_contract() -> None:
    smoke_path = Path(__file__).resolve().parents[2] / "integration" / "test_zcode_cli_smoke.py"
    if not smoke_path.exists():
        pytest.skip("zcode integration smoke moved or removed; update terminal-surface guard")
    smoke_source = smoke_path.read_text()

    assert "OUROBOROS_ZCODE_SMOKE" in smoke_source
    assert "test_real_zcode_runtime_returns_terminal_response" in smoke_source
    assert "test_real_zcode_llm_adapter_honors_json_object_response_format" in smoke_source
    assert 'response_format={"type": "json_object"}' in smoke_source
