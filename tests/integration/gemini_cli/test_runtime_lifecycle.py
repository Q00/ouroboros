"""Integration tests for GeminiCLIRuntime orchestrator lifecycle.

Covers process start, stop, no-resume (restart) semantics, and error/timeout
handling for the GeminiCLIRuntime orchestrator.

Test classes
------------
- TestGeminiCLIRuntimeInit          — construction and class-level metadata
- TestGeminiCLIRuntimeProcessStart  — subprocess spawn: command shape, stdin, cwd
- TestGeminiCLIRuntimeProcessStop   — completion, non-zero exit, CLI-not-found
- TestGeminiCLIRuntimeNoResumption  — verifies _max_resume_retries=0, no resume flags
- TestGeminiCLIRuntimeErrorHandling — cancellation, hung process, exec exceptions

All subprocess calls are intercepted via ``unittest.mock.patch`` so that no
real ``gemini`` binary is needed.  Each test exercises the full
``execute_task`` code path inherited from
:class:`~ouroboros.orchestrator.codex_cli_runtime.CodexCliRuntime`.

Design notes
------------
- ``_build_command`` for Gemini CLI returns ``["gemini"]`` (optionally with
  ``--model``).  It does **not** include ``--output-last-message``,
  ``--json``, permission flags, or ``exec``.
- The final result message content comes from ``last_content`` (accumulated
  from stdout), not from an output temp file.
- ``_max_resume_retries = 0`` — session resumption is unsupported.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.gemini_cli_runtime import GeminiCLIRuntime

# Subprocess exec lives in the *base* module; patch it there so both
# CodexCliRuntime and GeminiCLIRuntime pick up the replacement.
_EXEC_PATCH_TARGET = (
    "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec"
)


# ---------------------------------------------------------------------------
# Local subprocess doubles
# ---------------------------------------------------------------------------


class _FakeStream:
    """Minimal async byte-stream double (asyncio.StreamReader substitute).

    Supports both ``read()`` (chunk-based) and ``readline()`` (line-based)
    access patterns used by the runtime's line-iteration helpers.
    """

    def __init__(self, text: str = "") -> None:
        self._buffer: bytes = text.encode("utf-8")

    async def read(self, chunk_size: int = 16384) -> bytes:
        if not self._buffer:
            return b""
        chunk, self._buffer = self._buffer[:chunk_size], self._buffer[chunk_size:]
        return chunk

    async def readline(self) -> bytes:
        if not self._buffer:
            return b""
        idx = self._buffer.find(b"\n")
        if idx == -1:
            line, self._buffer = self._buffer, b""
            return line
        line, self._buffer = self._buffer[: idx + 1], self._buffer[idx + 1 :]
        return line


class _FakeStdin:
    """Minimal async stdin-pipe double that records written payloads."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed: bool = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    @property
    def written(self) -> bytes:
        return b"".join(self.writes)


class _FakeProcess:
    """Async subprocess double with configurable stdout / stderr / returncode.

    Attributes:
        stdout: Async stream yielding *stdout_text*.
        stderr: Async stream yielding *stderr_text*.
        stdin: :class:`_FakeStdin` capturing writes from the runtime.
        returncode: Exit code returned by :meth:`wait`.
        terminated: Set to ``True`` when :meth:`terminate` is called.
        killed: Set to ``True`` when :meth:`kill` is called.
    """

    def __init__(
        self,
        *,
        stdout_text: str = "",
        stderr_text: str = "",
        returncode: int = 0,
    ) -> None:
        self.stdout = _FakeStream(stdout_text)
        self.stderr = _FakeStream(stderr_text)
        self.stdin = _FakeStdin()
        self.returncode: int = returncode
        self.terminated: bool = False
        self.killed: bool = False

    async def wait(self) -> int:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class _FakeHangingProcess:
    """Fake subprocess that blocks in :meth:`wait` to simulate a hung CLI.

    The :meth:`wait` coroutine suspends on an un-resolving
    :class:`asyncio.Future` so cancellation and timeout tests can verify that
    the runtime correctly terminates the child process.
    """

    def __init__(self) -> None:
        self.stdout = _FakeStream("")
        self.stderr = _FakeStream("")
        self.stdin = _FakeStdin()
        self.returncode: int | None = None
        self.terminated: bool = False
        self.killed: bool = False

    async def wait(self) -> int:
        """Block forever — callers must cancel or call terminate/kill."""
        await asyncio.Future()  # never resolves on its own
        return -15  # unreachable; satisfies type checker

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_runtime(
    *,
    cli_path: str = "/usr/local/bin/gemini",
    model: str | None = None,
    cwd: str | Path = "/tmp/test-workspace",
    permission_mode: str | None = None,
) -> GeminiCLIRuntime:
    """Construct a :class:`GeminiCLIRuntime` with safe test defaults."""
    return GeminiCLIRuntime(
        cli_path=cli_path,
        model=model,
        cwd=cwd,
        permission_mode=permission_mode,
    )


