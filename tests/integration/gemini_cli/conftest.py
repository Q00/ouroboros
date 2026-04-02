"""Integration test fixtures and mock Gemini CLI process harness.

This module provides:
- Subprocess simulator (FakeGeminiProcess, FakeGeminiStream, FakeGeminiStdin)
- Fake event stream emitter (FakeGeminiEventStreamEmitter)
- Queue-backed subprocess stub (GeminiCLISubprocessStub)
- pytest fixtures for integration tests

Gemini CLI Event Schema (JSONL):
    Each line is a JSON object with a "type" field.  Ouroboros normalises
    these into internal AgentMessage objects via the event normaliser.

    session_started: {"type": "session_started", "session_id": "<id>"}
    message:         {"type": "message", "content": "<text>"}
    thinking:        {"type": "thinking", "content": "<reasoning>"}
    tool_call:       {"type": "tool_call", "name": "<tool>", "args": {...}}
    tool_result:     {"type": "tool_result", "name": "<tool>", "output": "<text>"}
    done:            {"type": "done", "exit_code": 0}
    error:           {"type": "error", "message": "<err>", "exit_code": 1}

Usage:
    def test_adapter_happy_path(gemini_subprocess_stub, gemini_session_id):
        gemini_subprocess_stub.queue(
            final_response="All acceptance criteria satisfied.",
            stdout_events=[
                gemini_event_session_started(gemini_session_id),
                gemini_event_message("Working on the task…"),
                gemini_event_done(),
            ],
        )
        ...
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Gemini CLI event constructors
# ---------------------------------------------------------------------------


def gemini_event_session_started(session_id: str) -> dict[str, Any]:
    """Create a ``session_started`` JSONL event.

    Args:
        session_id: Opaque session identifier returned by Gemini CLI.

    Returns:
        Event dict to include in a GeminiCLIScenario's stdout_events.
    """
    return {"type": "session_started", "session_id": session_id}


def gemini_event_thinking(content: str) -> dict[str, Any]:
    """Create a ``thinking`` JSONL event (agent internal reasoning).

    Args:
        content: Reasoning text produced by the Gemini model.

    Returns:
        Event dict.
    """
    return {"type": "thinking", "content": content}


def gemini_event_message(content: str) -> dict[str, Any]:
    """Create a ``message`` JSONL event (assistant text response).

    Args:
        content: Text content of the assistant message.

    Returns:
        Event dict.
    """
    return {"type": "message", "content": content}


def gemini_event_tool_call(
    name: str,
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a ``tool_call`` JSONL event.

    Args:
        name: Fully-qualified tool name (e.g. ``"Read"``, ``"mcp__server__tool"``).
        args: Optional key/value input arguments for the tool.

    Returns:
        Event dict.
    """
    return {"type": "tool_call", "name": name, "args": args or {}}


def gemini_event_tool_result(
    name: str,
    output: str,
    *,
    is_error: bool = False,
) -> dict[str, Any]:
    """Create a ``tool_result`` JSONL event.

    Args:
        name: Tool name whose result is being reported.
        output: Text output produced by the tool.
        is_error: Whether the tool execution returned an error.

    Returns:
        Event dict.
    """
    return {"type": "tool_result", "name": name, "output": output, "is_error": is_error}


def gemini_event_done(*, exit_code: int = 0) -> dict[str, Any]:
    """Create a ``done`` JSONL event signalling successful completion.

    Args:
        exit_code: Process exit code (0 == success).

    Returns:
        Event dict.
    """
    return {"type": "done", "exit_code": exit_code}


def gemini_event_error(message: str, *, exit_code: int = 1) -> dict[str, Any]:
    """Create an ``error`` JSONL event.

    Args:
        message: Human-readable error description.
        exit_code: Process exit code (non-zero indicates error).

    Returns:
        Event dict.
    """
    return {"type": "error", "message": message, "exit_code": exit_code}


# ---------------------------------------------------------------------------
# Subprocess stream / process doubles
# ---------------------------------------------------------------------------


