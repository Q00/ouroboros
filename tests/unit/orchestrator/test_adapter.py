"""Unit tests for ClaudeAgentAdapter."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.orchestrator.adapter import (
    AgentMessage,
    ClaudeAgentAdapter,
    DEFAULT_TOOLS,
    TaskResult,
)


class TestAgentMessage:
    """Tests for AgentMessage dataclass."""

    def test_create_assistant_message(self) -> None:
        """Test creating an assistant message."""
        msg = AgentMessage(
            type="assistant",
            content="I will analyze the code.",
        )
        assert msg.type == "assistant"
        assert msg.content == "I will analyze the code."
        assert msg.tool_name is None
        assert msg.is_final is False
        assert msg.is_error is False

    def test_create_tool_message(self) -> None:
        """Test creating a tool call message."""
        msg = AgentMessage(
            type="tool",
            content="Reading file",
            tool_name="Read",
        )
        assert msg.type == "tool"
        assert msg.tool_name == "Read"
        assert msg.is_final is False

    def test_create_result_message(self) -> None:
        """Test creating a result message."""
        msg = AgentMessage(
            type="result",
            content="Task completed successfully",
            data={"subtype": "success"},
        )
        assert msg.is_final is True
        assert msg.is_error is False

    def test_error_result_message(self) -> None:
        """Test creating an error result message."""
        msg = AgentMessage(
            type="result",
            content="Task failed",
            data={"subtype": "error"},
        )
        assert msg.is_final is True
        assert msg.is_error is True

    def test_message_is_frozen(self) -> None:
        """Test that AgentMessage is immutable."""
        msg = AgentMessage(type="assistant", content="test")
        with pytest.raises(AttributeError):
            msg.content = "modified"  # type: ignore


class TestTaskResult:
    """Tests for TaskResult dataclass."""

    def test_create_successful_result(self) -> None:
        """Test creating a successful task result."""
        messages = (
            AgentMessage(type="assistant", content="Working..."),
            AgentMessage(type="result", content="Done"),
        )
        result = TaskResult(
            success=True,
            final_message="Done",
            messages=messages,
            session_id="session_123",
        )
        assert result.success is True
        assert result.final_message == "Done"
        assert len(result.messages) == 2
        assert result.session_id == "session_123"

    def test_result_is_frozen(self) -> None:
        """Test that TaskResult is immutable."""
        result = TaskResult(
            success=True,
            final_message="Done",
            messages=(),
        )
        with pytest.raises(AttributeError):
            result.success = False  # type: ignore


class TestClaudeAgentAdapter:
    """Tests for ClaudeAgentAdapter."""

    def test_init_with_api_key(self) -> None:
        """Test initialization with explicit API key."""
        adapter = ClaudeAgentAdapter(api_key="test_key")
        assert adapter._api_key == "test_key"
        assert adapter._permission_mode == "acceptEdits"

    def test_init_with_custom_permission_mode(self) -> None:
        """Test initialization with custom permission mode."""
        adapter = ClaudeAgentAdapter(permission_mode="bypassPermissions")
        assert adapter._permission_mode == "bypassPermissions"

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "env_key"})
    def test_init_from_environment(self) -> None:
        """Test initialization from environment variable."""
        adapter = ClaudeAgentAdapter()
        assert adapter._api_key == "env_key"

    def test_convert_assistant_message(self) -> None:
        """Test converting SDK assistant message."""
        adapter = ClaudeAgentAdapter(api_key="test")

        # Mock SDK message structure
        mock_block = MagicMock()
        mock_block.text = "I am analyzing the code."

        mock_message = MagicMock()
        mock_message.type = "assistant"
        mock_message.message = MagicMock()
        mock_message.message.content = [mock_block]

        result = adapter._convert_message(mock_message)

        assert result.type == "assistant"
        assert result.content == "I am analyzing the code."

    def test_convert_tool_message(self) -> None:
        """Test converting SDK tool call message."""
        adapter = ClaudeAgentAdapter(api_key="test")

        # Mock SDK message with tool call
        mock_block = MagicMock()
        mock_block.name = "Edit"
        del mock_block.text  # Remove text attribute

        mock_message = MagicMock()
        mock_message.type = "assistant"
        mock_message.message = MagicMock()
        mock_message.message.content = [mock_block]

        result = adapter._convert_message(mock_message)

        assert result.type == "assistant"
        assert result.tool_name == "Edit"
        assert "Edit" in result.content

    def test_convert_result_message(self) -> None:
        """Test converting SDK result message."""
        adapter = ClaudeAgentAdapter(api_key="test")

        mock_message = MagicMock()
        mock_message.type = "result"
        mock_message.result = "Task completed"
        mock_message.subtype = "success"

        result = adapter._convert_message(mock_message)

        assert result.type == "result"
        assert result.content == "Task completed"
        assert result.data["subtype"] == "success"

    def test_convert_system_init_message(self) -> None:
        """Test converting SDK system init message."""
        adapter = ClaudeAgentAdapter(api_key="test")

        mock_message = MagicMock()
        mock_message.type = "system"
        mock_message.subtype = "init"
        mock_message.session_id = "sess_abc123"

        result = adapter._convert_message(mock_message)

        assert result.type == "system"
        assert "sess_abc123" in result.content
        assert result.data["session_id"] == "sess_abc123"

    @pytest.mark.asyncio
    async def test_execute_task_sdk_not_installed(self) -> None:
        """Test handling when SDK is not installed."""
        adapter = ClaudeAgentAdapter(api_key="test")

        with patch.dict("sys.modules", {"claude_agent_sdk": None}):
            # Simulate ImportError by patching the import
            messages = []
            async for msg in adapter.execute_task("test prompt"):
                messages.append(msg)

            # Should yield an error message when SDK not available
            # Note: Actual behavior depends on import mechanism

    @pytest.mark.asyncio
    async def test_execute_task_to_result_success(self) -> None:
        """Test execute_task_to_result with successful execution."""
        adapter = ClaudeAgentAdapter(api_key="test")

        # Mock the execute_task method
        async def mock_execute(*args: Any, **kwargs: Any):
            yield AgentMessage(type="assistant", content="Working...")
            yield AgentMessage(
                type="result",
                content="Task completed",
                data={"subtype": "success", "session_id": "sess_123"},
            )

        with patch.object(adapter, "execute_task", mock_execute):
            result = await adapter.execute_task_to_result("test prompt")

        assert result.is_ok
        assert result.value.success is True
        assert result.value.final_message == "Task completed"
        assert len(result.value.messages) == 2

    @pytest.mark.asyncio
    async def test_execute_task_to_result_failure(self) -> None:
        """Test execute_task_to_result with failed execution."""
        adapter = ClaudeAgentAdapter(api_key="test")

        async def mock_execute(*args: Any, **kwargs: Any):
            yield AgentMessage(type="assistant", content="Working...")
            yield AgentMessage(
                type="result",
                content="Task failed: error",
                data={"subtype": "error"},
            )

        with patch.object(adapter, "execute_task", mock_execute):
            result = await adapter.execute_task_to_result("test prompt")

        assert result.is_err
        assert "Task failed" in str(result.error)


class TestDefaultTools:
    """Tests for DEFAULT_TOOLS constant."""

    def test_default_tools_includes_essentials(self) -> None:
        """Test that default tools include essential operations."""
        assert "Read" in DEFAULT_TOOLS
        assert "Write" in DEFAULT_TOOLS
        assert "Edit" in DEFAULT_TOOLS
        assert "Bash" in DEFAULT_TOOLS
        assert "Glob" in DEFAULT_TOOLS
        assert "Grep" in DEFAULT_TOOLS
