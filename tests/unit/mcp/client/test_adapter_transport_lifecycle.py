"""Tests for STDIO transport context manager lifecycle in MCPClientAdapter.

Covers the edge cases identified in PR #263 review:
1. Rollback on failed initialization (transport entered but session fails)
2. disconnect() cleans transport when session is None (partial connect)
3. disconnect() closes transport even when session.__aexit__ raises
4. Transport teardown errors are surfaced, not swallowed
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.mcp.client.adapter import MCPClientAdapter
from ouroboros.mcp.types import MCPServerConfig, TransportType


def _make_stdio_config(name: str = "test") -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        transport=TransportType.STDIO,
        command="echo",
        args=("hello",),
    )


def _mock_transport_cm(read=None, write=None):
    """Create a mock stdio_client context manager."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=(read or MagicMock(), write or MagicMock()))
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _mock_session(*, init_fail=False, exit_fail=False):
    """Create a mock ClientSession."""
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    if exit_fail:
        session.__aexit__ = AsyncMock(side_effect=RuntimeError("session exit boom"))
    else:
        session.__aexit__ = AsyncMock(return_value=None)
    if init_fail:
        session.initialize = AsyncMock(side_effect=ConnectionError("init failed"))
    else:
        session.initialize = AsyncMock(
            return_value=MagicMock(
                serverInfo=MagicMock(name="test", version="1.0"),
                protocolVersion="2024-11-05",
                capabilities=MagicMock(tools=None, resources=None, prompts=None, logging=None),
            )
        )
    return session


class TestConnectRollback:
    """Finding #1: rollback transport on failed session init."""

    @pytest.mark.asyncio
    async def test_transport_closed_when_session_init_fails(self):
        """If initialize() fails after transport is entered, transport must be closed."""
        adapter = MCPClientAdapter(max_retries=1)
        transport_cm = _mock_transport_cm()
        session = _mock_session(init_fail=True)

        with (
            patch("mcp.client.stdio.stdio_client", return_value=transport_cm),
            patch("mcp.ClientSession", return_value=session),
        ):
            result = await adapter.connect(_make_stdio_config())

        assert result.is_err
        # Transport must have been cleaned up (called once per retry attempt)
        transport_cm.__aexit__.assert_called_with(None, None, None)
        assert adapter._transport_cm is None
        assert adapter._session is None

    @pytest.mark.asyncio
    async def test_transport_closed_when_session_enter_fails(self):
        """If session.__aenter__ fails, transport must be closed."""
        adapter = MCPClientAdapter()
        transport_cm = _mock_transport_cm()
        session = MagicMock()
        session.__aenter__ = AsyncMock(side_effect=RuntimeError("session enter boom"))

        with (
            patch("mcp.client.stdio.stdio_client", return_value=transport_cm),
            patch("mcp.ClientSession", return_value=session),
        ):
            result = await adapter.connect(_make_stdio_config())

        assert result.is_err
        transport_cm.__aexit__.assert_called_once()
        assert adapter._transport_cm is None


class TestDisconnectPartialConnect:
    """Finding #2: disconnect() must clean transport even when session is None."""

    @pytest.mark.asyncio
    async def test_disconnect_cleans_orphaned_transport(self):
        """If _session is None but _transport_cm is set, disconnect must close it."""
        adapter = MCPClientAdapter()
        adapter._config = _make_stdio_config()
        adapter._session = None  # No session

        transport_cm = _mock_transport_cm()
        adapter._transport_cm = transport_cm  # But transport is open

        result = await adapter.disconnect()
        assert result.is_ok
        transport_cm.__aexit__.assert_called_once_with(None, None, None)
        assert adapter._transport_cm is None

    @pytest.mark.asyncio
    async def test_disconnect_noop_when_both_none(self):
        """If both session and transport are None, disconnect is a no-op."""
        adapter = MCPClientAdapter()
        result = await adapter.disconnect()
        assert result.is_ok


class TestDisconnectSessionFailure:
    """Finding #3: transport cleanup must run even when session exit raises."""

    @pytest.mark.asyncio
    async def test_transport_closed_when_session_exit_raises(self):
        """If session.__aexit__ raises, transport must still be closed."""
        adapter = MCPClientAdapter()
        adapter._config = _make_stdio_config()

        session = _mock_session(exit_fail=True)
        adapter._session = session

        transport_cm = _mock_transport_cm()
        adapter._transport_cm = transport_cm

        result = await adapter.disconnect()
        # Should report the error
        assert result.is_err
        # But transport must still be cleaned up
        transport_cm.__aexit__.assert_called_once_with(None, None, None)
        assert adapter._transport_cm is None
        assert adapter._session is None


class TestDisconnectErrorReporting:
    """Finding #4: transport teardown errors must be surfaced."""

    @pytest.mark.asyncio
    async def test_transport_exit_error_is_reported(self):
        """If transport.__aexit__ raises, the error must be in the Result."""
        adapter = MCPClientAdapter()
        adapter._config = _make_stdio_config()
        adapter._session = None  # Only transport

        transport_cm = MagicMock()
        transport_cm.__aexit__ = AsyncMock(side_effect=OSError("pipe broken"))
        adapter._transport_cm = transport_cm

        result = await adapter.disconnect()
        assert result.is_err
        assert "pipe broken" in str(result.error)
        # Transport ref still cleaned up
        assert adapter._transport_cm is None

    @pytest.mark.asyncio
    async def test_both_errors_reported_session_first(self):
        """If both session and transport raise, session error is primary."""
        adapter = MCPClientAdapter()
        adapter._config = _make_stdio_config()

        session = _mock_session(exit_fail=True)
        adapter._session = session

        transport_cm = MagicMock()
        transport_cm.__aexit__ = AsyncMock(side_effect=OSError("transport boom"))
        adapter._transport_cm = transport_cm

        result = await adapter.disconnect()
        assert result.is_err
        # First error (session) is primary
        assert "session exit boom" in str(result.error)
        # Both resources cleaned up
        assert adapter._session is None
        assert adapter._transport_cm is None


class TestHappyPath:
    """Verify the fix doesn't break normal connect/disconnect."""

    @pytest.mark.asyncio
    async def test_normal_connect_stores_transport_cm(self):
        adapter = MCPClientAdapter()
        transport_cm = _mock_transport_cm()
        session = _mock_session()

        with (
            patch("mcp.client.stdio.stdio_client", return_value=transport_cm),
            patch("mcp.ClientSession", return_value=session),
        ):
            result = await adapter.connect(_make_stdio_config())

        assert result.is_ok
        assert adapter._transport_cm is transport_cm
        assert adapter._session is not None

    @pytest.mark.asyncio
    async def test_normal_disconnect_cleans_both(self):
        adapter = MCPClientAdapter()
        adapter._config = _make_stdio_config()

        session = _mock_session()
        adapter._session = session
        transport_cm = _mock_transport_cm()
        adapter._transport_cm = transport_cm

        result = await adapter.disconnect()
        assert result.is_ok
        session.__aexit__.assert_called_once()
        transport_cm.__aexit__.assert_called_once()
        assert adapter._session is None
        assert adapter._transport_cm is None