class FakeGeminiStream:
    """Minimal async byte-stream double for subprocess stdout/stderr pipes.

    Simulates :class:`asyncio.StreamReader` with a single-read buffer so
    tests do not need a real subprocess.  Supports both ``read()`` (for
    chunk-based readers) and ``readline()`` (for line-based readers).

    Example::

        stream = FakeGeminiStream('{"type": "done", "exit_code": 0}\\n')
        data = await stream.read(16384)
        assert data == b'{"type": "done", "exit_code": 0}\\n'
    """

    def __init__(self, text: str = "") -> None:
        """Initialise with UTF-8 encoded payload.

        Args:
            text: Text content that will be returned on the first read.
        """
        self._buffer: bytes = text.encode("utf-8")

    # ------------------------------------------------------------------
    # asyncio.StreamReader-compatible API
    # ------------------------------------------------------------------

    async def read(self, chunk_size: int = 16384) -> bytes:
        """Return up to *chunk_size* bytes, draining the internal buffer.

        Args:
            chunk_size: Maximum bytes to return per call.

        Returns:
            Next bytes from the buffer, or ``b""`` when exhausted.
        """
        if not self._buffer:
            return b""
        chunk, self._buffer = self._buffer[:chunk_size], self._buffer[chunk_size:]
        return chunk

    async def readline(self) -> bytes:
        """Return the next line (including the trailing ``\\n``).

        Returns:
            Next line as bytes, or ``b""`` when exhausted.
        """
        if not self._buffer:
            return b""
        idx = self._buffer.find(b"\n")
        if idx == -1:
            # No newline — return all remaining bytes
            line, self._buffer = self._buffer, b""
            return line
        line, self._buffer = self._buffer[: idx + 1], self._buffer[idx + 1 :]
        return line

    @property
    def is_drained(self) -> bool:
        """Return ``True`` when all buffered data has been consumed."""
        return len(self._buffer) == 0


class FakeGeminiStdin:
    """Minimal async stdin-pipe double that records written payloads.

    Tests can inspect :attr:`writes` to verify that the adapter sent the
    expected prompt bytes to the subprocess stdin.

    Example::

        stdin = FakeGeminiStdin()
        stdin.write(b"Hello Gemini!")
        await stdin.drain()
        stdin.close()
        assert stdin.written == b"Hello Gemini!"
        assert stdin.closed
    """

    def __init__(self) -> None:
        """Initialise with empty write log."""
        self.writes: list[bytes] = []
        self.closed: bool = False

    def write(self, data: bytes) -> None:
        """Record a write to the stdin pipe.

        Args:
            data: Bytes written by the adapter.
        """
        self.writes.append(data)

    async def drain(self) -> None:
        """No-op flush — satisfies the asyncio transport interface."""
        return None

    def close(self) -> None:
        """Mark the stdin pipe as closed."""
        self.closed = True

    async def wait_closed(self) -> None:
        """No-op — satisfies the asyncio transport interface."""
        return None

    @property
    def written(self) -> bytes:
        """Concatenation of all recorded write calls."""
        return b"".join(self.writes)


class FakeGeminiProcess:
    """Async subprocess double for Gemini CLI tests.

    Emulates :class:`asyncio.subprocess.Process` with preconfigured
    stdout, stderr, and return code so integration tests can exercise
    adapter code paths without spawning a real process.

    The double supports both the streaming API (via :attr:`stdout` /
    :attr:`stderr` :class:`FakeGeminiStream` objects) and the legacy
    ``communicate()`` API used by some codepaths.

    Example::

        process = FakeGeminiProcess(
            stdout_text='{"type": "done", "exit_code": 0}\\n',
            returncode=0,
            stdin=FakeGeminiStdin(),
        )
        rc = await process.wait()
        assert rc == 0
    """

    def __init__(
        self,
        *,
        stdout_text: str = "",
        stderr_text: str = "",
        returncode: int = 0,
        stdin: FakeGeminiStdin | None = None,
    ) -> None:
        """Initialise the fake process.

        Args:
            stdout_text: Content pre-loaded into the stdout stream.
            stderr_text: Content pre-loaded into the stderr stream.
            returncode: Exit code returned by :meth:`wait`.
            stdin: Optional stdin double (tests can inspect ``.writes``).
        """
        self.stdout: FakeGeminiStream = FakeGeminiStream(stdout_text)
        self.stderr: FakeGeminiStream = FakeGeminiStream(stderr_text)
        self.stdin: FakeGeminiStdin | None = stdin
        self.returncode: int = returncode
        # Also expose raw bytes for communicate()-based paths
        self._stdout_bytes: bytes = stdout_text.encode("utf-8")
        self._stderr_bytes: bytes = stderr_text.encode("utf-8")

    async def wait(self) -> int:
        """Return the preconfigured exit code.

        Returns:
            The configured returncode.
        """
        return self.returncode

    async def communicate(
        self, _input: bytes | None = None
    ) -> tuple[bytes, bytes]:
        """Return buffered stdout and stderr bytes.

        Args:
            _input: Ignored — stdin is not simulated in communicate mode.

        Returns:
            Tuple of (stdout_bytes, stderr_bytes).
        """
        return self._stdout_bytes, self._stderr_bytes