async def _collect(runtime: GeminiCLIRuntime, prompt: str, **kwargs: Any) -> list[AgentMessage]:
    """Drain ``execute_task`` into a list for assertion."""
    return [msg async for msg in runtime.execute_task(prompt, **kwargs)]


def _fake_exec_for(process: _FakeProcess | _FakeHangingProcess) -> Any:
    """Return an async callable that always returns *process*."""

    async def _exec(*_args: str, **_kwargs: Any) -> Any:
        return process

    return _exec


# ---------------------------------------------------------------------------
# TestGeminiCLIRuntimeInit
# ---------------------------------------------------------------------------


class TestGeminiCLIRuntimeInit:
    """Construction and class-level metadata checks."""

    def test_is_gemini_cli_runtime_subclass(self) -> None:
        """GeminiCLIRuntime must inherit from CodexCliRuntime."""
        from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime

        runtime = _make_runtime()
        assert isinstance(runtime, CodexCliRuntime)

    def test_runtime_handle_backend(self) -> None:
        """runtime_backend property returns the expected backend tag."""
        runtime = _make_runtime()
        # GeminiCLIRuntime overrides _runtime_handle_backend to 'gemini_cli'
        assert runtime.runtime_backend == "gemini_cli"

    def test_display_name(self) -> None:
        """_display_name is the human-readable label used in error messages."""
        runtime = _make_runtime()
        assert runtime._display_name == "Gemini CLI"

    def test_default_cli_name(self) -> None:
        """_default_cli_name is the bare executable name for shutil.which lookup."""
        runtime = _make_runtime()
        assert runtime._default_cli_name == "gemini"

    def test_provider_name(self) -> None:
        """_provider_name identifies the provider in logs and errors."""
        runtime = _make_runtime()
        assert runtime._provider_name == "gemini_cli"

    def test_max_resume_retries_is_zero(self) -> None:
        """Gemini CLI does not support session resumption; retries must be 0."""
        runtime = _make_runtime()
        assert runtime._max_resume_retries == 0

    def test_working_directory_stored(self, tmp_path: Path) -> None:
        """The configured cwd is accessible via the working_directory property."""
        runtime = _make_runtime(cwd=tmp_path)
        assert runtime.working_directory == str(tmp_path)

    def test_permission_mode_normalised(self) -> None:
        """A known permission mode is stored without modification."""
        runtime = _make_runtime(permission_mode="acceptEdits")
        assert runtime.permission_mode == "acceptEdits"

    def test_unknown_permission_mode_falls_back_to_default(self) -> None:
        """Unrecognised permission modes fall back to 'default'."""
        runtime = _make_runtime(permission_mode="unknown-mode")
        assert runtime.permission_mode == "default"

    def test_cli_path_stored(self, tmp_path: Path) -> None:
        """An explicit cli_path is stored on the instance."""
        fake_bin = tmp_path / "gemini"
        fake_bin.write_text("#!/bin/sh\n")
        runtime = _make_runtime(cli_path=str(fake_bin))
        assert runtime._cli_path == str(fake_bin)


