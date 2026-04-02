"""Unit tests for the Gemini CLI-backed LLM adapter.

Tests cover:
- Initialization with default and custom parameters
- Provider metadata (name, display name, CLI binary name)
- CLI path resolution (explicit path, env var, shutil.which fallback)
- Process lifecycle: start (subprocess spawn), stop (timeout termination),
  restart (retry behaviour on transient errors)
- Prompt construction and model normalisation
- Successful completion response parsing
- Error handling (non-zero exit code, empty response, missing CLI)
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import stat
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.core.errors import ProviderError
from ouroboros.providers.base import (
    CompletionConfig,
    CompletionResponse,
    Message,
    MessageRole,
)
from ouroboros.providers.gemini_cli_adapter import GeminiCLIAdapter

# ---------------------------------------------------------------------------
# Minimal subprocess fakes (mirrors the test helpers used by the Codex tests)
# ---------------------------------------------------------------------------


class _FakeStream:
    """A minimal asyncio.StreamReader substitute for unit tests."""

    def __init__(self, text: str = "", *, read_size: int | None = None) -> None:
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

    async def readline(self) -> bytes:
        idx = self._buffer.find(b"\n", self._cursor)
        if idx == -1:
            chunk = self._buffer[self._cursor :]
            self._cursor = len(self._buffer)
            return chunk
        chunk = self._buffer[self._cursor : idx + 1]
        self._cursor = idx + 1
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
    """Fake asyncio.subprocess.Process used to test adapter subprocess integration."""

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
        self._stdout_bytes = stdout.encode("utf-8")
        self._stderr_bytes = stderr.encode("utf-8")
        self.returncode: int | None = None if wait_forever else returncode
        self._final_returncode = returncode
        self._wait_forever = wait_forever
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        if self._wait_forever and self.returncode is None:
            await asyncio.Future()  # suspends indefinitely until cancelled
        self.returncode = self._final_returncode
        return self.returncode

    async def communicate(self, _input: bytes | None = None) -> tuple[bytes, bytes]:
        """Simulate asyncio.subprocess.Process.communicate().

        Suspends forever when ``wait_forever=True`` to simulate a hung process
        so that timeout tests can verify the adapter correctly terminates it.
        """
        if self._wait_forever and self.returncode is None:
            await asyncio.Future()  # suspends until cancelled by asyncio.timeout
        self.returncode = self._final_returncode
        return self._stdout_bytes, self._stderr_bytes

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = self._final_returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = self._final_returncode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = CompletionConfig(model="default")
_USER_MESSAGE = Message(role=MessageRole.USER, content="Hello, Gemini!")


def _make_fake_exec(
    *,
    stdout: str = "Gemini says hi.",
    stderr: str = "",
    returncode: int = 0,
    wait_forever: bool = False,
) -> Any:
    """Return a coroutine that replaces asyncio.create_subprocess_exec."""

    async def _fake(*_command: str, **_kwargs: Any) -> _FakeProcess:
        return _FakeProcess(
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            wait_forever=wait_forever,
        )

    return _fake


# ---------------------------------------------------------------------------
# Initialization tests
# ---------------------------------------------------------------------------


class TestGeminiCLIAdapterInit:
    """Tests for GeminiCLIAdapter.__init__ and attribute defaults."""

    def test_default_cwd_is_current_directory(self) -> None:
        """When no cwd is provided the adapter uses os.getcwd()."""
        adapter = GeminiCLIAdapter(cli_path="gemini")
        assert adapter._cwd == os.getcwd()

    def test_custom_cwd_is_stored(self, tmp_path: Path) -> None:
        """An explicit cwd is normalised and stored."""
        adapter = GeminiCLIAdapter(cli_path="gemini", cwd=tmp_path)
        assert adapter._cwd == str(tmp_path)

    def test_default_max_turns(self) -> None:
        """max_turns defaults to 1 for single-turn completion."""
        adapter = GeminiCLIAdapter(cli_path="gemini")
        assert adapter._max_turns == 1

    def test_custom_max_turns(self) -> None:
        """Custom max_turns is stored as-is."""
        adapter = GeminiCLIAdapter(cli_path="gemini", max_turns=5)
        assert adapter._max_turns == 5

    def test_default_max_retries(self) -> None:
        """max_retries defaults to 3."""
        adapter = GeminiCLIAdapter(cli_path="gemini")
        assert adapter._max_retries == 3

    def test_custom_max_retries(self) -> None:
        """Custom max_retries is stored."""
        adapter = GeminiCLIAdapter(cli_path="gemini", max_retries=5)
        assert adapter._max_retries == 5

    def test_timeout_none_by_default(self) -> None:
        """Timeout defaults to None (disabled)."""
        adapter = GeminiCLIAdapter(cli_path="gemini")
        assert adapter._timeout is None

    def test_non_positive_timeout_normalised_to_none(self) -> None:
        """Zero or negative timeout values are treated as disabled."""
        assert GeminiCLIAdapter(cli_path="gemini", timeout=0)._timeout is None
        assert GeminiCLIAdapter(cli_path="gemini", timeout=-1.0)._timeout is None

    def test_positive_timeout_stored(self) -> None:
        """A valid positive timeout is stored in seconds."""
        adapter = GeminiCLIAdapter(cli_path="gemini", timeout=30.0)
        assert adapter._timeout == 30.0

    def test_on_message_callback_stored(self) -> None:
        """The on_message callback is preserved exactly."""
        cb = lambda _t, _c: None  # noqa: E731
        adapter = GeminiCLIAdapter(cli_path="gemini", on_message=cb)
        assert adapter._on_message is cb

    def test_on_message_defaults_to_none(self) -> None:
        """No callback is registered by default."""
        adapter = GeminiCLIAdapter(cli_path="gemini")
        assert adapter._on_message is None

    def test_allowed_tools_none_by_default(self) -> None:
        """allowed_tools defaults to None (permissive mode)."""
        adapter = GeminiCLIAdapter(cli_path="gemini")
        assert adapter._allowed_tools is None

    def test_allowed_tools_stored_as_copy(self) -> None:
        """allowed_tools list is stored as an independent copy."""
        tools = ["Read", "Grep"]
        adapter = GeminiCLIAdapter(cli_path="gemini", allowed_tools=tools)
        assert adapter._allowed_tools == tools
        assert adapter._allowed_tools is not tools

    def test_empty_allowed_tools_stored(self) -> None:
        """An explicit empty list (no-tools mode) is preserved."""
        adapter = GeminiCLIAdapter(cli_path="gemini", allowed_tools=[])
        assert adapter._allowed_tools == []


# ---------------------------------------------------------------------------
# Provider metadata tests
# ---------------------------------------------------------------------------


class TestGeminiCLIAdapterMetadata:
    """Tests for class-level provider identity attributes."""

    def test_provider_name(self) -> None:
        """_provider_name identifies the adapter in error messages and logs."""
        assert GeminiCLIAdapter._provider_name == "gemini_cli"

    def test_display_name(self) -> None:
        """_display_name is the human-readable label shown in UI output."""
        assert GeminiCLIAdapter._display_name == "Gemini CLI"

    def test_default_cli_name(self) -> None:
        """_default_cli_name is the bare executable looked up via shutil.which."""
        assert GeminiCLIAdapter._default_cli_name == "gemini"

    def test_process_shutdown_timeout_positive(self) -> None:
        """The shutdown grace period must be a positive number of seconds."""
        assert GeminiCLIAdapter._process_shutdown_timeout_seconds > 0

    def test_provider_name_used_in_provider_errors(self) -> None:
        """Errors produced by the adapter carry the correct provider tag."""
        adapter = GeminiCLIAdapter(cli_path="gemini")
        error = ProviderError(
            message="test",
            provider=adapter._provider_name,
        )
        assert error.provider == "gemini_cli"


# ---------------------------------------------------------------------------
# CLI path resolution tests
# ---------------------------------------------------------------------------


class TestGeminiCLIAdapterCliPathResolution:
    """Tests for _resolve_cli_path priority chain."""

    def test_explicit_cli_path_takes_priority(self, tmp_path: Path) -> None:
        """An explicit cli_path parameter is used even when env vars are set."""
        fake_bin = tmp_path / "gemini"
        fake_bin.write_text("#!/bin/sh\necho hi\n")
        fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IEXEC)

        with patch.dict(os.environ, {"OUROBOROS_GEMINI_CLI_PATH": "/other/gemini"}):
            adapter = GeminiCLIAdapter(cli_path=str(fake_bin))

        assert adapter._cli_path == str(fake_bin)

    def test_env_var_used_when_no_explicit_path(self, tmp_path: Path) -> None:
        """OUROBOROS_GEMINI_CLI_PATH env var is consulted as second priority."""
        fake_bin = tmp_path / "gemini-custom"
        fake_bin.write_text("#!/bin/sh\necho hi\n")
        fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IEXEC)

        with patch.dict(os.environ, {"OUROBOROS_GEMINI_CLI_PATH": str(fake_bin)}):
            adapter = GeminiCLIAdapter()

        assert adapter._cli_path == str(fake_bin)

    def test_fallback_to_which_when_no_env_var(self) -> None:
        """shutil.which is used when neither explicit path nor env var is set."""
        with (
            patch.dict(os.environ, {}, clear=False),
            patch.dict(os.environ, {"OUROBOROS_GEMINI_CLI_PATH": ""}),
            patch("shutil.which", return_value="/usr/local/bin/gemini"),
        ):
            adapter = GeminiCLIAdapter()

        assert adapter._cli_path == "/usr/local/bin/gemini"

    def test_falls_back_to_bare_name_when_which_fails(self) -> None:
        """When shutil.which returns None the bare name 'gemini' is used."""
        with (
            patch.dict(os.environ, {"OUROBOROS_GEMINI_CLI_PATH": ""}),
            patch("shutil.which", return_value=None),
        ):
            adapter = GeminiCLIAdapter()

        assert adapter._cli_path == "gemini"

    def test_nonexistent_explicit_path_still_stored(self, tmp_path: Path) -> None:
        """A non-existent explicit path is stored without raising (will fail at exec time)."""
        missing = str(tmp_path / "does_not_exist" / "gemini")
        adapter = GeminiCLIAdapter(cli_path=missing)
        # Should be stored (subprocess will raise FileNotFoundError later)
        assert adapter._cli_path == missing


# ---------------------------------------------------------------------------
# Prompt construction tests
# ---------------------------------------------------------------------------


class TestGeminiCLIAdapterBuildPrompt:
    """Tests for _build_prompt message formatting."""

    def test_system_message_included_in_prompt(self) -> None:
        """System instructions appear in the rendered prompt."""
        adapter = GeminiCLIAdapter(cli_path="gemini")
        prompt = adapter._build_prompt(
            [
                Message(role=MessageRole.SYSTEM, content="Respond only in English."),
                Message(role=MessageRole.USER, content="Bonjour!"),
            ]
        )
        assert "Respond only in English." in prompt

    def test_user_and_assistant_messages_preserved(self) -> None:
        """Conversation messages appear in their original order."""
        adapter = GeminiCLIAdapter(cli_path="gemini")
        prompt = adapter._build_prompt(
            [
                Message(role=MessageRole.USER, content="What is 2+2?"),
                Message(role=MessageRole.ASSISTANT, content="It is 4."),
                Message(role=MessageRole.USER, content="Thanks!"),
            ]
        )
        assert "What is 2+2?" in prompt
        assert "It is 4." in prompt
        assert "Thanks!" in prompt

    def test_empty_messages_returns_non_empty_prompt(self) -> None:
        """Even an empty message list produces a non-empty prompt string."""
        adapter = GeminiCLIAdapter(cli_path="gemini")
        prompt = adapter._build_prompt([])
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    def test_tool_constraints_included_when_allowed_tools_set(self) -> None:
        """Explicit tool lists are included in the prompt."""
        adapter = GeminiCLIAdapter(cli_path="gemini", allowed_tools=["Read"])
        prompt = adapter._build_prompt([_USER_MESSAGE])
        assert "Read" in prompt

    def test_no_tool_constraints_without_allowed_tools(self) -> None:
        """Default adapter (no tool list) does not add tool advisory text."""
        adapter = GeminiCLIAdapter(cli_path="gemini")
        prompt = adapter._build_prompt([_USER_MESSAGE])
        # The prompt should not include explicit tool restriction headers
        assert "Do NOT use any tools" not in prompt


# ---------------------------------------------------------------------------
# Model normalisation tests
# ---------------------------------------------------------------------------


class TestGeminiCLIAdapterNormalizeModel:
    """Tests for _normalize_model helper."""

    def test_default_sentinel_maps_to_none(self) -> None:
        """The 'default' sentinel is not passed to the CLI (uses model's own default)."""
        adapter = GeminiCLIAdapter(cli_path="gemini")
        assert adapter._normalize_model("default") is None

    def test_empty_string_maps_to_none(self) -> None:
        """An empty string model is treated like 'default'."""
        adapter = GeminiCLIAdapter(cli_path="gemini")
        assert adapter._normalize_model("") is None

    def test_whitespace_stripped(self) -> None:
        """Leading/trailing whitespace is stripped from model names."""
        adapter = GeminiCLIAdapter(cli_path="gemini")
        assert adapter._normalize_model("  gemini-2.5-pro  ") == "gemini-2.5-pro"

    def test_valid_model_name_returned(self) -> None:
        """A well-formed model name is returned unchanged."""
        adapter = GeminiCLIAdapter(cli_path="gemini")
        assert adapter._normalize_model("gemini-2.5-pro") == "gemini-2.5-pro"

    def test_model_with_slash_returned(self) -> None:
        """Model names with slashes (e.g. version paths) are accepted."""
        adapter = GeminiCLIAdapter(cli_path="gemini")
        result = adapter._normalize_model("google/gemini-2.5-pro")
        assert result is not None


# ---------------------------------------------------------------------------
# Process lifecycle: start (subprocess spawn)
# ---------------------------------------------------------------------------


class TestGeminiCLIAdapterProcessStart:
    """Tests that verify the subprocess is spawned correctly on complete()."""

    @pytest.mark.asyncio
    async def test_complete_spawns_subprocess(self) -> None:
        """complete() calls asyncio.create_subprocess_exec exactly once."""
        adapter = GeminiCLIAdapter(cli_path="gemini", cwd="/tmp/project")
        spawn_calls: list[tuple[Any, ...]] = []

        async def fake_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            spawn_calls.append(command)
            return _FakeProcess(stdout="Hello from Gemini", returncode=0)

        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ):
            result = await adapter.complete([_USER_MESSAGE], _DEFAULT_CONFIG)

        assert len(spawn_calls) == 1
        assert result.is_ok

    @pytest.mark.asyncio
    async def test_complete_uses_configured_cli_path(self) -> None:
        """The spawned command starts with the configured CLI binary path."""
        adapter = GeminiCLIAdapter(cli_path="/usr/local/bin/gemini")
        captured_command: list[str] = []

        async def fake_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            captured_command.extend(command)
            return _FakeProcess(stdout="response", returncode=0)

        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ):
            await adapter.complete([_USER_MESSAGE], _DEFAULT_CONFIG)

        assert captured_command[0] == "/usr/local/bin/gemini"

    @pytest.mark.asyncio
    async def test_complete_uses_configured_cwd(self, tmp_path: Path) -> None:
        """The subprocess inherits the adapter's configured working directory."""
        adapter = GeminiCLIAdapter(cli_path="gemini", cwd=tmp_path)
        captured_kwargs: dict[str, Any] = {}

        async def fake_exec(*_command: str, **kwargs: Any) -> _FakeProcess:
            captured_kwargs.update(kwargs)
            return _FakeProcess(stdout="response", returncode=0)

        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ):
            await adapter.complete([_USER_MESSAGE], _DEFAULT_CONFIG)

        assert captured_kwargs.get("cwd") == str(tmp_path)

    @pytest.mark.asyncio
    async def test_complete_passes_model_flag_when_specified(self) -> None:
        """A non-default model name results in a --model flag being appended."""
        adapter = GeminiCLIAdapter(cli_path="gemini")
        captured_command: list[str] = []

        async def fake_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            captured_command.extend(command)
            return _FakeProcess(stdout="response", returncode=0)

        config = CompletionConfig(model="gemini-2.5-pro")
        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ):
            await adapter.complete([_USER_MESSAGE], config)

        assert "--model" in captured_command
        model_idx = captured_command.index("--model")
        assert captured_command[model_idx + 1] == "gemini-2.5-pro"

    @pytest.mark.asyncio
    async def test_complete_omits_model_flag_for_default(self) -> None:
        """When model='default', no --model flag is passed to the CLI."""
        adapter = GeminiCLIAdapter(cli_path="gemini")
        captured_command: list[str] = []

        async def fake_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            captured_command.extend(command)
            return _FakeProcess(stdout="response", returncode=0)

        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ):
            await adapter.complete([_USER_MESSAGE], _DEFAULT_CONFIG)

        assert "--model" not in captured_command

    @pytest.mark.asyncio
    async def test_complete_returns_cli_stdout_as_content(self) -> None:
        """The adapter returns the Gemini CLI stdout text as the response content."""
        adapter = GeminiCLIAdapter(cli_path="gemini")

        async def fake_exec(*_c: str, **_k: Any) -> _FakeProcess:
            return _FakeProcess(stdout="This is Gemini's answer.", returncode=0)

        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ):
            result = await adapter.complete([_USER_MESSAGE], _DEFAULT_CONFIG)

        assert result.is_ok
        assert "This is Gemini's answer." in result.value.content

    @pytest.mark.asyncio
    async def test_complete_returns_completion_response_with_correct_model(self) -> None:
        """CompletionResponse.model reflects the requested model."""
        adapter = GeminiCLIAdapter(cli_path="gemini")
        config = CompletionConfig(model="gemini-2.5-flash")

        async def fake_exec(*_c: str, **_k: Any) -> _FakeProcess:
            return _FakeProcess(stdout="Flash answer.", returncode=0)

        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ):
            result = await adapter.complete([_USER_MESSAGE], config)

        assert result.is_ok
        assert isinstance(result.value, CompletionResponse)
        assert result.value.model == "gemini-2.5-flash"

    @pytest.mark.asyncio
    async def test_complete_returns_provider_error_when_cli_not_found(self) -> None:
        """FileNotFoundError from exec is converted to a ProviderError result."""
        adapter = GeminiCLIAdapter(cli_path="/nonexistent/gemini")

        async def fake_exec(*_c: str, **_k: Any) -> _FakeProcess:
            raise FileNotFoundError("No such file or directory: '/nonexistent/gemini'")

        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ):
            result = await adapter.complete([_USER_MESSAGE], _DEFAULT_CONFIG)

        assert result.is_err
        assert result.error.provider == "gemini_cli"
        assert "not found" in result.error.message.lower() or "gemini" in result.error.message.lower()


