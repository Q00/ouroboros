"""Unit tests for the Codex CLI-backed LLM adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.codex_cli_adapter import CodexCliLLMAdapter


class _FakeProcess:
    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self._stdout = stdout.encode("utf-8")
        self._stderr = stderr.encode("utf-8")
        self.returncode = returncode

    async def communicate(self, _input: bytes | None = None) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


class TestCodexCliLLMAdapter:
    """Tests for CodexCliLLMAdapter."""

    def test_build_prompt_preserves_system_and_roles(self) -> None:
        """Prompt builder keeps system instructions and conversation order."""
        adapter = CodexCliLLMAdapter(cli_path="codex", cwd="/tmp/project")

        prompt = adapter._build_prompt(
            [
                Message(role=MessageRole.SYSTEM, content="Follow JSON strictly."),
                Message(role=MessageRole.USER, content="Explain the bug."),
                Message(role=MessageRole.ASSISTANT, content="Need more context."),
                Message(role=MessageRole.USER, content="It fails on startup."),
            ]
        )

        assert "## System Instructions" in prompt
        assert "Follow JSON strictly." in prompt
        assert "User: Explain the bug." in prompt
        assert "Assistant: Need more context." in prompt
        assert "User: It fails on startup." in prompt

    def test_build_prompt_includes_tool_constraints_and_turn_budget(self) -> None:
        """Prompt includes advisory interview settings for backend parity."""
        adapter = CodexCliLLMAdapter(
            cli_path="codex",
            allowed_tools=["Read", "Grep"],
            max_turns=5,
        )

        prompt = adapter._build_prompt([Message(role=MessageRole.USER, content="Inspect the repo.")])

        assert "## Tool Constraints" in prompt
        assert "- Read" in prompt
        assert "- Grep" in prompt
        assert "## Execution Budget" in prompt
        assert "5 tool-assisted turns" in prompt

    def test_normalize_model_omits_default_sentinel(self) -> None:
        """The backend-safe default sentinel is translated to no explicit model."""
        adapter = CodexCliLLMAdapter(cli_path="codex")

        assert adapter._normalize_model("default") is None
        assert adapter._normalize_model(" o3 ") == "o3"

    def test_build_command_uses_read_only_by_default(self) -> None:
        """Default permission mode maps to a read-only sandbox."""
        adapter = CodexCliLLMAdapter(cli_path="codex")

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
        )

        assert "--sandbox" in command
        assert "read-only" in command

    def test_build_command_uses_full_auto_for_accept_edits(self) -> None:
        """acceptEdits maps to Codex full-auto mode."""
        adapter = CodexCliLLMAdapter(cli_path="codex", permission_mode="acceptEdits")

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
        )

        assert "--full-auto" in command
        assert "--sandbox" not in command

    def test_build_command_uses_dangerous_bypass_when_requested(self) -> None:
        """bypassPermissions maps to the Codex dangerous bypass flag."""
        adapter = CodexCliLLMAdapter(cli_path="codex", permission_mode="bypassPermissions")

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
        )

        assert "--dangerously-bypass-approvals-and-sandbox" in command

    @pytest.mark.asyncio
    async def test_complete_success_reads_output_file(self) -> None:
        """Successful completions return the CLI output and session id."""
        adapter = CodexCliLLMAdapter(cli_path="codex", cwd="/tmp/project")

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Final answer", encoding="utf-8")
            assert "--model" not in command
            assert kwargs["cwd"] == "/tmp/project"
            # Prompt should be passed as the last positional argument
            assert command[-1] != "--ephemeral"  # prompt comes after flags
            return _FakeProcess(
                stdout=json.dumps({"type": "thread.started", "thread_id": "thread-123"}),
                returncode=0,
            )

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Summarize this change.")],
                CompletionConfig(model="default"),
            )

        assert result.is_ok
        assert result.value.content == "Final answer"
        assert result.value.model == "default"
        assert result.value.raw_response["session_id"] == "thread-123"

    @pytest.mark.asyncio
    async def test_complete_passes_json_schema_output_constraints(self) -> None:
        """Structured-output requests write and pass a JSON schema file."""
        adapter = CodexCliLLMAdapter(cli_path="codex")
        seen_schema: dict[str, object] = {}

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text('{"approved": true}', encoding="utf-8")

            schema_index = command.index("--output-schema") + 1
            seen_schema.update(json.loads(Path(command[schema_index]).read_text(encoding="utf-8")))
            return _FakeProcess(returncode=0)

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Return a verdict.")],
                CompletionConfig(
                    model="o3",
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "type": "object",
                            "properties": {"approved": {"type": "boolean"}},
                            "required": ["approved"],
                        },
                    },
                ),
            )

        assert result.is_ok
        assert seen_schema["type"] == "object"
        assert seen_schema["required"] == ["approved"]

    @pytest.mark.asyncio
    async def test_complete_returns_provider_error_on_nonzero_exit(self) -> None:
        """CLI failures are surfaced as ProviderError results."""
        adapter = CodexCliLLMAdapter(cli_path="codex")

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("", encoding="utf-8")
            return _FakeProcess(stderr="boom", returncode=2)

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Do the thing.")],
                CompletionConfig(model="o3"),
            )

        assert result.is_err
        assert result.error.provider == "codex_cli"
        assert result.error.details["returncode"] == 2
        assert "boom" in result.error.message

    @pytest.mark.asyncio
    async def test_complete_emits_debug_callbacks_from_json_events(self) -> None:
        """Codex adapter translates JSON events into debug callbacks."""
        callback_events: list[tuple[str, str]] = []

        def callback(message_type: str, content: str) -> None:
            callback_events.append((message_type, content))

        adapter = CodexCliLLMAdapter(cli_path="codex", on_message=callback)

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Final answer", encoding="utf-8")
            return _FakeProcess(
                stdout="\n".join(
                    [
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {"type": "reasoning", "text": "Thinking..."},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {
                                    "type": "command_execution",
                                    "command": "pytest -q",
                                },
                            }
                        ),
                    ]
                ),
                returncode=0,
            )

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Run the checks.")],
                CompletionConfig(model="default"),
            )

        assert result.is_ok
        assert callback_events == [("thinking", "Thinking..."), ("tool", "Bash: pytest -q")]

    def test_build_command_includes_prompt_as_positional_arg(self) -> None:
        """Prompt is passed as the last positional argument, not via stdin."""
        adapter = CodexCliLLMAdapter(cli_path="codex", cwd="/tmp/project")

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
            prompt="Explain this code",
        )

        assert command[-1] == "Explain this code"

    def test_build_command_without_prompt_omits_positional_arg(self) -> None:
        """When prompt is None, no positional argument is appended."""
        adapter = CodexCliLLMAdapter(cli_path="codex", cwd="/tmp/project")

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
        )

        # Last element should be a flag or path, not a prompt
        assert command[-1] in ("--ephemeral", "/tmp/out.txt") or command[-1].startswith("--")


class TestLazyImport:
    """Test lazy import of CodexCliLLMAdapter from providers package."""

    def test_codex_cli_adapter_accessible_from_providers_package(self) -> None:
        """CodexCliLLMAdapter is available via providers.__getattr__."""
        import ouroboros.providers as providers

        adapter_class = providers.CodexCliLLMAdapter
        assert adapter_class is CodexCliLLMAdapter

    def test_unknown_attribute_raises_attribute_error(self) -> None:
        """Accessing a nonexistent attribute raises AttributeError."""
        import ouroboros.providers as providers

        with pytest.raises(AttributeError, match="NonExistent"):
            _ = providers.NonExistent