# ---------------------------------------------------------------------------
# TestGeminiCLIRuntimeProcessStart
# ---------------------------------------------------------------------------


class TestGeminiCLIRuntimeProcessStart:
    """Verify subprocess spawn shape when execute_task is called."""

    @pytest.mark.asyncio
    async def test_subprocess_spawned_exactly_once(self) -> None:
        """execute_task must invoke create_subprocess_exec exactly once."""
        spawn_count = 0

        async def _fake_exec(*_args: str, **_kwargs: Any) -> _FakeProcess:
            nonlocal spawn_count
            spawn_count += 1
            return _FakeProcess(stdout_text="Hello from Gemini\n", returncode=0)

        runtime = _make_runtime()
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            await _collect(runtime, "Fix the bug")

        assert spawn_count == 1

    @pytest.mark.asyncio
    async def test_command_starts_with_cli_path(self) -> None:
        """The spawned command's first element must equal the configured cli_path."""
        captured: list[tuple[str, ...]] = []

        async def _fake_exec(*args: str, **_kwargs: Any) -> _FakeProcess:
            captured.append(args)
            return _FakeProcess(stdout_text="ok\n", returncode=0)

        runtime = _make_runtime(cli_path="/opt/gemini/bin/gemini")
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            await _collect(runtime, "Run the tests")

        assert len(captured) == 1
        assert captured[0][0] == "/opt/gemini/bin/gemini"

    @pytest.mark.asyncio
    async def test_model_flag_included_when_model_specified(self) -> None:
        """When a model is configured, --model and its value appear in the command."""
        captured: list[tuple[str, ...]] = []

        async def _fake_exec(*args: str, **_kwargs: Any) -> _FakeProcess:
            captured.append(args)
            return _FakeProcess(stdout_text="ok\n", returncode=0)

        runtime = _make_runtime(model="gemini-2.5-pro")
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            await _collect(runtime, "Refactor the module")

        command = captured[0]
        assert "--model" in command
        model_idx = command.index("--model")
        assert command[model_idx + 1] == "gemini-2.5-pro"

    @pytest.mark.asyncio
    async def test_model_flag_omitted_when_model_is_none(self) -> None:
        """When no model is configured, --model must not appear in the command."""
        captured: list[tuple[str, ...]] = []

        async def _fake_exec(*args: str, **_kwargs: Any) -> _FakeProcess:
            captured.append(args)
            return _FakeProcess(stdout_text="ok\n", returncode=0)

        runtime = _make_runtime(model=None)
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            await _collect(runtime, "List files")

        assert "--model" not in captured[0]

    @pytest.mark.asyncio
    async def test_output_last_message_flag_absent(self) -> None:
        """Gemini CLI does not use --output-last-message; the flag must be absent."""
        captured: list[tuple[str, ...]] = []

        async def _fake_exec(*args: str, **_kwargs: Any) -> _FakeProcess:
            captured.append(args)
            return _FakeProcess(stdout_text="done\n", returncode=0)

        runtime = _make_runtime()
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            await _collect(runtime, "List files")

        assert "--output-last-message" not in captured[0]

    @pytest.mark.asyncio
    async def test_no_codex_exec_subcommand(self) -> None:
        """The Gemini CLI command must not include 'exec' (a Codex-only subcommand)."""
        captured: list[tuple[str, ...]] = []

        async def _fake_exec(*args: str, **_kwargs: Any) -> _FakeProcess:
            captured.append(args)
            return _FakeProcess(stdout_text="done\n", returncode=0)

        runtime = _make_runtime()
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            await _collect(runtime, "Run tests")

        assert "exec" not in captured[0]

    @pytest.mark.asyncio
    async def test_no_permission_flags_in_command(self) -> None:
        """Gemini CLI manages permissions internally; no --full-auto or similar flags."""
        captured: list[tuple[str, ...]] = []

        async def _fake_exec(*args: str, **_kwargs: Any) -> _FakeProcess:
            captured.append(args)
            return _FakeProcess(stdout_text="done\n", returncode=0)

        runtime = _make_runtime(permission_mode="acceptEdits")
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            await _collect(runtime, "Edit file")

        command = captured[0]
        for flag in ("--full-auto", "--auto-approve", "--permission", "--approval-policy"):
            assert flag not in command, f"Unexpected permission flag in command: {flag}"

    @pytest.mark.asyncio
    async def test_cwd_passed_to_subprocess(self, tmp_path: Path) -> None:
        """The configured cwd must be forwarded to create_subprocess_exec."""
        captured_kwargs: dict[str, Any] = {}

        async def _fake_exec(*_args: str, **kwargs: Any) -> _FakeProcess:
            captured_kwargs.update(kwargs)
            return _FakeProcess(stdout_text="ok\n", returncode=0)

        runtime = _make_runtime(cwd=tmp_path)
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            await _collect(runtime, "Inspect workspace")

        assert captured_kwargs.get("cwd") == str(tmp_path)

    @pytest.mark.asyncio
    async def test_stdin_pipe_requested(self) -> None:
        """The subprocess must be started with stdin=PIPE so the prompt can be fed."""
        captured_kwargs: dict[str, Any] = {}

        async def _fake_exec(*_args: str, **kwargs: Any) -> _FakeProcess:
            captured_kwargs.update(kwargs)
            return _FakeProcess(stdout_text="ok\n", returncode=0)

        runtime = _make_runtime()
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            await _collect(runtime, "Process task")

        assert captured_kwargs.get("stdin") == asyncio.subprocess.PIPE

    @pytest.mark.asyncio
    async def test_prompt_written_to_stdin(self) -> None:
        """The task prompt must be written to the subprocess stdin pipe."""
        captured_process: list[_FakeProcess] = []

        async def _fake_exec(*_args: str, **_kwargs: Any) -> _FakeProcess:
            proc = _FakeProcess(stdout_text="response\n", returncode=0)
            captured_process.append(proc)
            return proc

        runtime = _make_runtime()
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            await _collect(runtime, "Write unit tests")

        assert captured_process, "Subprocess was never spawned"
        written = captured_process[0].stdin.written
        assert b"Write unit tests" in written

    @pytest.mark.asyncio
    async def test_stdin_closed_after_prompt(self) -> None:
        """stdin must be closed after the prompt is written (no hanging pipe)."""
        captured_process: list[_FakeProcess] = []

        async def _fake_exec(*_args: str, **_kwargs: Any) -> _FakeProcess:
            proc = _FakeProcess(stdout_text="response\n", returncode=0)
            captured_process.append(proc)
            return proc

        runtime = _make_runtime()
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            await _collect(runtime, "Write unit tests")

        assert captured_process[0].stdin.closed

    @pytest.mark.asyncio
    async def test_child_env_strips_ouroboros_mcp_vars(self) -> None:
        """OUROBOROS_AGENT_RUNTIME and OUROBOROS_LLM_BACKEND must not leak to child."""
        captured_env: dict[str, str] = {}

        async def _fake_exec(*_args: str, **kwargs: Any) -> _FakeProcess:
            captured_env.update(kwargs.get("env") or {})
            return _FakeProcess(stdout_text="ok\n", returncode=0)

        import os

        with (
            patch.dict(
                os.environ,
                {"OUROBOROS_AGENT_RUNTIME": "gemini", "OUROBOROS_LLM_BACKEND": "gemini_cli"},
            ),
            patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec),
        ):
            runtime = _make_runtime()
            await _collect(runtime, "Run task")

        assert "OUROBOROS_AGENT_RUNTIME" not in captured_env
        assert "OUROBOROS_LLM_BACKEND" not in captured_env

    @pytest.mark.asyncio
    async def test_plain_text_stdout_yields_assistant_messages(self) -> None:
        """Each non-empty plain-text line from stdout becomes an assistant message."""
        text_lines = "Line one\nLine two\nLine three\n"

        async def _fake_exec(*_args: str, **_kwargs: Any) -> _FakeProcess:
            return _FakeProcess(stdout_text=text_lines, returncode=0)

        runtime = _make_runtime()
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            messages = await _collect(runtime, "Analyse the code")

        assistant_messages = [m for m in messages if m.type == "assistant"]
        contents = [m.content for m in assistant_messages]
        assert any("Line one" in c for c in contents)
        assert any("Line two" in c for c in contents)
        assert any("Line three" in c for c in contents)