# ---------------------------------------------------------------------------
# Scenario + stub
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RecordedGeminiCLICall:
    """Captured subprocess invocation for assertion in tests.

    Attributes:
        command: Full argv tuple passed to ``create_subprocess_exec``.
        cwd: Working directory passed to the subprocess.
        stdin_requested: Whether stdin was opened as a pipe.
        env: Child environment passed to the subprocess (may be empty).
    """

    command: tuple[str, ...]
    cwd: str | None
    stdin_requested: bool = False
    env: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class GeminiCLIScenario:
    """Queued subprocess response for a single test invocation.

    Attributes:
        final_response: Final assistant response text (written to
            ``--output-last-message`` file when the stub handles the call).
        stdout_events: JSONL events emitted on stdout.
        stderr_text: Raw text on stderr (diagnostic messages).
        returncode: Process exit code.
    """

    final_response: str
    stdout_events: list[dict[str, Any]] = field(default_factory=list)
    stderr_text: str = ""
    returncode: int = 0

    def stdout_text(self) -> str:
        """Serialise *stdout_events* as newline-delimited JSON.

        Returns:
            JSONL string ready to feed to a :class:`FakeGeminiStream`.
        """
        if not self.stdout_events:
            return ""
        return (
            "\n".join(json.dumps(event) for event in self.stdout_events) + "\n"
        )


class GeminiCLISubprocessStub:
    """Queue-backed ``create_subprocess_exec`` replacement for integration tests.

    Tests queue one :class:`GeminiCLIScenario` per expected subprocess call.
    When the stub is invoked it:

    1. Pops the next scenario from the queue.
    2. Writes *final_response* to the ``--output-last-message`` path (if
       present in the command) so file-based output paths work correctly.
    3. Returns a :class:`FakeGeminiProcess` whose stdout emits the scenario's
       JSONL events.

    Example::

        stub = GeminiCLISubprocessStub()
        stub.queue(
            final_response="Task complete.",
            stdout_events=[
                gemini_event_session_started("sess-1"),
                gemini_event_message("Working…"),
                gemini_event_done(),
            ],
        )
        monkeypatch.setattr(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            stub,
        )
    """

    def __init__(self) -> None:
        """Initialise with empty call log and scenario queue."""
        self.calls: list[RecordedGeminiCLICall] = []
        self.processes: list[FakeGeminiProcess] = []
        self._scenarios: list[GeminiCLIScenario] = []

    def queue(
        self,
        *,
        final_response: str,
        stdout_events: list[dict[str, Any]] | None = None,
        stderr_text: str = "",
        returncode: int = 0,
    ) -> None:
        """Enqueue a scenario for the next subprocess invocation.

        Args:
            final_response: Text written to the ``--output-last-message`` file.
            stdout_events: JSONL events emitted on stdout.
            stderr_text: Text on stderr.
            returncode: Process exit code.
        """
        self._scenarios.append(
            GeminiCLIScenario(
                final_response=final_response,
                stdout_events=list(stdout_events or ()),
                stderr_text=stderr_text,
                returncode=returncode,
            )
        )

    async def __call__(self, *command: str, **kwargs: Any) -> FakeGeminiProcess:
        """Simulate ``asyncio.create_subprocess_exec``.

        Args:
            *command: Argv tuple passed by the adapter.
            **kwargs: Keyword args (cwd, stdin, stdout, stderr, env, …).

        Returns:
            A :class:`FakeGeminiProcess` pre-loaded with the next scenario's
            stdout/stderr/returncode.

        Raises:
            AssertionError: If no scenario has been queued.
        """
        if not self._scenarios:
            raise AssertionError(
                "No subprocess scenario queued for GeminiCLI test stub. "
                "Call stub.queue(...) before exercising the adapter."
            )

        scenario = self._scenarios.pop(0)
        stdin_requested = kwargs.get("stdin") == asyncio.subprocess.PIPE
        env: dict[str, str] = dict(kwargs.get("env") or {})

        self.calls.append(
            RecordedGeminiCLICall(
                command=tuple(command),
                cwd=str(kwargs.get("cwd")) if kwargs.get("cwd") is not None else None,
                stdin_requested=stdin_requested,
                env=env,
            )
        )

        # Write final_response to --output-last-message path if present
        command_list = list(command)
        if "--output-last-message" in command_list:
            output_index = command_list.index("--output-last-message") + 1
            if output_index < len(command_list):
                Path(command_list[output_index]).write_text(
                    scenario.final_response, encoding="utf-8"
                )

        process = FakeGeminiProcess(
            stdout_text=scenario.stdout_text(),
            stderr_text=scenario.stderr_text,
            returncode=scenario.returncode,
            stdin=FakeGeminiStdin() if stdin_requested else None,
        )
        self.processes.append(process)
        return process

    @property
    def call_count(self) -> int:
        """Number of subprocess invocations recorded so far."""
        return len(self.calls)

    @property
    def last_call(self) -> RecordedGeminiCLICall | None:
        """Most recent recorded call, or ``None`` if no calls yet."""
        return self.calls[-1] if self.calls else None


