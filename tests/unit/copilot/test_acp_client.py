"""Exercise the stdio protocol boundary without a live Copilot account."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ouroboros.copilot.acp_client import AcpClientError, CopilotAcpClient
from ouroboros.copilot.acp_permissions import CopilotAcpPermissions

_FAKE = Path(__file__).parents[2] / "fixtures" / "fake_copilot_acp.py"


def client(tmp_path: Path, mode: str = "normal", **kwargs: object) -> CopilotAcpClient:
    permissions = CopilotAcpPermissions.for_task(str(tmp_path), "acceptEdits", ["Bash"])
    return CopilotAcpClient(
        [str(_FAKE), "--acp", "--stdio"],
        cwd=str(tmp_path),
        env={
            **os.environ,
            "FAKE_COPILOT_ACP_MODE": mode,
            "FAKE_COPILOT_ACP_LOG": str(tmp_path / "wire.ndjson"),
        },
        permission_handler=permissions.request_permission,
        shutdown_timeout=0.1,
        **kwargs,
    )


async def test_handshake_prompt_and_permission_rpc_use_exact_wire_contract(tmp_path: Path) -> None:
    connection = client(tmp_path)
    async with connection:
        initialization = await connection.initialize()
        assert initialization["protocolVersion"] == 1
        session = await connection.new_session()
        frames = [frame async for frame in connection.prompt("Inspect this fixture")]
    assert connection.process.returncode == 0
    assert connection.completed
    assert frames[-1]["result"]["stopReason"] == "end_turn"
    assert any(frame.get("method") == "session/request_permission" for frame in frames)
    wire = [json.loads(line) for line in (tmp_path / "wire.ndjson").read_text().splitlines()]
    requests = {r["method"]: r["params"] for r in wire if "method" in r}
    assert requests["initialize"]["clientCapabilities"] == {}
    assert requests["session/new"] == {"cwd": str(tmp_path), "mcpServers": []}
    assert requests["session/prompt"] == {
        "sessionId": session["sessionId"],
        "prompt": [{"type": "text", "text": "Inspect this fixture"}],
    }


@pytest.mark.parametrize(
    "mode,error_type",
    [
        ("unsupported", "rpc_error"),
        ("malformed", "malformed_response"),
        ("bad_envelope", "malformed_response"),
        ("wrong_id", "malformed_response"),
        ("both_result_error", "malformed_response"),
        ("non_object_result", "malformed_response"),
        ("bad_version", "protocol_incompatible"),
        ("auth_init", "authentication_error"),
        ("auth_plain", "authentication_error"),
        ("auth_data", "authentication_error"),
        ("auth_truncated", "authentication_error"),
        ("auth_nonrpc", "authentication_error"),
        ("stderr_auth", "authentication_error"),
    ],
)
async def test_startup_failures_are_classified_and_reaped(
    tmp_path: Path, mode: str, error_type: str
) -> None:
    connection = client(tmp_path, mode)
    with pytest.raises(AcpClientError) as error:
        async with connection:
            await connection.initialize()
    assert error.value.error_type == error_type
    assert not connection.prompt_sent
    assert connection.process.returncode is not None


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_timeouts_cannot_disable_protocol_bounds(tmp_path: Path, timeout: float) -> None:
    with pytest.raises(ValueError, match="positive"):
        client(tmp_path, idle_timeout=timeout)


async def test_only_one_prompt_per_connection_can_be_submitted(tmp_path: Path) -> None:
    connection = client(tmp_path)
    async with connection:
        await connection.initialize()
        await connection.new_session()
        assert [frame async for frame in connection.prompt("first")]
        with pytest.raises(AcpClientError, match="fresh"):
            assert [frame async for frame in connection.prompt("second")]


async def test_no_prompt_can_run_without_session_creation(tmp_path: Path) -> None:
    connection = client(tmp_path)
    async with connection:
        await connection.initialize()
        with pytest.raises(AcpClientError, match="fresh"):
            assert [frame async for frame in connection.prompt("first")]