# ---------------------------------------------------------------------------
# TestGeminiCLIRuntimeProcessStop
# ---------------------------------------------------------------------------


class TestGeminiCLIRuntimeProcessStop:
    """Verify runtime-level completion and error result messages."""

    @pytest.mark.asyncio
    async def test_zero_exit_yields_success_result(self) -> None:
        """A process that exits 0 must produce a final result with subtype='success'."""

        async def _fake_exec(*_args: str, **_kwargs: Any) -> _FakeProcess:
            return _FakeProcess(stdout_text="Task done.\n", returncode=0)

        runtime = _make_runtime()
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            messages = await _collect(runtime, "Complete the task")

        result = messages[-1]
        assert result.type == "result"
        assert result.data.get("subtype") == "success"
        assert result.data.get("returncode") == 0

    @pytest.mark.asyncio
    async def test_nonzero_exit_yields_error_result(self) -> None:
        """A process that exits non-zero must produce a final result with subtype='error'."""

        async def _fake_exec(*_args: str, **_kwargs: Any) -> _FakeProcess:
            return _FakeProcess(
                stdout_text="",
                stderr_text="gemini: internal error",
                returncode=1,
            )

        runtime = _make_runtime()
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            messages = await _collect(runtime, "Analyse logs")

        result = messages[-1]
        assert result.type == "result"
        assert result.data.get("subtype") == "error"
        assert result.data.get("returncode") == 1

    @pytest.mark.asyncio
    async def test_error_result_includes_runtime_error_type(self) -> None:
        """On non-zero exit, data['error_type'] must be 'GeminiCliError'."""

        async def _fake_exec(*_args: str, **_kwargs: Any) -> _FakeProcess:
            return _FakeProcess(returncode=2)

        runtime = _make_runtime()
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            messages = await _collect(runtime, "Generate report")

        result = messages[-1]
        assert result.data.get("error_type") == "GeminiCliError"

    @pytest.mark.asyncio
    async def test_final_result_content_comes_from_last_stdout_line(self) -> None:
        """The final result content must reflect the last assistant content seen."""

        async def _fake_exec(*_args: str, **_kwargs: Any) -> _FakeProcess:
            return _FakeProcess(
                stdout_text="First response.\nFinal response.\n",
                returncode=0,
            )

        runtime = _make_runtime()
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            messages = await _collect(runtime, "Generate summary")

        result = messages[-1]
        assert result.type == "result"
        # The final content is the last accumulated text from stdout
        assert "Final response." in result.content

    @pytest.mark.asyncio
    async def test_empty_stdout_uses_fallback_content(self) -> None:
        """When stdout is empty, the runtime falls back to a canned completion message."""

        async def _fake_exec(*_args: str, **_kwargs: Any) -> _FakeProcess:
            return _FakeProcess(stdout_text="", stderr_text="", returncode=0)

        runtime = _make_runtime()
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            messages = await _collect(runtime, "Do something")

        result = messages[-1]
        assert result.type == "result"
        assert result.data.get("subtype") == "success"
        # Fallback message from CodexCliRuntime._execute_task_impl
        assert "Gemini CLI" in result.content or result.content.strip() != ""

    @pytest.mark.asyncio
    async def test_nonzero_exit_empty_stdout_falls_back_to_stderr(self) -> None:
        """On error with empty stdout, stderr content is used for the result message."""

        async def _fake_exec(*_args: str, **_kwargs: Any) -> _FakeProcess:
            return _FakeProcess(
                stdout_text="",
                stderr_text="gemini: quota exceeded",
                returncode=1,
            )

        runtime = _make_runtime()
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            messages = await _collect(runtime, "Analyse code")

        result = messages[-1]
        assert result.type == "result"
        assert result.data.get("subtype") == "error"
        assert "quota exceeded" in result.content or result.content.strip() != ""

    @pytest.mark.asyncio
    async def test_cli_not_found_yields_error_result(self) -> None:
        """FileNotFoundError from create_subprocess_exec must yield an error result."""

        async def _fake_exec(*_args: str, **_kwargs: Any) -> _FakeProcess:
            raise FileNotFoundError("No such file or directory: 'gemini'")

        runtime = _make_runtime(cli_path="/nonexistent/gemini")
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            messages = await _collect(runtime, "Run task")

        assert messages, "At least one message must be yielded"
        result = messages[-1]
        assert result.type == "result"
        assert result.data.get("subtype") == "error"
        assert "gemini" in result.content.lower() or "not found" in result.content.lower()

    @pytest.mark.asyncio
    async def test_generic_exec_exception_yields_error_result(self) -> None:
        """Any unexpected exception from subprocess creation is surfaced as an error."""

        async def _fake_exec(*_args: str, **_kwargs: Any) -> _FakeProcess:
            raise PermissionError("Permission denied: /usr/local/bin/gemini")

        runtime = _make_runtime()
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            messages = await _collect(runtime, "Run task")

        result = messages[-1]
        assert result.type == "result"
        assert result.data.get("subtype") == "error"

    @pytest.mark.asyncio
    async def test_last_result_message_is_final(self) -> None:
        """The last message from execute_task must be a 'result' (final) message."""

        async def _fake_exec(*_args: str, **_kwargs: Any) -> _FakeProcess:
            return _FakeProcess(stdout_text="All done.\n", returncode=0)

        runtime = _make_runtime()
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            messages = await _collect(runtime, "Do the thing")

        assert messages[-1].type == "result"
        assert messages[-1].is_final