# ---------------------------------------------------------------------------
# Fake event stream emitter
# ---------------------------------------------------------------------------


class FakeGeminiEventStreamEmitter:
    """Async generator that emits a configurable sequence of JSONL events.

    Useful for testing the event normaliser in isolation — pass one of
    these to the normaliser instead of a real subprocess stdout stream.

    Example::

        emitter = FakeGeminiEventStreamEmitter([
            gemini_event_session_started("sess-abc"),
            gemini_event_thinking("Let me read the files first."),
            gemini_event_tool_call("Read", {"file_path": "src/foo.py"}),
            gemini_event_tool_result("Read", "contents…"),
            gemini_event_message("Done."),
            gemini_event_done(),
        ])
        async for line in emitter:
            event = json.loads(line)
            ...
    """

    def __init__(
        self,
        events: list[dict[str, Any]],
        *,
        delay_seconds: float = 0.0,
    ) -> None:
        """Initialise with a sequence of events.

        Args:
            events: JSONL event dicts to emit in order.
            delay_seconds: Optional per-event delay to simulate slow CLIs.
        """
        self._events: list[dict[str, Any]] = list(events)
        self._delay: float = delay_seconds

    def __aiter__(self) -> AsyncIterator[str]:
        """Return async iterator over serialised JSONL lines."""
        return self._generate()

    async def _generate(self) -> AsyncIterator[str]:
        """Yield serialised JSONL lines one at a time.

        Yields:
            Newline-terminated JSON string for each event.
        """
        for event in self._events:
            if self._delay > 0:
                await asyncio.sleep(self._delay)
            yield json.dumps(event) + "\n"

    @property
    def event_count(self) -> int:
        """Total number of events in this emitter."""
        return len(self._events)


# ---------------------------------------------------------------------------
# Canned event sequences
# ---------------------------------------------------------------------------


def make_happy_path_events(
    session_id: str,
    response_text: str = "Task completed successfully.",
) -> list[dict[str, Any]]:
    """Build a minimal happy-path Gemini CLI event sequence.

    Args:
        session_id: Session ID to include in the ``session_started`` event.
        response_text: Text to include in the final ``message`` event.

    Returns:
        List of event dicts representing a successful single-turn run.
    """
    return [
        gemini_event_session_started(session_id),
        gemini_event_thinking("Analysing the request."),
        gemini_event_message(response_text),
        gemini_event_done(),
    ]


