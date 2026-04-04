"""Unit tests for ClaudeAgentAdapter."""

from __future__ import annotations

from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.orchestrator.adapter import (
    DEFAULT_TOOLS,
    AgentMessage,
    ClaudeAgentAdapter,
    RuntimeHandle,
    TaskResult,
    _clone_runtime_handle_data,
)


# Helper function to create mock SDK messages with correct class names
def _create_mock_sdk_message(class_name: str, **attrs: Any) -> Any:
    """Create a mock object with a specific class name for SDK message testing."""
    mock_class = type(class_name, (), {})
    instance = mock_class()
    for key, value in attrs.items():
        setattr(instance, key, value)
    return instance


def _build_mock_claude_agent_sdk(
    *,
    query_impl: Any,
    options_sink: list[dict[str, Any]] | None = None,
) -> dict[str, ModuleType | None]:
    """Build a minimal Claude SDK module stub for adapter execution tests.

    Returns a dict suitable for ``patch.dict("sys.modules", ...)``,
    covering both ``claude_agent_sdk`` and ``claude_agent_sdk.types``.
    """
    module = ModuleType("claude_agent_sdk")

    class _MockClaudeAgentOptions:
        def __init__(self, **kwargs: Any) -> None:
            if options_sink is not None:
                options_sink.append(kwargs)

    class _MockHookMatcher:
        def __init__(self, **kwargs: Any) -> None:
            pass

    module.ClaudeAgentOptions = _MockClaudeAgentOptions
    module.query = query_impl

    types_module = ModuleType("claude_agent_sdk.types")
    types_module.HookMatcher = _MockHookMatcher  # type: ignore[attr-defined]
    module.types = types_module  # type: ignore[attr-defined]

    return {
        "claude_agent_sdk": module,
        "claude_agent_sdk.types": types_module,
    }


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

    def test_to_dict_writes_only_canonical_backend_field(self) -> None:
        """New runtime payload writes should emit only the canonical backend selector."""
        handle = RuntimeHandle(
            backend="claude_code",
            native_session_id="sess_123",
            cwd="/tmp/project",
        )

        serialized = handle.to_dict()

        assert serialized["backend"] == "claude"
        assert "provider" not in serialized

    @pytest.mark.parametrize(
        ("selector", "expected_backend"),
        [
            ("claude_code", "claude"),
            ("codex", "codex_cli"),
            ("opencode_cli", "opencode"),
        ],
    )
    def test_init_normalizes_legacy_backend_aliases(
        self,
        selector: str,
        expected_backend: str,
    ) -> None:
        """Legacy backend aliases should normalize immediately on construction."""
        handle = RuntimeHandle(
            backend=selector,
            native_session_id="sess_123",
            cwd="/tmp/project",
        )

        assert handle.backend == expected_backend
        assert handle == RuntimeHandle(
            backend=expected_backend,
            native_session_id="sess_123",
            cwd="/tmp/project",
        )

    def test_non_dict_payload_returns_none(self) -> None:
        """Missing runtime payloads still deserialize to None."""
        assert RuntimeHandle.from_dict(None) is None

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            pytest.param(
                {
                    "backend": "claude_code",
                    "native_session_id": "sess_123",
                    "cwd": "/tmp/project",
                },
                RuntimeHandle(
                    backend="claude",
                    native_session_id="sess_123",
                    cwd="/tmp/project",
                ),
                id="backend-alias-only",
            ),
            pytest.param(
                {
                    "provider": "codex",
                    "kind": "agent_runtime",
                    "native_session_id": "thread-123",
                    "cwd": "/tmp/project",
                },
                RuntimeHandle(
                    backend="codex_cli",
                    kind="agent_runtime",
                    native_session_id="thread-123",
                    cwd="/tmp/project",
                ),
                id="provider-only-alias",
            ),
            pytest.param(
                {
                    "backend": "opencode_cli",
                    "provider": "opencode",
                    "native_session_id": "oc-session-123",
                },
                RuntimeHandle(
                    backend="opencode",
                    native_session_id="oc-session-123",
                ),
                id="matching-backend-provider-aliases",
            ),
        ],
    )
    def test_from_dict_accepts_legacy_selector_aliases(
        self,
        payload: dict[str, Any],
        expected: RuntimeHandle,
    ) -> None:
        """Supported backend/provider aliases should deserialize to the canonical backend."""
        restored = RuntimeHandle.from_dict(payload)

        assert restored == expected

    def test_provider_only_payload_serializes_back_to_canonical_backend_on_new_write(self) -> None:
        """Legacy provider-only reads should emit canonical backend data on new writes."""
        payload = {
            "provider": "opencode_cli",
            "kind": "implementation_session",
            "native_session_id": "oc-session-123",
            "cwd": "/tmp/project",
            "approval_mode": "acceptEdits",
            "metadata": {"server_session_id": "server-42"},
        }

        restored = RuntimeHandle.from_dict(payload)

        assert restored is not None
        assert payload == {
            "provider": "opencode_cli",
            "kind": "implementation_session",
            "native_session_id": "oc-session-123",
            "cwd": "/tmp/project",
            "approval_mode": "acceptEdits",
            "metadata": {"server_session_id": "server-42"},
        }
        assert restored.to_dict() == {
            "backend": "opencode",
            "kind": "implementation_session",
            "native_session_id": "oc-session-123",
            "conversation_id": None,
            "previous_response_id": None,
            "transcript_path": None,
            "cwd": "/tmp/project",
            "approval_mode": "acceptEdits",
            "updated_at": None,
            "metadata": {"server_session_id": "server-42"},
        }

    def test_from_dict_detaches_legacy_provider_only_payload_from_source_metadata(self) -> None:
        """Legacy payload reads should not retain mutable aliases to persisted metadata."""
        payload = {
            "provider": "opencode_cli",
            "kind": "implementation_session",
            "cwd": "/tmp/project",
            "metadata": {
                "server_session_id": "server-42",
                "tool_catalog": [{"name": "Read"}],
            },
        }

        restored = RuntimeHandle.from_dict(payload)
        assert restored is not None

        restored.metadata["server_session_id"] = "server-99"
        restored.metadata["tool_catalog"][0]["name"] = "Write"

        assert payload["metadata"] == {
            "server_session_id": "server-42",
            "tool_catalog": [{"name": "Read"}],
        }

    def test_from_dict_rejects_payload_without_selector(self) -> None:
        """Selector-less payloads should fail eagerly."""
        with pytest.raises(ValueError) as exc_info:
            RuntimeHandle.from_dict({"native_session_id": "sess_123"})

        assert exc_info.type is ValueError
        assert "selector" in str(exc_info.value).lower()

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(
                {
                    "backend": "   ",
                    "provider": "\t",
                    "native_session_id": "sess_123",
                },
                id="blank-backend-and-provider",
            ),
            pytest.param(
                {
                    "backend": None,
                    "provider": "",
                    "native_session_id": "sess_123",
                },
                id="empty-provider-without-backend",
            ),
        ],
    )
    def test_from_dict_rejects_unresolvable_selector_shapes(
        self,
        payload: dict[str, Any],
    ) -> None:
        """Ambiguous selector payloads should keep the existing boundary failure semantics."""
        with pytest.raises(ValueError) as exc_info:
            RuntimeHandle.from_dict(payload)

        assert exc_info.type is ValueError
        assert "selector" in str(exc_info.value).lower()
        assert "determined" in str(exc_info.value).lower()

    def test_init_rejects_unknown_backend_selector(self) -> None:
        """Unknown backend aliases should fail with the public exception type."""
        with pytest.raises(ValueError) as exc_info:
            RuntimeHandle(backend="mystery-runtime")

        assert exc_info.type is ValueError
        assert "unsupported" in str(exc_info.value).lower()
        assert "backend" in str(exc_info.value).lower()

    @pytest.mark.parametrize(
        ("payload", "field_name"),
        [
            pytest.param(
                {
                    "backend": "mystery-runtime",
                    "native_session_id": "sess_123",
                },
                "backend",
                id="unknown-backend",
            ),
            pytest.param(
                {
                    "provider": "mystery-runtime",
                    "native_session_id": "sess_123",
                },
                "provider",
                id="unknown-provider",
            ),
        ],
    )
    def test_from_dict_rejects_unknown_selector_aliases(
        self,
        payload: dict[str, Any],
        field_name: str,
    ) -> None:
        """Unknown selector spellings should fail eagerly instead of widening alias support."""
        with pytest.raises(ValueError) as exc_info:
            RuntimeHandle.from_dict(payload)

        assert exc_info.type is ValueError
        assert "unsupported" in str(exc_info.value).lower()
        assert field_name in str(exc_info.value).lower()

    def test_from_dict_rejects_conflicting_backend_and_provider(self) -> None:
        """Conflicting canonical selectors should fail eagerly at the boundary."""
        with pytest.raises(ValueError) as exc_info:
            RuntimeHandle.from_dict(
                {
                    "backend": "codex_cli",
                    "provider": "opencode_cli",
                    "native_session_id": "sess_123",
                }
            )

        assert exc_info.type is ValueError
        assert "backend/provider" in str(exc_info.value).lower()
        assert "conflict" in str(exc_info.value).lower()

    def test_opencode_session_state_dict_keeps_only_resume_fields(self) -> None:
        """OpenCode session persistence should strip transient runtime fields."""
        handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="oc-session-123",
            conversation_id="conversation-1",
            previous_response_id="response-1",
            transcript_path="/tmp/opencode.jsonl",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            updated_at="2026-03-13T09:00:00+00:00",
            metadata={
                "ac_id": "ac_2",
                "server_session_id": "server-42",
                "session_attempt_id": "ac_2_attempt_2",
                "session_scope_id": "ac_2",
                "session_state_path": "execution.acceptance_criteria.ac_2.implementation_session",
                "scope": "ac",
                "session_role": "implementation",
                "retry_attempt": 1,
                "attempt_number": 2,
                "tool_catalog": [{"name": "Read"}],
                "runtime_event_type": "session.started",
                "debug_token": "drop-me",
            },
        )

        persisted = handle.to_session_state_dict()
        restored = RuntimeHandle.from_dict(persisted)

        assert persisted == {
            "backend": "opencode",
            "kind": "implementation_session",
            "native_session_id": "oc-session-123",
            "cwd": "/tmp/project",
            "approval_mode": "acceptEdits",
            "metadata": {
                "ac_id": "ac_2",
                "server_session_id": "server-42",
                "session_attempt_id": "ac_2_attempt_2",
                "session_scope_id": "ac_2",
                "session_state_path": "execution.acceptance_criteria.ac_2.implementation_session",
                "scope": "ac",
                "session_role": "implementation",
                "retry_attempt": 1,
                "attempt_number": 2,
                "tool_catalog": [{"name": "Read"}],
            },
        }
        assert restored is not None
        assert restored.backend == "opencode"
        assert restored.native_session_id == "oc-session-123"
        assert restored.cwd == "/tmp/project"
        assert restored.approval_mode == "acceptEdits"
        assert restored.ac_id == "ac_2"
        assert restored.metadata["server_session_id"] == "server-42"
        assert restored.session_scope_id == "ac_2"
        assert restored.session_attempt_id == "ac_2_attempt_2"
        assert "runtime_event_type" not in restored.metadata

    def test_opencode_handle_exposes_reconnect_identifiers(self) -> None:
        """OpenCode handles should expose the reconnect ids carried in metadata."""
        handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="oc-session-123",
            metadata={"server_session_id": "server-42"},
        )
        server_only_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            metadata={"server_session_id": "server-99"},
        )

        assert handle.server_session_id == "server-42"
        assert handle.resume_session_id == "oc-session-123"
        assert server_only_handle.server_session_id == "server-99"
        assert server_only_handle.resume_session_id == "server-99"

    @pytest.mark.asyncio
    async def test_runtime_handle_exposes_lifecycle_snapshot_and_live_controls(self) -> None:
        """Live controls stay off the persisted payload but remain callable in memory."""
        control_calls = {"observe": 0, "terminate": 0}

        async def _observe(handle: RuntimeHandle) -> dict[str, object]:
            control_calls["observe"] += 1
            snapshot = handle.snapshot()
            snapshot["observed"] = True
            return snapshot

        async def _terminate(_handle: RuntimeHandle) -> bool:
            control_calls["terminate"] += 1
            return True

        handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="oc-session-123",
            metadata={
                "server_session_id": "server-42",
                "runtime_event_type": "session.started",
            },
        ).bind_controls(
            observe_callback=_observe,
            terminate_callback=_terminate,
        )

        observed = await handle.observe()

        assert handle.control_session_id == "server-42"
        assert handle.lifecycle_state == "running"
        assert handle.can_resume is True
        assert handle.can_observe is True
        assert handle.can_terminate is True
        assert observed["observed"] is True
        assert observed["control_session_id"] == "server-42"
        assert observed["lifecycle_state"] == "running"
        assert await handle.terminate() is True
        assert control_calls == {"observe": 1, "terminate": 1}
        assert RuntimeHandle.from_dict(handle.to_session_state_dict()) == RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="oc-session-123",
            metadata={"server_session_id": "server-42"},
        )


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

    def test_init_with_custom_cwd_and_cli_path(self) -> None:
        """Test initialization stores backend-neutral runtime construction data."""
        adapter = ClaudeAgentAdapter(cwd="/tmp/project", cli_path="/tmp/claude")
        assert adapter._cwd == "/tmp/project"
        assert adapter._cli_path == "/tmp/claude"

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "env_key"})
    def test_init_from_environment(self) -> None:
        """Test initialization from environment variable."""
        adapter = ClaudeAgentAdapter()
        assert adapter._api_key == "env_key"

    def test_build_runtime_handle_preserves_existing_scope_metadata(self) -> None:
        """Coordinator-scoped runtime metadata survives native session binding."""
        adapter = ClaudeAgentAdapter(api_key="test", cwd="/tmp/project")
        seeded_handle = RuntimeHandle(
            backend="claude",
            kind="level_coordinator",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "scope": "level",
                "level_number": 3,
                "session_role": "coordinator",
            },
        )

        handle = adapter._build_runtime_handle("sess_123", seeded_handle)

        assert handle is not None
        assert handle.backend == "claude"
        assert handle.kind == "level_coordinator"
        assert handle.native_session_id == "sess_123"
        assert handle.cwd == "/tmp/project"
        assert handle.approval_mode == "acceptEdits"
        assert handle.metadata == seeded_handle.metadata

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

    @pytest.mark.asyncio
    async def test_execute_task_sdk_not_installed(self) -> None:
        """Test handling when SDK is not installed."""
        adapter = ClaudeAgentAdapter(api_key="test")

        with patch.dict("sys.modules", {"claude_agent_sdk": None}):
            messages = [msg async for msg in adapter.execute_task("test prompt")]

        assert len(messages) == 1
        assert messages[0] == AgentMessage(
            type="result",
            content="Claude Agent SDK is not installed. Run: pip install claude-agent-sdk",
            data={"subtype": "error"},
        )

    @pytest.mark.asyncio
    async def test_execute_task_rejects_foreign_runtime_handle_before_sdk_dispatch_as_error_result(
        self,
    ) -> None:
        """Foreign runtime handles should fail at the streaming boundary before SDK dispatch."""
        adapter = ClaudeAgentAdapter(api_key="test")
        query_calls = 0

        async def mock_query(*args: Any, **kwargs: Any):
            nonlocal query_calls
            query_calls += 1
            if False:
                yield args, kwargs

        sdk_modules = _build_mock_claude_agent_sdk(query_impl=mock_query)

        with patch.dict("sys.modules", sdk_modules):
            messages = [
                message
                async for message in adapter.execute_task(
                    "test prompt",
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        native_session_id="oc-session-123",
                    ),
                )
            ]

        assert query_calls == 0
        assert len(messages) == 1
        assert messages[0] == AgentMessage(
            type="result",
            content="Task execution failed: runtime handle is incompatible with this runtime.",
            data={
                "subtype": "error",
                "error_type": "RuntimeHandleError",
            },
        )

    @pytest.mark.asyncio
    async def test_execute_task_yields_error_result_without_propagating_sdk_exception(
        self,
    ) -> None:
        """SDK exceptions should stay on the streamed error path with resume context intact."""
        adapter = ClaudeAgentAdapter(api_key="test", cwd="/tmp/project")

        async def mock_query(*, prompt: str, options: Any):
            assert prompt == "test prompt"
            assert options is not None
            yield _create_mock_sdk_message(
                "SystemMessage",
                subtype="init",
                data={"session_id": "sess_456"},
            )
            raise RuntimeError("boom")

        sdk_modules = _build_mock_claude_agent_sdk(query_impl=mock_query)

        with patch.dict("sys.modules", sdk_modules):
            messages = [message async for message in adapter.execute_task("test prompt")]

        assert len(messages) == 2
        assert messages[0].type == "system"
        assert messages[0].data["session_id"] == "sess_456"
        assert messages[0].resume_handle is not None
        assert messages[0].resume_handle.backend == "claude"
        assert messages[0].resume_handle.native_session_id == "sess_456"
        assert messages[0].resume_handle.cwd == "/tmp/project"
        assert messages[0].resume_handle.approval_mode == "acceptEdits"
        assert messages[0].resume_handle.updated_at is not None
        assert messages[1].type == "result"
        assert messages[1].content == "Task execution failed: boom"
        assert messages[1].data == {
            "subtype": "error",
            "error_type": "RuntimeError",
            "session_id": "sess_456",
        }
        assert messages[1].resume_handle == messages[0].resume_handle

    @pytest.mark.asyncio
    async def test_execute_task_to_result_preserves_runtime_handle_contract(self) -> None:
        """Result aggregation should preserve the streamed RuntimeHandle contract."""
        adapter = ClaudeAgentAdapter(api_key="test", cwd="/tmp/project")

        async def mock_query(*, prompt: str, options: Any):
            assert prompt == "test prompt"
            assert options is not None
            yield _create_mock_sdk_message(
                "SystemMessage",
                subtype="init",
                data={"session_id": "sess_456"},
            )
            yield _create_mock_sdk_message(
                "AssistantMessage",
                content=[_create_mock_sdk_message("TextBlock", text="Working...")],
            )
            yield _create_mock_sdk_message(
                "ResultMessage",
                result="Task completed",
                subtype="success",
            )

        sdk_modules = _build_mock_claude_agent_sdk(query_impl=mock_query)

        with patch.dict("sys.modules", sdk_modules):
            result = await adapter.execute_task_to_result(
                "test prompt",
                resume_handle=RuntimeHandle(
                    backend="claude",
                    native_session_id="sess_123",
                ),
                resume_session_id="legacy-session-id",
            )

        assert result.is_ok
        task_result = result.value
        assert task_result.success is True
        assert task_result.final_message == "Task completed"
        assert task_result.session_id == "sess_456"
        runtime_handle = task_result.resume_handle
        assert runtime_handle is not None
        assert runtime_handle.backend == "claude"
        assert runtime_handle.native_session_id == "sess_456"
        assert runtime_handle.cwd == "/tmp/project"
        assert runtime_handle.approval_mode == "acceptEdits"
        assert runtime_handle.updated_at is not None
        assert [message.type for message in task_result.messages] == [
            "system",
            "assistant",
            "result",
        ]
        assert [message.content for message in task_result.messages] == [
            "Session initialized: sess_456",
            "Working...",
            "Task completed",
        ]
        assert all(message.resume_handle == runtime_handle for message in task_result.messages)

    @pytest.mark.asyncio
    async def test_execute_task_to_result_failure(self) -> None:
        """Failure aggregation should preserve existing ProviderError details."""
        adapter = ClaudeAgentAdapter(api_key="test", cwd="/tmp/project")

        async def mock_query(*, prompt: str, options: Any):
            assert prompt == "test prompt"
            assert options is not None
            yield _create_mock_sdk_message(
                "SystemMessage",
                subtype="init",
                data={"session_id": "sess_456"},
            )
            raise RuntimeError("boom")

        sdk_modules = _build_mock_claude_agent_sdk(query_impl=mock_query)

        with patch.dict("sys.modules", sdk_modules):
            result = await adapter.execute_task_to_result("test prompt")

        assert result.is_err
        assert result.error.message == "Task execution failed: boom"
        assert result.error.provider is None
        assert result.error.status_code is None
        assert result.error.details == {
            "messages": [
                "Session initialized: sess_456",
                "Task execution failed: boom",
            ]
        }

    @pytest.mark.asyncio
    async def test_execute_task_to_result_rejects_foreign_runtime_handle_before_sdk_dispatch(
        self,
    ) -> None:
        """Foreign runtime handles should stay on the existing ProviderError result path."""
        adapter = ClaudeAgentAdapter(api_key="test")
        query_calls = 0

        async def mock_query(*args: Any, **kwargs: Any):
            nonlocal query_calls
            query_calls += 1
            if False:
                yield args, kwargs

        sdk_modules = _build_mock_claude_agent_sdk(query_impl=mock_query)

        with patch.dict("sys.modules", sdk_modules):
            result = await adapter.execute_task_to_result(
                "test prompt",
                resume_handle=RuntimeHandle(
                    backend="opencode",
                    native_session_id="oc-session-123",
                ),
            )

        assert result.is_err
        assert result.error.message == (
            "Task execution failed: runtime handle is incompatible with this runtime."
        )
        assert result.error.provider is None
        assert result.error.status_code is None
        assert result.error.details == {
            "messages": ["Task execution failed: runtime handle is incompatible with this runtime."]
        }
        assert query_calls == 0

    @pytest.mark.asyncio
    async def test_execute_task_to_result_preserves_sdk_not_installed_error_precedence(
        self,
    ) -> None:
        """Aggregation should preserve the streaming path's SDK import error precedence."""
        adapter = ClaudeAgentAdapter(api_key="test")

        with patch.dict(
            "sys.modules",
            {"claude_agent_sdk": None, "claude_agent_sdk.types": None},
        ):
            result = await adapter.execute_task_to_result(
                "test prompt",
                resume_handle=RuntimeHandle(
                    backend="opencode",
                    native_session_id="oc-session-123",
                ),
            )

        # dispatch rejects the foreign handle *before* the SDK import path
        assert result.is_err
        assert "incompatible" in result.error.message

    @pytest.mark.asyncio
    async def test_execute_task_streams_runtime_handle_contract_across_messages(
        self,
    ) -> None:
        """Streaming execution should attach one canonical RuntimeHandle to each message."""
        adapter = ClaudeAgentAdapter(api_key="test", cwd="/tmp/project")

        async def mock_query(*, prompt: str, options: Any):
            assert prompt == "test prompt"
            assert options is not None
            yield _create_mock_sdk_message(
                "SystemMessage",
                subtype="init",
                data={"session_id": "sess_456"},
            )
            yield _create_mock_sdk_message(
                "AssistantMessage",
                content=[
                    _create_mock_sdk_message(
                        "TextBlock",
                        text="Inspecting repository state.",
                    )
                ],
            )
            yield _create_mock_sdk_message(
                "ResultMessage",
                result="Task completed",
                subtype="success",
                session_id="sess_456",
            )

        sdk_modules = _build_mock_claude_agent_sdk(query_impl=mock_query)

        with patch.dict("sys.modules", sdk_modules):
            stream = adapter.execute_task(
                "test prompt",
                resume_handle=RuntimeHandle(
                    backend="claude",
                    native_session_id="sess_123",
                ),
            )
            first_message = await anext(stream)
            second_message = await anext(stream)
            final_message = await anext(stream)

            with pytest.raises(StopAsyncIteration):
                await anext(stream)

        assert first_message.type == "system"
        assert first_message.content == "Session initialized: sess_456"
        runtime_handle = first_message.resume_handle
        assert runtime_handle is not None
        assert runtime_handle.backend == "claude"
        assert runtime_handle.native_session_id == "sess_456"
        assert runtime_handle.cwd == "/tmp/project"
        assert runtime_handle.approval_mode == "acceptEdits"
        assert runtime_handle.updated_at is not None
        assert runtime_handle.to_dict()["backend"] == "claude"
        assert "provider" not in runtime_handle.to_dict()

        assert second_message.type == "assistant"
        assert second_message.content == "Inspecting repository state."
        assert second_message.resume_handle == runtime_handle

        assert final_message.type == "result"
        assert final_message.content == "Task completed"
        assert final_message.resume_handle == runtime_handle


