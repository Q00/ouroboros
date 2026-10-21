"""Exercise the production adapter/SDK boundary used by the doctor probe."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest

from ouroboros.cli.commands.mcp_doctor import (
    CheckResult,
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
    assert [result.status for result in results] == ["pass", "pass", "fail", "fail"]
    assert results[-1].name == "local_stdio_cleanup"
    assert "teardown failed" in results[-1].message
    assert "child reap failed" in results[-1].message
    client.__aexit__.assert_awaited_once()


async def test_discovery_and_teardown_failure_preserve_separate_stage_results():
    client = MagicMock()
    client.__aenter__ = AsyncMock(side_effect=RuntimeError("discovery rejected"))
    client.__aexit__ = AsyncMock(side_effect=SystemExit(73))
    with patch(
        "ouroboros.mcp.client.adapter.build_sdk_client",
        return_value=SDKClientResources(
            client, transport_lifecycle=TransportLifecycle(entered=True)
        ),
    ):
        results = await _probe_local_stdio()

    assert [result.status for result in results] == ["pass", "fail", "fail", "fail"]
    assert "discovery rejected" in results[1].message
    assert "Not run" in results[2].message
    assert results[3].name == "local_stdio_cleanup"
    assert "73" in results[3].message
    client.__aexit__.assert_awaited_once()


async def test_successful_stages_preserved_when_both_owned_cleanup_operations_fail(
    tmp_path, monkeypatch
):
    class FailingTemporaryDirectory:
        name = str(tmp_path)

        def __init__(self, *, prefix):
            pass

        def cleanup(self):
            raise OSError("temporary-state cleanup failed")

    monkeypatch.setattr(
        "ouroboros.cli.commands.mcp_doctor.tempfile.TemporaryDirectory",
        FailingTemporaryDirectory,
    )
    adapter = MagicMock()
    adapter.disconnect = AsyncMock(side_effect=SystemExit(74))
    monkeypatch.setattr("ouroboros.cli.commands.mcp_doctor.MCPClientAdapter", lambda **_: adapter)
    observed = [
        CheckResult(name, "pass", "observed success")
        for name in (
            "local_stdio_startup_transport",
            "local_stdio_protocol_discovery",
            "local_stdio_tool_recognition",
        )
    ]
    monkeypatch.setattr(
        "ouroboros.cli.commands.mcp_doctor._collect_local_stdio_results",
        AsyncMock(return_value=observed),
    )

    results = await _probe_local_stdio()

    assert [result.status for result in results] == ["pass", "pass", "pass", "fail"]
    assert "SystemExit: 74" in results[-1].message
    assert "temporary-state cleanup failed" in results[-1].message


async def test_baseexception_from_real_adapter_teardown_becomes_visible_probe_failure():
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(side_effect=SystemExit(73))
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

    assert [result.status for result in results] == ["pass", "pass", "fail", "fail"]
    assert "SystemExit: 73" in results[-1].message
    client.__aexit__.assert_awaited_once()


async def test_temporary_directory_cleanup_failure_becomes_visible_probe_failure(
    tmp_path, monkeypatch
):
    class CleanupFailingTemporaryDirectory:
        name = str(tmp_path)

        def __init__(self, *, prefix):
            assert prefix == "ouroboros-doctor-"

        def cleanup(self):
            raise OSError("temporary-state cleanup failed")

    monkeypatch.setattr(
        "ouroboros.cli.commands.mcp_doctor.tempfile.TemporaryDirectory",
        CleanupFailingTemporaryDirectory,
    )
    adapter = MagicMock()
    adapter.disconnect = AsyncMock(return_value=SimpleNamespace(is_err=False))
    adapter.connect = AsyncMock(return_value=SimpleNamespace(is_err=True, error="not used"))
    adapter.transport_entered = False
    monkeypatch.setattr("ouroboros.cli.commands.mcp_doctor.MCPClientAdapter", lambda **_: adapter)

    results = await _probe_local_stdio()

    assert [result.status for result in results] == ["fail", "fail", "fail", "fail"]
    assert "temporary-state cleanup failed" in results[-1].message


async def test_probe_preserves_external_cancellation_while_teardown_also_fails(monkeypatch):
    cancellation = anyio.get_cancelled_exc_class()("probe cancelled")
    adapter = MagicMock()
    adapter.disconnect = AsyncMock(side_effect=SystemExit(74))
    monkeypatch.setattr("ouroboros.cli.commands.mcp_doctor.MCPClientAdapter", lambda **_: adapter)

    async def cancel_probe(*_args):
        raise cancellation

    monkeypatch.setattr(
        "ouroboros.cli.commands.mcp_doctor._collect_local_stdio_results", cancel_probe
    )

    with pytest.raises(type(cancellation)) as caught:
        await _probe_local_stdio()

    assert caught.value is cancellation
    assert any("SystemExit" in note and "74" in note for note in cancellation.__notes__)
    adapter.disconnect.assert_awaited_once()


async def test_probe_preserves_primary_error_when_teardown_also_fails(monkeypatch):
    adapter = MagicMock()
    adapter.disconnect = AsyncMock(side_effect=SystemExit(74))
    monkeypatch.setattr("ouroboros.cli.commands.mcp_doctor.MCPClientAdapter", lambda **_: adapter)

    async def fail_probe(*_args):
        raise RuntimeError("discovery rejected")

    monkeypatch.setattr(
        "ouroboros.cli.commands.mcp_doctor._collect_local_stdio_results", fail_probe
    )

    results = await _probe_local_stdio()

    assert [result.status for result in results] == ["fail", "fail", "fail", "fail"]
    assert "discovery rejected" in results[0].message
    assert "teardown failed (SystemExit: 74)" in results[-1].message
    adapter.disconnect.assert_awaited_once()


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