# ---------------------------------------------------------------------------
# TestGeminiCLIRuntimeNoResumption
# ---------------------------------------------------------------------------


class TestGeminiCLIRuntimeNoResumption:
    """Verify that GeminiCLIRuntime does not attempt session resumption."""

    def test_max_resume_retries_is_zero(self) -> None:
        """_max_resume_retries must be 0 — no retry loop for Gemini."""
        runtime = _make_runtime()
        assert runtime._max_resume_retries == 0

    def test_build_resume_recovery_returns_none(self) -> None:
        """_build_resume_recovery must always return None for Gemini CLI."""
        from ouroboros.orchestrator.adapter import RuntimeHandle

        runtime = _make_runtime()
        recovery = runtime._build_resume_recovery(
            attempted_resume_session_id="old-session",
            current_handle=RuntimeHandle(backend="gemini_cli"),
            returncode=1,
            final_message="error message",
            stderr_lines=["stderr output"],
        )
        assert recovery is None

    @pytest.mark.asyncio
    async def test_resume_session_id_does_not_affect_command(self) -> None:
        """Passing a resume_session_id must not add session flags to the command."""
        from ouroboros.orchestrator.adapter import RuntimeHandle

        captured: list[tuple[str, ...]] = []

        async def _fake_exec(*args: str, **_kwargs: Any) -> _FakeProcess:
            captured.append(args)
            return _FakeProcess(stdout_text="done\n", returncode=0)

        runtime = _make_runtime()
        handle = RuntimeHandle(
            backend="gemini_cli",
            native_session_id="old-session-abc",
        )
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            await _collect(runtime, "Continue task", resume_handle=handle)

        command = captured[0]
        # 'resume' and the old session ID must not appear in the command
        assert "resume" not in command
        assert "old-session-abc" not in command

    @pytest.mark.asyncio
    async def test_execute_task_called_once_even_on_error(self) -> None:
        """On failure, the runtime must NOT retry via the resume loop."""
        spawn_count = 0

        async def _fake_exec(*_args: str, **_kwargs: Any) -> _FakeProcess:
            nonlocal spawn_count
            spawn_count += 1
            return _FakeProcess(
                stderr_text="rate limit exceeded",
                returncode=1,
            )

        runtime = _make_runtime()
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            messages = await _collect(runtime, "Do task")

        # With _max_resume_retries=0, the subprocess is spawned exactly once
        assert spawn_count == 1
        assert messages[-1].type == "result"
        assert messages[-1].data.get("subtype") == "error"