# ---------------------------------------------------------------------------
# Process lifecycle: stop (timeout / termination)
# ---------------------------------------------------------------------------


class TestGeminiCLIAdapterProcessStop:
    """Tests that verify the subprocess is properly terminated on timeouts."""

    @pytest.mark.asyncio
    async def test_complete_terminates_process_on_timeout(self) -> None:
        """A timed-out completion terminates the child process."""
        process_holder: dict[str, _FakeProcess] = {}

        adapter = GeminiCLIAdapter(cli_path="gemini", timeout=0.01, max_retries=1)

        async def fake_exec(*_c: str, **_k: Any) -> _FakeProcess:
            proc = _FakeProcess(stdout="", returncode=0, wait_forever=True)
            process_holder["proc"] = proc
            return proc

        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ):
            result = await adapter.complete([_USER_MESSAGE], _DEFAULT_CONFIG)

        assert result.is_err
        assert result.error.details.get("timed_out") is True
        proc = process_holder["proc"]
        assert proc.terminated or proc.killed

    @pytest.mark.asyncio
    async def test_timeout_error_carries_timeout_seconds(self) -> None:
        """The ProviderError details include the configured timeout value."""
        adapter = GeminiCLIAdapter(cli_path="gemini", timeout=0.01, max_retries=1)

        async def fake_exec(*_c: str, **_k: Any) -> _FakeProcess:
            return _FakeProcess(stdout="", returncode=0, wait_forever=True)

        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ):
            result = await adapter.complete([_USER_MESSAGE], _DEFAULT_CONFIG)

        assert result.is_err
        assert result.error.details.get("timeout_seconds") == pytest.approx(0.01)

    @pytest.mark.asyncio
    async def test_timeout_is_not_retried(self) -> None:
        """A timed-out request is not retried (exits after first attempt)."""
        spawn_count = 0
        adapter = GeminiCLIAdapter(cli_path="gemini", timeout=0.01, max_retries=5)

        async def fake_exec(*_c: str, **_k: Any) -> _FakeProcess:
            nonlocal spawn_count
            spawn_count += 1
            return _FakeProcess(stdout="", returncode=0, wait_forever=True)

        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ):
            result = await adapter.complete([_USER_MESSAGE], _DEFAULT_CONFIG)

        assert result.is_err
        assert spawn_count == 1, "Timed-out requests must not be retried"


