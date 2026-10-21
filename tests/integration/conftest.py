"""Shared fixtures for integration tests that stub local CLI runtimes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

import pytest


class FakeCLIStream:
    """Minimal async byte stream for subprocess stdout/stderr tests."""

    def __init__(self, text: str = "") -> None:
        self._buffer = text.encode("utf-8")
        self._drained = False

    async def read(self, _chunk_size: int = 16384) -> bytes:
        if self._drained:
            return b""
        self._drained = True
        return self._buffer


class FakeCLIStdin:
    """Minimal async stdin pipe that records written payloads."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class FakeCLIProcess:
    """Async subprocess double that supports both runtime and adapter flows."""

    def __init__(
        self,
        *,
        stdout_text: str = "",
        stderr_text: str = "",
        returncode: int = 0,
        stdin: FakeCLIStdin | None = None,
    ) -> None:
        self.stdout = FakeCLIStream(stdout_text)
        self.stderr = FakeCLIStream(stderr_text)
        self.stdin = stdin
        self._stdout_bytes = stdout_text.encode("utf-8")
        self._stderr_bytes = stderr_text.encode("utf-8")
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode

    async def communicate(self, _input: bytes | None = None) -> tuple[bytes, bytes]:
        return self._stdout_bytes, self._stderr_bytes


@dataclass(slots=True)
class RecordedCLICall:
    """Captured subprocess invocation."""

    command: tuple[str, ...]
    cwd: str | None
    stdin_requested: bool = False


@dataclass(slots=True)
class CLIScenario:
    """Queued subprocess response for a test invocation."""

    final_message: str
    stdout_events: list[dict[str, Any]] = field(default_factory=list)
    stderr_text: str = ""
    returncode: int = 0

    def stdout_text(self) -> str:
        if not self.stdout_events:
            return ""
        return "\n".join(json.dumps(event) for event in self.stdout_events) + "\n"


class OpenCodeSubprocessStub:
    """Queue-backed subprocess stub for runtime and provider integration tests."""

    def __init__(self) -> None:
        self.calls: list[RecordedCLICall] = []
        self.processes: list[FakeCLIProcess] = []
        self._scenarios: list[CLIScenario] = []

    def queue(
        self,
        *,
        final_message: str,
        stdout_events: list[dict[str, Any]] | None = None,
        stderr_text: str = "",
        returncode: int = 0,
    ) -> None:
        self._scenarios.append(
            CLIScenario(
                final_message=final_message,
                stdout_events=list(stdout_events or ()),
                stderr_text=stderr_text,
                returncode=returncode,
            )
        )

    async def __call__(self, *command: str, **kwargs: Any) -> FakeCLIProcess:
        if not self._scenarios:
            raise AssertionError("No subprocess scenario queued for OpenCode test stub")

        scenario = self._scenarios.pop(0)
        stdin_requested = kwargs.get("stdin") == asyncio.subprocess.PIPE
        self.calls.append(
            RecordedCLICall(
                command=tuple(command),
                cwd=kwargs.get("cwd"),
                stdin_requested=stdin_requested,
            )
        )

        output_index = command.index("--output-last-message") + 1
        Path(command[output_index]).write_text(scenario.final_message, encoding="utf-8")

        process = FakeCLIProcess(
            stdout_text=scenario.stdout_text(),
            stderr_text=scenario.stderr_text,
            returncode=scenario.returncode,
            stdin=FakeCLIStdin() if stdin_requested else None,
        )
        self.processes.append(process)
        return process


@pytest.fixture
def opencode_subprocess_stub() -> OpenCodeSubprocessStub:
    """Provide a reusable queued subprocess stub for OpenCode tests."""
    return OpenCodeSubprocessStub()


@pytest.fixture
def opencode_runtime_lifecycle_events() -> list[dict[str, Any]]:
    """Representative OpenCode JSONL events for runtime lifecycle tests."""
    return [
        {"type": "thread.started", "thread_id": "oc-session-123"},
        {
            "type": "item.completed",
            "item": {"type": "reasoning", "text": "Inspecting the current workspace state."},
        },
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "name": "execute_seed",
                "arguments": {"session_id": "sess-1", "cwd": "/tmp/workspace"},
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "Patched the implementation and prepared verification notes.",
            },
        },
    ]