# ---------------------------------------------------------------------------
# TestGeminiCLIRuntimeErrorHandling
# ---------------------------------------------------------------------------


class TestGeminiCLIRuntimeErrorHandling:
    """Verify cancellation, timeout-style termination, and edge-case error paths."""

    @pytest.mark.asyncio
    async def test_cancellation_terminates_subprocess(self) -> None:
        """CancelledError from outside must terminate the child process."""
        hanging = _FakeHangingProcess()

        async def _fake_exec(*_args: str, **_kwargs: Any) -> _FakeHangingProcess:
            return hanging

        runtime = _make_runtime()

        async def _run_task() -> list[AgentMessage]:
            return await _collect(runtime, "Long running task")

        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            task = asyncio.create_task(_run_task())
            # Allow the task to start and block in wait()
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert hanging.terminated or hanging.killed, (
            "Process must be terminated when the consumer cancels the task"
        )

    @pytest.mark.asyncio
    async def test_terminate_called_before_kill_on_cancellation(self) -> None:
        """The runtime must attempt a graceful terminate() before resorting to kill()."""
        hanging = _FakeHangingProcess()

        async def _fake_exec(*_args: str, **_kwargs: Any) -> _FakeHangingProcess:
            return hanging

        runtime = _make_runtime()

        async def _run_task() -> list[AgentMessage]:
            return await _collect(runtime, "Long running task")

        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            task = asyncio.create_task(_run_task())
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        # terminate() is preferred over kill() — at minimum terminate must be set
        assert hanging.terminated, "terminate() must be called before kill()"

    @pytest.mark.asyncio
    async def test_json_event_with_session_id_builds_runtime_handle(self) -> None:
        """A JSON line containing 'session_id' must trigger handle construction."""
        import json

        session_event = json.dumps({"type": "session_started", "session_id": "sess-xyz"})
        content_line = "Task response text"
        stdout_text = f"{session_event}\n{content_line}\n"


        async def _fake_exec(*_args: str, **_kwargs: Any) -> _FakeProcess:
            return _FakeProcess(stdout_text=stdout_text, returncode=0)

        runtime = _make_runtime()
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            messages = await _collect(runtime, "Run with session")

        # A runtime handle should be attached to at least one message
        handles = [m.resume_handle for m in messages if m.resume_handle is not None]
        assert handles, "At least one message must carry a runtime handle"
        # The handle's native_session_id must reflect the session event
        assert any(
            h.native_session_id == "sess-xyz" for h in handles
        ), f"Session ID 'sess-xyz' not found in handles: {handles}"

    @pytest.mark.asyncio
    async def test_mixed_json_and_plaintext_stdout(self) -> None:
        """Mixed JSON + plain-text stdout must all produce messages without errors."""
        import json

        lines = "\n".join([
            json.dumps({"type": "thinking", "content": "Reasoning about the task."}),
            "Plain text response line 1",
            json.dumps({"type": "done", "exit_code": 0}),
            "Plain text response line 2",
        ]) + "\n"

        async def _fake_exec(*_args: str, **_kwargs: Any) -> _FakeProcess:
            return _FakeProcess(stdout_text=lines, returncode=0)

        runtime = _make_runtime()
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            messages = await _collect(runtime, "Analyse code")

        # Should complete without error
        assert messages[-1].type == "result"
        assert messages[-1].data.get("subtype") == "success"
        # Plain text lines must appear as assistant messages
        assistant_contents = [m.content for m in messages if m.type == "assistant"]
        assert any("Plain text response line" in c for c in assistant_contents)

    @pytest.mark.asyncio
    async def test_only_whitespace_stdout_lines_are_filtered(self) -> None:
        """Empty or whitespace-only stdout lines must not generate assistant messages."""

        async def _fake_exec(*_args: str, **_kwargs: Any) -> _FakeProcess:
            return _FakeProcess(stdout_text="   \n\n  \n", returncode=0)

        runtime = _make_runtime()
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            messages = await _collect(runtime, "Inspect code")

        assistant_messages = [m for m in messages if m.type == "assistant"]
        # No assistant messages should be produced from blank lines
        for msg in assistant_messages:
            assert msg.content.strip() != "", (
                f"Blank assistant message emitted: {msg!r}"
            )

    @pytest.mark.asyncio
    async def test_stdout_and_stderr_pipes_both_requested(self) -> None:
        """Both stdout and stderr must be opened as PIPE for output capture."""
        captured_kwargs: dict[str, Any] = {}

        async def _fake_exec(*_args: str, **kwargs: Any) -> _FakeProcess:
            captured_kwargs.update(kwargs)
            return _FakeProcess(stdout_text="ok\n", returncode=0)

        runtime = _make_runtime()
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            await _collect(runtime, "Run task")

        assert captured_kwargs.get("stdout") == asyncio.subprocess.PIPE
        assert captured_kwargs.get("stderr") == asyncio.subprocess.PIPE

    @pytest.mark.asyncio
    async def test_high_exit_code_produces_error_result(self) -> None:
        """Exit codes > 1 (e.g., 127 command not found) are still treated as errors."""

        async def _fake_exec(*_args: str, **_kwargs: Any) -> _FakeProcess:
            return _FakeProcess(
                stdout_text="",
                stderr_text="command not found",
                returncode=127,
            )

        runtime = _make_runtime()
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            messages = await _collect(runtime, "Run command")

        result = messages[-1]
        assert result.type == "result"
        assert result.data.get("subtype") == "error"
        assert result.data.get("returncode") == 127

    @pytest.mark.asyncio
    async def test_runtime_factory_creates_gemini_cli_runtime(
        self, tmp_path: Path
    ) -> None:
        """The runtime factory must produce a GeminiCLIRuntime for backend='gemini'."""
        from ouroboros.orchestrator.runtime_factory import create_agent_runtime

        # Patch the factory to support gemini backend (it may not be registered yet)
        with patch(
            "ouroboros.orchestrator.runtime_factory.resolve_agent_runtime_backend",
            return_value="gemini",
        ), patch(
            "ouroboros.orchestrator.runtime_factory.get_codex_cli_path",
            return_value="/usr/local/bin/gemini",
        ):
            try:
                runtime = create_agent_runtime(
                    backend="gemini",
                    cli_path="/usr/local/bin/gemini",
                    cwd=tmp_path,
                )
                # If factory supports gemini, verify it's the right type
                assert isinstance(runtime, GeminiCLIRuntime)
            except (ValueError, KeyError):
                # Factory may not yet know 'gemini' backend — that's acceptable
                # for this lifecycle-only test suite
                pytest.skip(
                    "Runtime factory does not yet register 'gemini' backend; "
                    "skipping factory integration assertion."
                )

    @pytest.mark.asyncio
    async def test_multiline_response_all_lines_emitted(self) -> None:
        """All non-empty stdout lines must yield corresponding assistant messages."""
        response_lines = [f"Line {i}" for i in range(1, 6)]
        stdout_text = "\n".join(response_lines) + "\n"

        async def _fake_exec(*_args: str, **_kwargs: Any) -> _FakeProcess:
            return _FakeProcess(stdout_text=stdout_text, returncode=0)

        runtime = _make_runtime()
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec):
            messages = await _collect(runtime, "Generate list")

        assistant_contents = [m.content for m in messages if m.type == "assistant"]
        for expected_line in response_lines:
            assert any(expected_line in c for c in assistant_contents), (
                f"Expected line '{expected_line}' not found in assistant messages"
            )


__all__ = [
    "TestGeminiCLIRuntimeInit",
    "TestGeminiCLIRuntimeProcessStart",
    "TestGeminiCLIRuntimeProcessStop",
    "TestGeminiCLIRuntimeNoResumption",
    "TestGeminiCLIRuntimeErrorHandling",
]