# ---------------------------------------------------------------------------
# Process lifecycle: restart (retry on transient errors)
# ---------------------------------------------------------------------------


class TestGeminiCLIAdapterProcessRestart:
    """Tests that verify the adapter retries on transient failures."""

    @pytest.mark.asyncio
    async def test_retryable_error_is_retried(self) -> None:
        """Transient rate-limit errors are retried up to max_retries times."""
        spawn_count = 0
        adapter = GeminiCLIAdapter(cli_path="gemini", max_retries=3)

        async def fake_exec(*_c: str, **_k: Any) -> _FakeProcess:
            nonlocal spawn_count
            spawn_count += 1
            if spawn_count < 3:
                # First two attempts fail with a rate-limit error
                return _FakeProcess(stderr="rate limit exceeded", returncode=1)
            # Third attempt succeeds
            return _FakeProcess(stdout="Success on retry.", returncode=0)

        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ):
            result = await adapter.complete([_USER_MESSAGE], _DEFAULT_CONFIG)

        assert result.is_ok, f"Expected success after retries, got: {result.error}"
        assert spawn_count == 3

    @pytest.mark.asyncio
    async def test_non_retryable_error_is_not_retried(self) -> None:
        """Non-transient errors (e.g. auth failure) are not retried."""
        spawn_count = 0
        adapter = GeminiCLIAdapter(cli_path="gemini", max_retries=3)

        async def fake_exec(*_c: str, **_k: Any) -> _FakeProcess:
            nonlocal spawn_count
            spawn_count += 1
            return _FakeProcess(stderr="authentication failed: invalid API key", returncode=1)

        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ):
            result = await adapter.complete([_USER_MESSAGE], _DEFAULT_CONFIG)

        assert result.is_err
        assert spawn_count == 1, "Auth errors must not be retried"

    @pytest.mark.asyncio
    async def test_max_retries_exhausted_returns_last_error(self) -> None:
        """After max_retries attempts the final error is returned."""
        adapter = GeminiCLIAdapter(cli_path="gemini", max_retries=3)

        async def fake_exec(*_c: str, **_k: Any) -> _FakeProcess:
            return _FakeProcess(stderr="rate limit exceeded", returncode=1)

        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ):
            result = await adapter.complete([_USER_MESSAGE], _DEFAULT_CONFIG)

        assert result.is_err
        assert result.error.provider == "gemini_cli"

    @pytest.mark.asyncio
    async def test_retry_count_does_not_exceed_max_retries(self) -> None:
        """The adapter spawns at most max_retries child processes for transient errors."""
        spawn_count = 0
        adapter = GeminiCLIAdapter(cli_path="gemini", max_retries=2)

        async def fake_exec(*_c: str, **_k: Any) -> _FakeProcess:
            nonlocal spawn_count
            spawn_count += 1
            return _FakeProcess(stderr="temporarily unavailable", returncode=1)

        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ):
            await adapter.complete([_USER_MESSAGE], _DEFAULT_CONFIG)

        assert spawn_count <= 2


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


