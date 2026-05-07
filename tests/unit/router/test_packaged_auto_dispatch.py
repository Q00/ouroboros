from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator.command_dispatcher import create_codex_command_dispatcher
from ouroboros.router import Resolved, resolve_skill_dispatch


def test_packaged_auto_skill_dispatches_to_ouroboros_auto(tmp_path: Path) -> None:
    """Lock the packaged ``ooo auto`` dispatch metadata used by Codex runtimes."""
    prompt = 'ooo auto "Audit the open PRs" --skip-run'

    result = resolve_skill_dispatch(prompt, cwd=tmp_path)

    assert isinstance(result, Resolved)
    assert result.command_prefix == "ooo auto"
    assert result.mcp_tool == "ouroboros_auto"
    assert result.mcp_args == {
        "goal": "Audit the open PRs",
        "resume": "",
        "cwd": str(tmp_path),
        "max_interview_rounds": "",
        "max_repair_rounds": "",
        "skip_run": True,
        "driver": "",
        "brake": "",
    }
    assert result.first_argument == "Audit the open PRs --skip-run"


def test_packaged_auto_skill_dispatches_driver_and_brake_to_ouroboros_auto(
    tmp_path: Path,
) -> None:
    """Lock selected-driver options on the packaged ``ooo auto`` skill surface."""
    prompt = 'ooo auto "Audit the open PRs" --driver hermes --brake off'

    result = resolve_skill_dispatch(prompt, cwd=tmp_path)

    assert isinstance(result, Resolved)
    assert result.command_prefix == "ooo auto"
    assert result.mcp_tool == "ouroboros_auto"
    assert result.mcp_args == {
        "goal": "Audit the open PRs",
        "resume": "",
        "cwd": str(tmp_path),
        "max_interview_rounds": "",
        "max_repair_rounds": "",
        "skip_run": "",
        "driver": "hermes",
        "brake": "off",
    }
    assert result.first_argument == "Audit the open PRs --driver hermes --brake off"


def _fake_ouroboros_server() -> AsyncMock:
    server = AsyncMock()
    server.call_tool = AsyncMock(
        return_value=Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="ok"),),
                meta={"auto_session_id": "auto_test"},
            )
        )
    )
    return server


@pytest.mark.asyncio
async def test_packaged_auto_skill_dispatch_forwards_driver_and_brake_to_mcp_payload(
    tmp_path: Path,
) -> None:
    """Lock packaged ``ooo auto`` driver/brake forwarding into the actual MCP call."""
    prompt = 'ooo auto "Audit the open PRs" --driver hermes --brake off --skip-run'
    result = resolve_skill_dispatch(prompt, cwd=tmp_path)
    assert isinstance(result, Resolved)

    fake_server = _fake_ouroboros_server()
    dispatch = create_codex_command_dispatcher(cwd=tmp_path)

    with patch(
        "ouroboros.mcp.server.adapter.create_ouroboros_server",
        return_value=fake_server,
    ):
        messages = await dispatch(result, None)

    fake_server.call_tool.assert_awaited_once()
    tool_name, payload = fake_server.call_tool.await_args.args
    assert tool_name == "ouroboros_auto"
    assert payload["goal"] == "Audit the open PRs"
    assert payload["driver"] == "hermes"
    assert payload["brake"] == "off"
    assert payload["skip_run"] is True
    assert messages is not None
    assert messages[0].data["tool_input"] == payload


@pytest.mark.asyncio
async def test_packaged_auto_skill_dispatch_does_not_leak_placeholder_strings_for_unset_driver_brake(
    tmp_path: Path,
) -> None:
    """Plain ``ooo auto`` must not leak ``$driver``/``$brake`` placeholder literals into MCP."""
    result = resolve_skill_dispatch('ouroboros:auto "Audit the open PRs"', cwd=tmp_path)
    if not isinstance(result, Resolved):
        result = resolve_skill_dispatch('ooo auto "Audit the open PRs"', cwd=tmp_path)
    assert isinstance(result, Resolved)

    fake_server = _fake_ouroboros_server()
    dispatch = create_codex_command_dispatcher(cwd=tmp_path)

    with patch(
        "ouroboros.mcp.server.adapter.create_ouroboros_server",
        return_value=fake_server,
    ):
        await dispatch(result, None)

    payload = fake_server.call_tool.await_args.args[1]
    assert payload["goal"] == "Audit the open PRs"
    assert payload.get("driver", "") != "$driver"
    assert payload.get("brake", "") != "$brake"
    assert payload.get("driver", "") in ("", None)
    assert payload.get("brake", "") in ("", None)