def make_tool_use_events(
    session_id: str,
    tool_name: str = "Read",
    tool_args: dict[str, Any] | None = None,
    tool_output: str = "file contents here",
    final_text: str = "Done reading files.",
) -> list[dict[str, Any]]:
    """Build a Gemini CLI event sequence with a single tool invocation.

    Args:
        session_id: Session ID for the ``session_started`` event.
        tool_name: Name of the tool being called.
        tool_args: Input arguments for the tool call.
        tool_output: Simulated output returned from the tool.
        final_text: Final assistant response text.

    Returns:
        List of event dicts representing a run with one tool call.
    """
    return [
        gemini_event_session_started(session_id),
        gemini_event_thinking("I need to read the file."),
        gemini_event_tool_call(tool_name, tool_args or {"file_path": "src/foo.py"}),
        gemini_event_tool_result(tool_name, tool_output),
        gemini_event_message(final_text),
        gemini_event_done(),
    ]


def make_error_events(
    session_id: str,
    error_message: str = "Rate limit exceeded. Please try again later.",
) -> list[dict[str, Any]]:
    """Build a Gemini CLI event sequence representing a runtime error.

    Args:
        session_id: Session ID for the ``session_started`` event.
        error_message: Error description to include in the ``error`` event.

    Returns:
        List of event dicts representing a failed run.
    """
    return [
        gemini_event_session_started(session_id),
        gemini_event_error(error_message, exit_code=1),
    ]


def make_multi_turn_events(
    session_id: str,
    turns: int = 3,
) -> list[dict[str, Any]]:
    """Build a multi-turn Gemini CLI event sequence.

    Args:
        session_id: Session ID for the ``session_started`` event.
        turns: Number of (tool_call → tool_result → message) cycles to emit.

    Returns:
        List of event dicts representing a multi-turn run.
    """
    events: list[dict[str, Any]] = [gemini_event_session_started(session_id)]
    for i in range(1, turns + 1):
        events.append(gemini_event_thinking(f"Turn {i}: deciding next step."))
        events.append(
            gemini_event_tool_call("Read", {"file_path": f"src/module_{i}.py"})
        )
        events.append(
            gemini_event_tool_result("Read", f"# Module {i} contents\n\ndef fn_{i}(): ...")
        )
        events.append(gemini_event_message(f"Processed module {i}."))
    events.append(gemini_event_done())
    return events


# ---------------------------------------------------------------------------
# pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def gemini_session_id() -> str:
    """A stable, deterministic Gemini CLI session ID for test assertions.

    Returns:
        Opaque session ID string.
    """
    return "gemini-session-test-abc123"


@pytest.fixture
def gemini_subprocess_stub() -> GeminiCLISubprocessStub:
    """Provide a fresh queue-backed subprocess stub for each test.

    Returns:
        :class:`GeminiCLISubprocessStub` with an empty call log and
        scenario queue.
    """
    return GeminiCLISubprocessStub()


@pytest.fixture
def gemini_happy_path_events(gemini_session_id: str) -> list[dict[str, Any]]:
    """Representative happy-path Gemini CLI JSONL events.

    Returns:
        List of event dicts: session_started → thinking → message → done.
    """
    return make_happy_path_events(
        gemini_session_id,
        response_text="Task completed successfully.",
    )


@pytest.fixture
def gemini_tool_use_events(gemini_session_id: str) -> list[dict[str, Any]]:
    """Gemini CLI JSONL event sequence with a single Read tool invocation.

    Returns:
        List of event dicts: session_started → thinking → tool_call
        → tool_result → message → done.
    """
    return make_tool_use_events(
        gemini_session_id,
        tool_name="Read",
        tool_args={"file_path": "src/ouroboros/providers/gemini_cli_adapter.py"},
        tool_output="# GeminiCLIAdapter source\n",
        final_text="File read successfully.",
    )


@pytest.fixture
def gemini_error_events(gemini_session_id: str) -> list[dict[str, Any]]:
    """Gemini CLI JSONL event sequence representing a rate-limit error.

    Returns:
        List of event dicts: session_started → error.
    """
    return make_error_events(
        gemini_session_id,
        error_message="Rate limit exceeded. Please try again later.",
    )


