"""Exercise the production adapter/SDK boundary used by the doctor probe."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest

from ouroboros.cli.commands.mcp_doctor import (
    _collect_local_stdio_results,
    _local_stdio_probe_config,
    _probe_local_stdio,
)
from ouroboros.core.types import Result
from ouroboros.mcp.client.sdk_factory import SDKClientResources, TransportLifecycle
from ouroboros.mcp.tool_manifest import BUILTIN_TOOL_NAMES


async def test_real_sdk_discovery_failure_is_not_a_transport_failure():
    @asynccontextmanager
    async def transport():
        send_in, read = anyio.create_memory_object_stream(1)
        write, receive_out = anyio.create_memory_object_stream(1)
        async with send_in, read, write, receive_out:
            yield read, write

    with (
        patch("mcp.client.stdio.stdio_client", side_effect=lambda _: transport()),
        patch("mcp.client.client.negotiate_auto", side_effect=RuntimeError("discovery rejected")),
    ):
        results = await _probe_local_stdio()
    assert [result.status for result in results] == ["pass", "fail", "fail"]
    assert "protocol discovery failed" in results[1].message


async def test_real_adapter_teardown_failure_cannot_return_passing_probe():
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(side_effect=RuntimeError("child reap failed"))
    client.protocol_version = "2026-07-28"
    client.server_info = None
    client.server_capabilities = SimpleNamespace(
        tools=None, resources=None, prompts=None, logging=None, extensions=None
    )
    client.instructions = None
    client.session = SimpleNamespace(discover_result=None)
    client.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[]))
    with patch(
        "ouroboros.mcp.client.adapter.build_sdk_client",
        return_value=SDKClientResources(
            client, transport_lifecycle=TransportLifecycle(entered=True)
        ),
    ):
        results = await _probe_local_stdio()
    assert all(result.status == "fail" for result in results)
    assert "teardown failed" in results[0].message
    assert "child reap failed" in results[0].message
    client.__aexit__.assert_awaited_once()


@pytest.mark.parametrize(
    "missing",
    [
        "ouroboros_ac_dashboard",
        "ouroboros_record_conductor_decision",
        "ouroboros_session_signal",
        "ouroboros_session_signal_targets",
    ],
)
async def test_production_manifest_requires_tools_missing_from_legacy_oracle(missing, tmp_path):
    adapter = MagicMock()
    adapter.connect = AsyncMock(return_value=Result.ok(None))
    adapter.server_snapshot = object()
    adapter.protocol_version = "2026-07-28"
    adapter.list_tools = AsyncMock(
        return_value=Result.ok(
            tuple(SimpleNamespace(name=name) for name in BUILTIN_TOOL_NAMES - {missing})
        )
    )
    results = await _collect_local_stdio_results(adapter, _local_stdio_probe_config(tmp_path))
    assert results[2].status == "fail"
    assert missing in results[2].message


def test_authoritative_manifest_matches_real_production_composition(tmp_path):
    from ouroboros.mcp.server.adapter import create_ouroboros_server
    from ouroboros.persistence.event_store import EventStore, sqlite_database_url

    store = EventStore(sqlite_database_url(tmp_path / "manifest.db"))
    server = create_ouroboros_server(
        event_store=store, runtime_backend="host", project_dir=tmp_path, state_dir=tmp_path
    )
    assert frozenset(tool.name for tool in server.info.tools) == BUILTIN_TOOL_NAMES
