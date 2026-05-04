"""Unit tests for the Codex CLI-backed LLM adapter."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.config.models import OuroborosConfig
from ouroboros.core.errors import ProviderError
from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.codex_cli_adapter import CodexCliLLMAdapter
from ouroboros.providers.codex_cli_stream import collect_stream_lines


class _FakeStream:
    def __init__(
        self,
        text: str = "",
        *,
        read_size: int | None = None,
    ) -> None:
        self._buffer = text.encode("utf-8")
        self._cursor = 0
        self._read_size = read_size

    async def read(self, chunk_size: int = 16384) -> bytes:
        if self._cursor >= len(self._buffer):
            return b""

        size = self._read_size or chunk_size
        next_cursor = min(self._cursor + size, len(self._buffer))
        chunk = self._buffer[self._cursor : next_cursor]
        self._cursor = next_cursor
        return chunk


class _FakeStdin:
    """Minimal stdin stub that captures written bytes."""

    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        wait_forever: bool = False,
        read_size: int | None = None,
    ) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(stdout, read_size=read_size)
        self.stderr = _FakeStream(stderr, read_size=read_size)
        self.returncode = None if wait_forever else returncode
        self._final_returncode = returncode
        self._wait_forever = wait_forever
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        if self._wait_forever and self.returncode is None:
            await asyncio.Future()
        self.returncode = self._final_returncode
        return self.returncode

    async def communicate(self, _input: bytes | None = None) -> tuple[bytes, bytes]:
        raise AssertionError("communicate() should not be used by the streaming adapter")

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = self._final_returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = self._final_returncode


class _LegacyFakeProcess:
    """Process stub that exercises the communicate() fallback branch."""

    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        self.stdin = _FakeStdin()
        self._stdout = stdout.encode("utf-8")
        self._stderr = stderr.encode("utf-8")
        self.returncode = returncode
        self.communicate_calls = 0

    async def communicate(self, _input: bytes | None = None) -> tuple[bytes, bytes]:
        self.communicate_calls += 1
        return self._stdout, self._stderr


class TestCodexCliLLMAdapter:
    """Tests for CodexCliLLMAdapter."""

    @staticmethod
    def _write_wrapper(path: Path) -> Path:
        path.write_bytes(b"\xcf\xfa\xed\xfe")
        path.chmod(0o755)
        return path

    @staticmethod
    def _write_real_cli(path: Path) -> Path:
        path.write_text("#!/usr/bin/env node\nconsole.log('codex')\n", encoding="utf-8")
        path.chmod(0o755)
        return path

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

        prompt = adapter._build_prompt(
            [Message(role=MessageRole.USER, content="Inspect the repo.")]
        )

        assert "## Tool Constraints" in prompt
        assert "- Read" in prompt
        assert "- Grep" in prompt
        assert "## Execution Budget" in prompt
        assert "5 tool-assisted turns" in prompt

    def test_build_prompt_omits_tool_constraints_when_tools_unspecified(self) -> None:
        """Default adapters keep tool policy unspecified for non-interview flows."""
        adapter = CodexCliLLMAdapter(cli_path="codex")

        prompt = adapter._build_prompt([Message(role=MessageRole.USER, content="Summarize this.")])

        assert "## Tool Constraints" not in prompt
        assert "Do NOT use any tools or MCP calls" not in prompt

    def test_build_prompt_explicit_empty_tools_forbids_tool_use(self) -> None:
        """An explicit empty tool list requests a text-only response."""
        adapter = CodexCliLLMAdapter(cli_path="codex", allowed_tools=[], max_turns=5)

        prompt = adapter._build_prompt([Message(role=MessageRole.USER, content="Summarize this.")])

        assert "## Tool Constraints" in prompt
        assert "Do NOT use any tools or MCP calls" in prompt
        assert "tool-assisted turns" not in prompt
        assert "avoid turning this into a multi-step tool workflow" in prompt

    def test_normalize_model_omits_default_sentinel(self) -> None:
        """The backend-safe default sentinel is translated to no explicit model."""
        adapter = CodexCliLLMAdapter(cli_path="codex")

        assert adapter._normalize_model("default") is None
        assert adapter._normalize_model(" o3 ") == "o3"

    def test_resolve_cli_path_falls_back_from_wrapper(self, tmp_path: Path) -> None:
        """Provider adapter should apply the same wrapper-safe fallback as runtime."""
        wrapper = self._write_wrapper(tmp_path / "codex-wrapper")
        real_dir = tmp_path / "bin"
        real_dir.mkdir()
        real_cli = self._write_real_cli(real_dir / "codex")

        with (
            patch.dict(os.environ, {"PATH": str(real_dir)}),
            patch("ouroboros.providers.codex_cli_adapter.log.warning") as mock_warning,
            patch("ouroboros.providers.codex_cli_adapter.log.info") as mock_info,
        ):
            adapter = CodexCliLLMAdapter(cli_path=wrapper)

        assert adapter._cli_path == str(real_cli)
        mock_warning.assert_called_once_with(
            "codex_cli_adapter.cli_wrapper_detected",
            wrapper_path=str(wrapper),
            hint="Searching PATH for the real Node.js codex CLI.",
        )
        mock_info.assert_called_once_with(
            "codex_cli_adapter.cli_resolved_via_fallback",
            fallback_path=str(real_cli),
        )

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

    def test_build_command_prefers_profile_over_model(self) -> None:
        """Codex task profiles use --profile and avoid a conflicting --model."""
        adapter = CodexCliLLMAdapter(cli_path="codex")

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model="gpt-5.4",
            profile="ouroboros-deep",
        )

        assert "--profile" in command
        assert "ouroboros-deep" in command
        assert "--model" not in command

    @pytest.mark.asyncio
    async def test_complete_resolves_codex_profile_from_task_role(self) -> None:
        """Role profile resolution reaches the Codex CLI command line."""
        adapter = CodexCliLLMAdapter(cli_path="codex")
        task_config = OuroborosConfig(
            llm_profiles={
                "fast": {
                    "providers": {
                        "codex": {
                            "profile": "ouroboros-fast",
                            "model": "gpt-5.3-codex-spark",
                        },
                    },
                },
            },
            llm_role_profiles={"qa": "fast"},
        )

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Profiled answer", encoding="utf-8")
            assert "--profile" in command
            assert "ouroboros-fast" in command
            assert "--model" not in command
            return _FakeProcess(returncode=0)

        with (
            patch(
                "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ),
            patch("ouroboros.providers.profiles.load_config", return_value=task_config),
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Return a QA verdict.")],
                CompletionConfig(model="default", role="qa"),
            )

        assert result.is_ok
        assert result.value.content == "Profiled answer"

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
            assert Path(kwargs["cwd"]) == Path("/tmp/project")
            # Prompt is now fed via stdin, not as a positional argument
            assert kwargs.get("stdin") is not None
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
        assert seen_schema["additionalProperties"] is False

    @pytest.mark.asyncio
    async def test_complete_normalizes_optional_object_fields_for_codex_schema(self) -> None:
        """Codex schemas must require every property and disallow extras."""
        adapter = CodexCliLLMAdapter(cli_path="codex")
        seen_schema: dict[str, object] = {}

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text(
                '{"approved": true, "confidence": 0.92, "reasoning": "Looks good."}',
                encoding="utf-8",
            )

            schema_index = command.index("--output-schema") + 1
            seen_schema.update(json.loads(Path(command[schema_index]).read_text(encoding="utf-8")))
            return _FakeProcess(returncode=0)

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Return a vote.")],
                CompletionConfig(
                    model="default",
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "type": "object",
                            "properties": {
                                "approved": {"type": "boolean"},
                                "confidence": {"type": "number"},
                                "reasoning": {"type": "string"},
                            },
                            "required": ["approved"],
                        },
                    },
                ),
            )

        assert result.is_ok
        assert seen_schema["required"] == ["approved", "confidence", "reasoning"]
        assert seen_schema["additionalProperties"] is False

    @pytest.mark.asyncio
    async def test_complete_restores_open_map_objects_after_codex_schema_rewrite(self) -> None:
        """Open-map object schemas are rewritten for Codex and restored on output."""
        adapter = CodexCliLLMAdapter(cli_path="codex")
        seen_schema: dict[str, object] = {}

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text(
                json.dumps(
                    {
                        "score": 0.9,
                        "verdict": "pass",
                        "dimensions": [
                            {"key": "coverage", "value": 0.88},
                            {"key": "ux", "value": 0.91},
                        ],
                        "differences": [],
                        "suggestions": [],
                        "reasoning": "Looks solid.",
                    }
                ),
                encoding="utf-8",
            )

            schema_index = command.index("--output-schema") + 1
            seen_schema.update(json.loads(Path(command[schema_index]).read_text(encoding="utf-8")))
            return _FakeProcess(returncode=0)

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Return a QA verdict.")],
                CompletionConfig(
                    model="default",
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "type": "object",
                            "properties": {
                                "score": {"type": "number"},
                                "verdict": {"type": "string"},
                                "dimensions": {
                                    "type": "object",
                                    "additionalProperties": {"type": "number"},
                                },
                                "differences": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "suggestions": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "reasoning": {"type": "string"},
                            },
                            "required": [
                                "score",
                                "verdict",
                                "dimensions",
                                "differences",
                                "suggestions",
                                "reasoning",
                            ],
                            "additionalProperties": False,
                        },
                    },
                ),
            )

        assert result.is_ok
        assert json.loads(result.value.content)["dimensions"] == {
            "coverage": 0.88,
            "ux": 0.91,
        }
        dimensions_schema = seen_schema["properties"]["dimensions"]  # type: ignore[index]
        assert dimensions_schema["type"] == "array"  # type: ignore[index]

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

    @pytest.mark.asyncio
    async def test_complete_streams_events_incrementally_and_times_out_once(self) -> None:
        """Timeout should terminate the child while preserving streamed partial events."""
        callback_events: list[tuple[str, str]] = []
        create_calls = 0
        process_holder: dict[str, _FakeProcess] = {}

        def callback(message_type: str, content: str) -> None:
            callback_events.append((message_type, content))

        adapter = CodexCliLLMAdapter(
            cli_path="codex",
            on_message=callback,
            timeout=0.01,
            max_retries=3,
        )

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            nonlocal create_calls
            create_calls += 1
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("", encoding="utf-8")
            process = _FakeProcess(
                stdout=json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "reasoning", "text": "Still working..."},
                    }
                )
                + "\n",
                returncode=124,
                wait_forever=True,
                read_size=5,
            )
            process_holder["process"] = process
            return process

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Analyze dependencies.")],
                CompletionConfig(model="default"),
            )

        assert result.is_err
        assert result.error.details["timed_out"] is True
        assert create_calls == 1
        assert callback_events == [("thinking", "Still working...")]
        assert process_holder["process"].terminated or process_holder["process"].killed

    def test_build_command_does_not_include_prompt_as_positional_arg(self) -> None:
        """Prompt is fed via stdin, not as a positional CLI argument."""
        adapter = CodexCliLLMAdapter(cli_path="codex", cwd="/tmp/project")

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
        )

        # Last element should be a flag, not user-supplied text
        assert command[-1] in ("--ephemeral", "/tmp/out.txt") or command[-1].startswith("--")

    @pytest.mark.asyncio
    async def test_collect_stream_lines_rejects_unbounded_capture(self) -> None:
        """The shared stream collector should fail once cumulative capture exceeds its cap."""
        stream = _FakeStream("line-1\nline-2\n")

        with pytest.raises(ProviderError, match="stream capture exceeded"):
            await collect_stream_lines(stream, max_total_bytes=8)

    @pytest.mark.asyncio
    async def test_complete_returns_provider_error_when_stderr_capture_overflows(self) -> None:
        """Adapter converts stream-capture guard trips into ProviderError results."""
        adapter = CodexCliLLMAdapter(cli_path="codex")

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("", encoding="utf-8")
            return _FakeProcess(stderr="overflow\n", returncode=0)

        with (
            patch(
                "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ),
            patch(
                "ouroboros.providers.codex_cli_adapter.collect_stream_lines",
                side_effect=ProviderError(
                    message="Codex CLI stream capture exceeded 8 bytes",
                    provider="codex_cli",
                    details={"capture_limit_bytes": 8, "overflow_stage": "stream_capture"},
                ),
            ),
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Do the thing.")],
                CompletionConfig(model="default"),
            )

        assert result.is_err
        assert result.error.provider == "codex_cli"
        assert result.error.details["overflow_stage"] == "stream_capture"
        assert result.error.details["capture_limit_bytes"] == 8

    @pytest.mark.asyncio
    async def test_complete_surfaces_stdout_error_events_on_nonzero_exit(self) -> None:
        """Codex emits in-flight failures as JSONL on stdout — those messages must
        reach ProviderError.message and details, not just the static stderr banner.

        Regression for #560: previously the adapter forwarded only stderr so all
        codex failures looked identical regardless of root cause.
        """
        adapter = CodexCliLLMAdapter(cli_path="codex")

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("", encoding="utf-8")
            return _FakeProcess(
                stdout="\n".join(
                    [
                        json.dumps({"type": "thread.started", "thread_id": "t1"}),
                        json.dumps({"type": "turn.started"}),
                        json.dumps(
                            {
                                "type": "error",
                                "message": "Reconnecting... 1/5 (502 Bad Gateway)",
                            }
                        ),
                        json.dumps(
                            {
                                "type": "turn.failed",
                                "error": {"message": "502 Bad Gateway final"},
                            }
                        ),
                    ]
                ),
                stderr="Reading prompt from stdin...",
                returncode=1,
            )

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Reflect on the run.")],
                CompletionConfig(model="default"),
            )

        assert result.is_err
        assert result.error.provider == "codex_cli"
        assert "502 Bad Gateway final" in result.error.message
        stdout_errors = result.error.details["stdout_errors"]
        assert len(stdout_errors) == 2
        assert "Reconnecting... 1/5" in stdout_errors[0]
        assert "502 Bad Gateway final" in stdout_errors[1]
        assert result.error.details["stderr"] == "Reading prompt from stdin..."

    @pytest.mark.asyncio
    async def test_legacy_complete_surfaces_stdout_error_events_on_nonzero_exit(
        self,
    ) -> None:
        """The communicate() fallback reports stdout JSONL errors like the streaming path."""
        adapter = CodexCliLLMAdapter(cli_path="codex")
        process_holder: dict[str, _LegacyFakeProcess] = {}

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _LegacyFakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("", encoding="utf-8")
            process = _LegacyFakeProcess(
                stdout="\n".join(
                    [
                        json.dumps({"type": "thread.started", "thread_id": "legacy-thread"}),
                        json.dumps(
                            {
                                "type": "error",
                                "message": "legacy transient failure",
                            }
                        ),
                        json.dumps(
                            {
                                "type": "turn.failed",
                                "error": {"message": "legacy final failure"},
                            }
                        ),
                    ]
                ),
                stderr="Reading prompt from stdin...",
                returncode=1,
            )
            process_holder["process"] = process
            return process

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Reflect on the legacy run.")],
                CompletionConfig(model="default"),
            )

        assert result.is_err
        assert result.error.provider == "codex_cli"
        assert result.error.message == "legacy final failure"
        assert result.error.details["session_id"] == "legacy-thread"
        assert result.error.details["stdout_errors"] == [
            "legacy transient failure",
            "legacy final failure",
        ]
        assert result.error.details["stderr"] == "Reading prompt from stdin..."
        assert process_holder["process"].communicate_calls == 1

    def test_extract_stdout_errors_returns_messages_in_arrival_order(self) -> None:
        """Helper extracts only error/turn.failed events and preserves order."""
        adapter = CodexCliLLMAdapter(cli_path="codex")
        stdout_lines = [
            json.dumps({"type": "thread.started", "thread_id": "t1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "error", "message": "first transient"}),
            json.dumps({"type": "item.completed", "item": {"text": "ignored"}}),
            json.dumps({"type": "error", "message": "second transient"}),
            json.dumps({"type": "turn.failed", "error": {"message": "final fatal"}}),
            "not-a-json-line",
        ]
        errors = adapter._extract_stdout_errors(stdout_lines)
        assert errors == ["first transient", "second transient", "final fatal"]

    def test_extract_stdout_errors_handles_empty_and_malformed(self) -> None:
        """Empty input and malformed events do not crash."""
        adapter = CodexCliLLMAdapter(cli_path="codex")
        assert adapter._extract_stdout_errors([]) == []
        assert adapter._extract_stdout_errors([json.dumps({"type": "error"})]) == []
        assert adapter._extract_stdout_errors(
            [json.dumps({"type": "turn.failed", "error": "string fatal"})]
        ) == ["string fatal"]


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