class TestCloneRuntimeHandleData:
    """Tests for _clone_runtime_handle_data deep-clone behavior."""

    def test_clones_nested_dict_list_structures(self) -> None:
        """Nested mutable structures should be fully detached from the source."""
        source: dict[str, Any] = {"a": [{"b": 1}, {"c": [2, 3]}], "d": {"e": "f"}}
        cloned = _clone_runtime_handle_data(source)

        assert cloned == source
        cloned["a"][0]["b"] = 99
        cloned["d"]["e"] = "changed"
        assert source["a"][0]["b"] == 1  # type: ignore[index]
        assert source["d"]["e"] == "f"  # type: ignore[index]

    def test_clones_tuple_contents(self) -> None:
        """Tuple values should be recursively cloned."""
        inner = {"key": [1, 2]}
        source = {"data": (inner, "scalar")}
        cloned = _clone_runtime_handle_data(source)

        assert cloned["data"] == ({"key": [1, 2]}, "scalar")
        assert isinstance(cloned["data"], tuple)
        cloned["data"][0]["key"].append(3)
        assert inner["key"] == [1, 2]

    def test_scalars_pass_through(self) -> None:
        """Scalar values should pass through unchanged."""
        assert _clone_runtime_handle_data("hello") == "hello"
        assert _clone_runtime_handle_data(42) == 42
        assert _clone_runtime_handle_data(None) is None
        assert _clone_runtime_handle_data(True) is True