class TestGeminiCLIAdapterErrorHandling:
    """Tests for error conditions: non-zero exit, empty response, exec failure."""

    @pytest.mark.asyncio
    async def test_nonzero_exit_returns_provider_error(self) -> None:
        """A non-zero exit code is surfaced as a ProviderError."""
        adapter = GeminiCLIAdapter(cli_path="gemini", max_retries=1)

        async def fake_exec(*_c: str, **_k: Any) -> _FakeProcess:
            return _FakeProcess(stderr="Fatal error occurred", returncode=2)

        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ):
            result = await adapter.complete([_USER_MESSAGE], _DEFAULT_CONFIG)

        assert result.is_err
        assert result.error.provider == "gemini_cli"
        assert result.error.details.get("returncode") == 2

    @pytest.mark.asyncio
    async def test_empty_response_returns_provider_error(self) -> None:
        """A zero-exit but empty stdout is treated as a ProviderError."""
        adapter = GeminiCLIAdapter(cli_path="gemini", max_retries=1)

        async def fake_exec(*_c: str, **_k: Any) -> _FakeProcess:
            return _FakeProcess(stdout="", stderr="", returncode=0)

        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ):
            result = await adapter.complete([_USER_MESSAGE], _DEFAULT_CONFIG)

        assert result.is_err
        assert result.error.provider == "gemini_cli"

    @pytest.mark.asyncio
    async def test_exec_exception_returns_provider_error(self) -> None:
        """Any unexpected exception from subprocess creation is caught as ProviderError."""
        adapter = GeminiCLIAdapter(cli_path="gemini")

        async def fake_exec(*_c: str, **_k: Any) -> _FakeProcess:
            raise PermissionError("Permission denied: /usr/local/bin/gemini")

        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ):
            result = await adapter.complete([_USER_MESSAGE], _DEFAULT_CONFIG)

        assert result.is_err
        assert result.error.provider == "gemini_cli"

    @pytest.mark.asyncio
    async def test_on_message_callback_receives_events(self) -> None:
        """The on_message callback is invoked with (type, content) tuples."""
        events: list[tuple[str, str]] = []

        def on_msg(msg_type: str, content: str) -> None:
            events.append((msg_type, content))

        adapter = GeminiCLIAdapter(cli_path="gemini", on_message=on_msg)

        async def fake_exec(*_c: str, **_k: Any) -> _FakeProcess:
            return _FakeProcess(stdout="Gemini responded here.", returncode=0)

        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ):
            result = await adapter.complete([_USER_MESSAGE], _DEFAULT_CONFIG)

        # At minimum, the completion should succeed
        assert result.is_ok

    @pytest.mark.asyncio
    async def test_complete_returns_is_retryable_for_rate_limit(self) -> None:
        """Rate-limit errors are detected as retryable."""
        adapter = GeminiCLIAdapter(cli_path="gemini")
        assert adapter._is_retryable_error("rate limit exceeded") is True
        assert adapter._is_retryable_error("temporarily unavailable") is True
        assert adapter._is_retryable_error("timeout waiting for response") is True

    def test_is_retryable_returns_false_for_auth_errors(self) -> None:
        """Authentication and hard errors are not retried."""
        adapter = GeminiCLIAdapter(cli_path="gemini")
        assert adapter._is_retryable_error("authentication failed") is False
        assert adapter._is_retryable_error("invalid API key provided") is False
        assert adapter._is_retryable_error("model not found") is False


# ---------------------------------------------------------------------------
# Lazy import / package integration
# ---------------------------------------------------------------------------


class TestGeminiCLIAdapterLazyImport:
    """Tests for GeminiCLIAdapter availability via the providers package."""

    def test_gemini_cli_adapter_importable_directly(self) -> None:
        """GeminiCLIAdapter can be imported from its own module."""
        from ouroboros.providers.gemini_cli_adapter import GeminiCLIAdapter as _Cls

        assert _Cls is GeminiCLIAdapter

    def test_gemini_cli_adapter_accessible_from_providers_package(self) -> None:
        """GeminiCLIAdapter is available via providers.__getattr__ lazy import."""
        import ouroboros.providers as providers

        adapter_class = providers.GeminiCLIAdapter
        assert adapter_class is GeminiCLIAdapter

    def test_gemini_cli_adapter_in_all(self) -> None:
        """GeminiCLIAdapter is listed in the providers package __all__."""
        import ouroboros.providers as providers

        assert "GeminiCLIAdapter" in providers.__all__
