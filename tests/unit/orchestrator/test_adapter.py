"""Unit tests for ClaudeAgentAdapter."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.orchestrator.adapter import (
    DEFAULT_TOOLS,
    DELEGATED_EXECUTE_SEED_TOOL_MATCHER,
    DELEGATED_PARENT_CWD_ARG,
    DELEGATED_PARENT_EFFECTIVE_TOOLS_ARG,
    DELEGATED_PARENT_PERMISSION_MODE_ARG,
    DELEGATED_PARENT_SESSION_ID_ARG,
    DELEGATED_PARENT_TRANSCRIPT_PATH_ARG,
    AgentMessage,
    ClaudeAgentAdapter,
    RuntimeHandle,
    TaskResult,
    _build_delegated_tool_context_update,
)


# Helper function to create mock SDK messages with correct class names
def _create_mock_sdk_message(class_name: str, **attrs: Any) -> Any:
    """Create a mock object with a specific class name for SDK message testing."""
    mock_class = type(class_name, (), {})
    instance = mock_class()
    for key, value in attrs.items():
        setattr(instance, key, value)
    return instance


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


class TestRuntimeHandle:
    """Tests for RuntimeHandle serialization helpers."""

    def test_round_trip_dict(self) -> None:
        """Test runtime handles can be serialized and restored."""
        handle = RuntimeHandle(
            backend="claude",
            native_session_id="sess_123",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={"source": "test"},
        )

        restored = RuntimeHandle.from_dict(handle.to_dict())

        assert restored == handle

    def test_invalid_dict_returns_none(self) -> None:
        """Test invalid runtime handle payloads are rejected."""
        assert RuntimeHandle.from_dict({"native_session_id": "sess_123"}) is None


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

        # Create mock block with correct class name
        mock_block = _create_mock_sdk_message("TextBlock", text="I am analyzing the code.")

        # Create mock message with correct class name
        mock_message = _create_mock_sdk_message(
            "AssistantMessage",
            content=[mock_block],
        )

        result = adapter._convert_message(mock_message)

        assert result.type == "assistant"
        assert result.content == "I am analyzing the code."

    def test_convert_tool_message(self) -> None:
        """Test converting SDK tool call message."""
        adapter = ClaudeAgentAdapter(api_key="test")

        # Create mock block with correct class name (ToolUseBlock)
        mock_block = _create_mock_sdk_message("ToolUseBlock", name="Edit")

        # Create mock message with correct class name
        mock_message = _create_mock_sdk_message(
            "AssistantMessage",
            content=[mock_block],
        )

        result = adapter._convert_message(mock_message)

        assert result.type == "assistant"
        assert result.tool_name == "Edit"
        assert "Edit" in result.content

    def test_convert_result_message(self) -> None:
        """Test converting SDK result message."""
        adapter = ClaudeAgentAdapter(api_key="test")

        # Create mock message with correct class name
        mock_message = _create_mock_sdk_message(
            "ResultMessage",
            result="Task completed",
            subtype="success",
        )

        result = adapter._convert_message(mock_message)

        assert result.type == "result"
        assert result.content == "Task completed"
        assert result.data["subtype"] == "success"

    def test_convert_system_init_message(self) -> None:
        """Test converting SDK system init message."""
        adapter = ClaudeAgentAdapter(api_key="test")

        # Create mock message with correct class name
        mock_message = _create_mock_sdk_message(
            "SystemMessage",
            subtype="init",
            data={"session_id": "sess_abc123"},
        )

        result = adapter._convert_message(mock_message)

        assert result.type == "system"
        assert "sess_abc123" in result.content
        assert result.data["session_id"] == "sess_abc123"

    def test_build_delegated_tool_context_update_for_execute_seed(self) -> None:
        """Delegated execute-seed tool calls receive internal parent runtime metadata."""
        update = _build_delegated_tool_context_update(
            {
                "tool_name": "mcp__plugin_ouroboros_ouroboros__ouroboros_execute_seed",
                "tool_input": {"seed_content": "goal: test"},
                "session_id": "sess_parent",
                "transcript_path": "/tmp/parent.jsonl",
                "cwd": "/tmp/project",
                "permission_mode": "acceptEdits",
            },
            ["Read", "mcp__chrome-devtools__click"],
        )

        assert update is not None
        updated_input = update["updatedInput"]
        assert updated_input["seed_content"] == "goal: test"
        assert updated_input[DELEGATED_PARENT_SESSION_ID_ARG] == "sess_parent"
        assert updated_input[DELEGATED_PARENT_TRANSCRIPT_PATH_ARG] == "/tmp/parent.jsonl"
        assert updated_input[DELEGATED_PARENT_CWD_ARG] == "/tmp/project"
        assert updated_input[DELEGATED_PARENT_PERMISSION_MODE_ARG] == "acceptEdits"
        assert updated_input[DELEGATED_PARENT_EFFECTIVE_TOOLS_ARG] == [
            "Read",
            "mcp__chrome-devtools__click",
        ]

    def test_build_delegated_tool_context_update_ignores_other_tools(self) -> None:
        """Only delegated execute-seed tool calls should be rewritten."""
        update = _build_delegated_tool_context_update(
            {
                "tool_name": "Read",
                "tool_input": {"file_path": "src/app.py"},
                "session_id": "sess_parent",
            },
            ["Read"],
        )

        assert update is None

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
        runtime_handle = RuntimeHandle(backend="claude", native_session_id="sess_123")

        # Mock the execute_task method
        async def mock_execute(*args: Any, **kwargs: Any):
            yield AgentMessage(type="assistant", content="Working...")
            yield AgentMessage(
                type="result",
                content="Task completed",
                data={"subtype": "success", "session_id": "sess_123"},
                resume_handle=runtime_handle,
            )

        with patch.object(adapter, "execute_task", mock_execute):
            result = await adapter.execute_task_to_result("test prompt")

        assert result.is_ok
        assert result.value.success is True
        assert result.value.final_message == "Task completed"
        assert len(result.value.messages) == 2
        assert result.value.session_id == "sess_123"
        assert result.value.resume_handle == runtime_handle

    @pytest.mark.asyncio
    async def test_execute_task_uses_hook_and_fork_session_for_inherited_runtime(self) -> None:
        """Delegated child runs should fork the parent Claude session and inject tool context."""
        adapter = ClaudeAgentAdapter(api_key="test")
        inherited_handle = RuntimeHandle(
            backend="claude",
            native_session_id="sess_parent",
            approval_mode="bypassPermissions",
            metadata={"fork_session": True},
        )
        captured_options: dict[str, Any] = {}

        class FakeClaudeAgentOptions:
            def __init__(self, **kwargs: Any) -> None:
                captured_options.update(kwargs)

        async def fake_query(*, prompt: str, options: Any):
            yield _create_mock_sdk_message(
                "SystemMessage",
                subtype="init",
                data={"session_id": "sess_child"},
            )
            yield _create_mock_sdk_message(
                "ResultMessage",
                result="Done",
                subtype="success",
                session_id="sess_child",
                is_error=False,
            )

        with (
            patch("claude_agent_sdk.ClaudeAgentOptions", FakeClaudeAgentOptions),
            patch("claude_agent_sdk.query", fake_query),
        ):
            messages = [
                message
                async for message in adapter.execute_task(
                    "test prompt",
                    tools=["Read", "mcp__chrome-devtools__click"],
                    resume_handle=inherited_handle,
                )
            ]

        assert messages[-1].is_final is True
        assert captured_options["resume"] == "sess_parent"
        assert captured_options["fork_session"] is True
        assert captured_options["permission_mode"] == "bypassPermissions"
        assert captured_options["allowed_tools"] == ["Read", "mcp__chrome-devtools__click"]
        assert messages[-1].resume_handle is not None
        assert messages[-1].resume_handle.approval_mode == "bypassPermissions"

        hook_matchers = captured_options["hooks"]["PreToolUse"]
        assert len(hook_matchers) == 1
        assert hook_matchers[0].matcher == DELEGATED_EXECUTE_SEED_TOOL_MATCHER
        assert "mcp__plugin_ouroboros_ouroboros__ouroboros_execute_seed" in hook_matchers[0].matcher

        hook_output = await hook_matchers[0].hooks[0](
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "mcp__plugin_ouroboros_ouroboros__ouroboros_start_execute_seed",
                "tool_input": {"seed_content": "goal: child"},
                "tool_use_id": "toolu_123",
                "session_id": "sess_parent",
                "transcript_path": "/tmp/parent.jsonl",
                "cwd": "/tmp/project",
                "permission_mode": "acceptEdits",
            },
            None,
            {"signal": None},
        )

        assert hook_output is not None
        updated_input = hook_output["updatedInput"]
        assert updated_input[DELEGATED_PARENT_SESSION_ID_ARG] == "sess_parent"
        assert updated_input[DELEGATED_PARENT_EFFECTIVE_TOOLS_ARG] == [
            "Read",
            "mcp__chrome-devtools__click",
        ]

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