class TestRuntimeHandleIdentityAliases:
    """Tests for identity alias mappings (canonical → canonical)."""

    @pytest.mark.parametrize(
        ("canonical_backend",),
        [
            ("claude",),
            ("codex_cli",),
            ("opencode",),
        ],
    )
    def test_init_preserves_canonical_backend_as_is(
        self,
        canonical_backend: str,
    ) -> None:
        """Canonical backend values should pass through normalization unchanged."""
        handle = RuntimeHandle(
            backend=canonical_backend,
            native_session_id="sess_123",
        )
        assert handle.backend == canonical_backend


class TestBuildRuntimeHandleFreshPath:
    """Tests for _build_runtime_handle when no seeded handle is provided."""

    def test_build_runtime_handle_creates_fresh_handle_without_seeded_handle(self) -> None:
        """When no current_handle is provided, a fresh handle should be created."""
        adapter = ClaudeAgentAdapter(
            api_key="test",
            cwd="/tmp/project",
            permission_mode="acceptEdits",
        )
        handle = adapter._build_runtime_handle(
            native_session_id="sess_789",
            current_handle=None,
        )

        assert handle is not None
        assert handle.backend == "claude"
        assert handle.kind == "agent_runtime"
        assert handle.native_session_id == "sess_789"
        assert handle.cwd == "/tmp/project"
        assert handle.approval_mode == "acceptEdits"
        assert handle.metadata == {}
        assert handle.updated_at is not None

    def test_build_runtime_handle_returns_none_without_session_id(self) -> None:
        """When no session_id is provided, no handle should be created."""
        adapter = ClaudeAgentAdapter(api_key="test")
        handle = adapter._build_runtime_handle(
            native_session_id=None,
            current_handle=None,
        )
        assert handle is None

    def test_build_runtime_handle_deep_clones_seeded_metadata(self) -> None:
        """Seeded handle metadata should be deep-cloned, not shallow-copied."""
        adapter = ClaudeAgentAdapter(api_key="test", cwd="/tmp/project")
        nested_metadata = {"tools": [{"name": "Read"}], "config": {"key": "val"}}
        seeded = RuntimeHandle(
            backend="claude",
            native_session_id="sess_old",
            cwd="/tmp/project",
            metadata=nested_metadata,
        )

        handle = adapter._build_runtime_handle(
            native_session_id="sess_new",
            current_handle=seeded,
        )

        assert handle is not None
        handle.metadata["tools"][0]["name"] = "Write"
        handle.metadata["config"]["key"] = "changed"
        assert nested_metadata["tools"][0]["name"] == "Read"  # type: ignore[index]
        assert nested_metadata["config"]["key"] == "val"  # type: ignore[index]


class TestNonStringSelectorErrorMessage:
    """Tests for improved error messages when selectors are non-string types."""

    @pytest.mark.parametrize(
        ("selector_value", "expected_type"),
        [
            (42, "int"),
            (["claude"], "list"),
            (True, "bool"),
        ],
    )
    def test_init_rejects_non_string_backend_with_type_info(
        self,
        selector_value: Any,
        expected_type: str,
    ) -> None:
        """Non-string backend selectors should report the actual type in the error."""
        with pytest.raises(ValueError, match=f"must be a string, got {expected_type}"):
            RuntimeHandle(backend=selector_value)

    def test_from_dict_rejects_non_string_backend_with_type_info(self) -> None:
        """Non-string backend in persisted payload should report type in the error."""
        with pytest.raises(ValueError, match="must be a string, got int"):
            RuntimeHandle.from_dict({"backend": 123, "native_session_id": "sess"})


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
