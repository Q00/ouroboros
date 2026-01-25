"""Shared fixtures for MCP tests."""

import pytest

from ouroboros.mcp.types import (
    MCPServerConfig,
    MCPToolDefinition,
    MCPToolParameter,
    ToolInputType,
    TransportType,
)


@pytest.fixture
def stdio_server_config() -> MCPServerConfig:
    """Create a sample stdio server configuration."""
    return MCPServerConfig(
        name="test-server",
        transport=TransportType.STDIO,
        command="test-mcp-server",
        args=("--mode", "test"),
        timeout=30.0,
    )


@pytest.fixture
def sample_tool_definition() -> MCPToolDefinition:
    """Create a sample tool definition."""
    return MCPToolDefinition(
        name="test_tool",
        description="A test tool for unit testing",
        parameters=(
            MCPToolParameter(
                name="input",
                type=ToolInputType.STRING,
                description="The input value",
                required=True,
            ),
            MCPToolParameter(
                name="count",
                type=ToolInputType.INTEGER,
                description="Number of iterations",
                required=False,
                default=1,
            ),
        ),
        server_name="test-server",
    )