@pytest.fixture
def gemini_multi_turn_events(gemini_session_id: str) -> list[dict[str, Any]]:
    """Multi-turn Gemini CLI JSONL event sequence (3 tool-call cycles).

    Returns:
        List of event dicts for a 3-turn execution.
    """
    return make_multi_turn_events(gemini_session_id, turns=3)


@pytest.fixture
def gemini_event_emitter(
    gemini_happy_path_events: list[dict[str, Any]],
) -> FakeGeminiEventStreamEmitter:
    """Async event-stream emitter pre-loaded with happy-path events.

    Returns:
        :class:`FakeGeminiEventStreamEmitter` ready to iterate.
    """
    return FakeGeminiEventStreamEmitter(gemini_happy_path_events)


@pytest.fixture
def gemini_error_emitter(
    gemini_error_events: list[dict[str, Any]],
) -> FakeGeminiEventStreamEmitter:
    """Async event-stream emitter pre-loaded with error events.

    Returns:
        :class:`FakeGeminiEventStreamEmitter` for error-path tests.
    """
    return FakeGeminiEventStreamEmitter(gemini_error_events)


@pytest.fixture
def gemini_tool_use_emitter(
    gemini_tool_use_events: list[dict[str, Any]],
) -> FakeGeminiEventStreamEmitter:
    """Async event-stream emitter pre-loaded with tool-use events.

    Returns:
        :class:`FakeGeminiEventStreamEmitter` for tool-call tests.
    """
    return FakeGeminiEventStreamEmitter(gemini_tool_use_events)


# ---------------------------------------------------------------------------
# Convenience: pre-configured subprocess stub fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def gemini_subprocess_stub_happy(
    gemini_subprocess_stub: GeminiCLISubprocessStub,
    gemini_happy_path_events: list[dict[str, Any]],
) -> GeminiCLISubprocessStub:
    """Subprocess stub pre-queued with a happy-path scenario.

    Returns:
        :class:`GeminiCLISubprocessStub` with one queued scenario.
    """
    gemini_subprocess_stub.queue(
        final_response="Task completed successfully.",
        stdout_events=gemini_happy_path_events,
    )
    return gemini_subprocess_stub


@pytest.fixture
def gemini_subprocess_stub_error(
    gemini_subprocess_stub: GeminiCLISubprocessStub,
    gemini_error_events: list[dict[str, Any]],
) -> GeminiCLISubprocessStub:
    """Subprocess stub pre-queued with an error scenario (exit code 1).

    Returns:
        :class:`GeminiCLISubprocessStub` with one error scenario queued.
    """
    gemini_subprocess_stub.queue(
        final_response="",
        stdout_events=gemini_error_events,
        stderr_text="gemini: error: rate limit exceeded",
        returncode=1,
    )
    return gemini_subprocess_stub


@pytest.fixture
def gemini_subprocess_stub_tool_use(
    gemini_subprocess_stub: GeminiCLISubprocessStub,
    gemini_tool_use_events: list[dict[str, Any]],
) -> GeminiCLISubprocessStub:
    """Subprocess stub pre-queued with a tool-use scenario.

    Returns:
        :class:`GeminiCLISubprocessStub` with one tool-use scenario queued.
    """
    gemini_subprocess_stub.queue(
        final_response="File read successfully.",
        stdout_events=gemini_tool_use_events,
    )
    return gemini_subprocess_stub


__all__ = [
    # Event constructors
    "gemini_event_done",
    "gemini_event_error",
    "gemini_event_message",
    "gemini_event_session_started",
    "gemini_event_thinking",
    "gemini_event_tool_call",
    "gemini_event_tool_result",
    # Canned sequences
    "make_error_events",
    "make_happy_path_events",
    "make_multi_turn_events",
    "make_tool_use_events",
    # Subprocess doubles
    "FakeGeminiProcess",
    "FakeGeminiStdin",
    "FakeGeminiStream",
    # Scenario / stub
    "GeminiCLIScenario",
    "GeminiCLISubprocessStub",
    "RecordedGeminiCLICall",
    # Event stream emitter
    "FakeGeminiEventStreamEmitter",
]
