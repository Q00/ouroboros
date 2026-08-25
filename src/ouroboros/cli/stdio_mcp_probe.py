"""Dependency-free stdio MCP health probe shared by runtime setup and doctors.

The probe intentionally speaks the small MCP initialize/list_tools/call_tool
JSON-RPC sequence directly instead of using :class:`MCPClientAdapter`.
Callers validate self-contained launcher commands such as
``uvx --isolated --python >=3.12 --from ouroboros-ai[mcp] ouroboros mcp serve``;
that validation must not first require the current interpreter to have
installed the optional local ``mcp`` extra.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import json
import os
import signal
from typing import Any

_MCP_PROTOCOL_VERSION = "2024-11-05"
_MCP_STDERR_TAIL_BYTES = 8192


class StdioMcpFramingMismatch(RuntimeError):
    """Raised when a stdio MCP response clearly uses another wire framing."""


class StdioMcpFramingProbeFailed(RuntimeError):
    """Raised when the initialize response failed before framing was established."""


async def list_stdio_mcp_tool_names(
    command: str, args: tuple[str, ...], env: dict[str, str]
) -> frozenset[str]:
    """Launch a stdio MCP server and return the names exposed by list_tools()."""
    return await _probe_stdio_mcp(command, args, env)


async def probe_stdio_mcp_tool(
    command: str,
    args: tuple[str, ...],
    env: dict[str, str],
    *,
    tool_name: str,
    tool_arguments: dict[str, object],
) -> frozenset[str]:
    """Initialize a stdio MCP server, list tools, and call one health-check tool."""
    return await _probe_stdio_mcp(
        command,
        args,
        env,
        tool_call=(tool_name, tool_arguments),
    )


async def _probe_stdio_mcp(
    command: str,
    args: tuple[str, ...],
    env: dict[str, str],
    *,
    tool_call: tuple[str, dict[str, object]] | None = None,
) -> frozenset[str]:
    try:
        return await _list_stdio_mcp_tool_names_with_framing(
            command,
            args,
            env,
            framing="jsonl",
            tool_call=tool_call,
        )
    except StdioMcpFramingProbeFailed:
        return await _list_stdio_mcp_tool_names_with_framing(
            command,
            args,
            env,
            framing="content-length",
            tool_call=tool_call,
        )


async def _list_stdio_mcp_tool_names_with_framing(
    command: str,
    args: tuple[str, ...],
    env: dict[str, str],
    *,
    framing: str,
    tool_call: tuple[str, dict[str, object]] | None = None,
) -> frozenset[str]:
    """Launch a stdio MCP server with one wire framing and return tool names."""
    process_env = os.environ.copy()
    process_env.update(env)
    proc = await asyncio.create_subprocess_exec(
        command,
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=process_env,
        start_new_session=os.name == "posix",
    )
    stderr_buffer = bytearray()
    stderr_task = (
        asyncio.create_task(_drain_stdio_mcp_stderr(proc.stderr, stderr_buffer))
        if proc.stderr is not None
        else None
    )
    try:
        try:
            await _send_stdio_mcp_message(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": _MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {
                            "name": "ouroboros-mcp-probe",
                            "version": "0.0.0",
                        },
                    },
                },
                framing=framing,
            )
            await _read_stdio_mcp_response(
                proc,
                request_id=1,
                timeout=30.0,
                stderr_buffer=stderr_buffer,
                framing=framing,
            )
        except (
            OSError,
            json.JSONDecodeError,
            StdioMcpFramingMismatch,
            RuntimeError,
            TimeoutError,
        ) as exc:
            if framing == "jsonl" and _should_retry_stdio_mcp_framing(exc):
                raise StdioMcpFramingProbeFailed(str(exc)) from exc
            raise
        await _send_stdio_mcp_message(
            proc,
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            framing=framing,
        )
        await _send_stdio_mcp_message(
            proc,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            framing=framing,
        )
        response = await _read_stdio_mcp_response(
            proc,
            request_id=2,
            timeout=30.0,
            stderr_buffer=stderr_buffer,
            framing=framing,
        )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise RuntimeError("tools/list response did not contain an object result")
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise RuntimeError("tools/list response did not contain a tools list")
        tool_names = frozenset(
            tool["name"]
            for tool in tools
            if isinstance(tool, Mapping) and isinstance(tool.get("name"), str)
        )
        if tool_call is not None:
            tool_name, tool_arguments = tool_call
            if tool_name not in tool_names:
                raise RuntimeError(f"required health-check tool is unavailable: {tool_name}")
            await _send_stdio_mcp_message(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": tool_arguments},
                },
                framing=framing,
            )
            call_response = await _read_stdio_mcp_response(
                proc,
                request_id=3,
                timeout=30.0,
                stderr_buffer=stderr_buffer,
                framing=framing,
            )
            call_result = call_response.get("result")
            if not isinstance(call_result, Mapping):
                raise RuntimeError("tools/call response did not contain an object result")
            if call_result.get("isError") is True:
                raise RuntimeError(f"health-check tool returned an error: {tool_name}")
        return tool_names
    finally:
        await _terminate_stdio_mcp_process(proc)
        if stderr_task is not None:
            try:
                await asyncio.wait_for(stderr_task, timeout=0.2)
            except TimeoutError:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)


def _should_retry_stdio_mcp_framing(exc: BaseException) -> bool:
    """Return True when JSONL failed before a valid initialize response existed."""
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, (OSError, json.JSONDecodeError, StdioMcpFramingMismatch)):
        return True
    return isinstance(exc, RuntimeError) and "exited before response" in str(exc)


async def _send_stdio_mcp_message(
    proc: asyncio.subprocess.Process, message: Mapping[str, Any], *, framing: str
) -> None:
    if proc.stdin is None:
        raise RuntimeError("MCP stdio process has no stdin")
    body = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if framing == "jsonl":
        proc.stdin.write(body + b"\n")
    elif framing == "content-length":
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        proc.stdin.write(header + body)
    else:  # pragma: no cover - internal defensive guard
        raise RuntimeError(f"unsupported MCP stdio framing: {framing}")
    await proc.stdin.drain()


async def _read_stdio_mcp_response(
    proc: asyncio.subprocess.Process,
    *,
    request_id: int,
    timeout: float,
    stderr_buffer: bytearray,
    framing: str,
) -> Mapping[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for MCP stdio response id {request_id}")
        message = await asyncio.wait_for(
            _read_stdio_mcp_message(proc, stderr_buffer=stderr_buffer, framing=framing),
            timeout=remaining,
        )
        if message.get("id") != request_id:
            continue
        error = message.get("error")
        if isinstance(error, Mapping):
            raise RuntimeError(str(error.get("message") or error))
        return message


async def _read_stdio_mcp_message(
    proc: asyncio.subprocess.Process, *, stderr_buffer: bytearray, framing: str
) -> Mapping[str, Any]:
    if proc.stdout is None:
        raise RuntimeError("MCP stdio process has no stdout")

    if framing == "content-length":
        return await _read_content_length_stdio_mcp_message(proc, stderr_buffer=stderr_buffer)
    if framing != "jsonl":  # pragma: no cover - internal defensive guard
        raise RuntimeError(f"unsupported MCP stdio framing: {framing}")

    while True:
        line = await proc.stdout.readline()
        if line == b"":
            stderr = _format_stdio_mcp_stderr(stderr_buffer)
            detail = f": {stderr}" if stderr else ""
            raise RuntimeError(f"MCP stdio process exited before response{detail}")
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(b"content-length:"):
            raise StdioMcpFramingMismatch("MCP stdio response used Content-Length framing")
        decoded = json.loads(stripped.decode("utf-8"))
        if not isinstance(decoded, Mapping):
            raise RuntimeError("MCP stdio response was not a JSON object")
        return decoded


async def _read_content_length_stdio_mcp_message(
    proc: asyncio.subprocess.Process, *, stderr_buffer: bytearray
) -> Mapping[str, Any]:
    if proc.stdout is None:
        raise RuntimeError("MCP stdio process has no stdout")

    while True:
        headers: dict[str, str] = {}
        while True:
            line = await proc.stdout.readline()
            if line == b"":
                stderr = _format_stdio_mcp_stderr(stderr_buffer)
                detail = f": {stderr}" if stderr else ""
                raise RuntimeError(f"MCP stdio process exited before response{detail}")
            stripped = line.strip()
            if not stripped:
                break
            name, separator, value = stripped.decode("ascii", errors="replace").partition(":")
            if not separator:
                raise RuntimeError("MCP stdio response header was malformed")
            headers[name.lower()] = value.strip()

        content_length = headers.get("content-length")
        if content_length is None:
            continue
        body = await proc.stdout.readexactly(int(content_length))
        decoded = json.loads(body.decode("utf-8"))
        if not isinstance(decoded, Mapping):
            raise RuntimeError("MCP stdio response was not a JSON object")
        return decoded


async def _drain_stdio_mcp_stderr(stderr: asyncio.StreamReader, stderr_buffer: bytearray) -> None:
    while True:
        chunk = await stderr.read(4096)
        if not chunk:
            return
        stderr_buffer.extend(chunk)
        if len(stderr_buffer) > _MCP_STDERR_TAIL_BYTES:
            del stderr_buffer[: len(stderr_buffer) - _MCP_STDERR_TAIL_BYTES]


def _format_stdio_mcp_stderr(stderr_buffer: bytearray) -> str:
    return bytes(stderr_buffer).decode("utf-8", errors="replace").strip()


async def _terminate_stdio_mcp_process(proc: asyncio.subprocess.Process) -> None:
    if proc.stdin is not None:
        proc.stdin.close()

    if os.name != "posix":  # pragma: no cover - exercised by Windows CI/runtime
        if proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except TimeoutError:
            proc.kill()
            await proc.wait()
        return

    process_group_id = proc.pid
    _signal_stdio_mcp_process(proc, force=False)
    forced = False
    if proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except TimeoutError:
            forced = True
    if not forced:
        try:
            await asyncio.wait_for(
                _wait_for_stdio_mcp_process_group_exit(process_group_id), timeout=2.0
            )
        except TimeoutError:
            forced = True
    if forced:
        _signal_stdio_mcp_process(proc, force=True)
        if proc.returncode is None:
            await proc.wait()


async def _wait_for_stdio_mcp_process_group_exit(process_group_id: int) -> None:
    """Wait until no wrapper descendant remains in the probe's POSIX group."""
    while True:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            # EPERM still proves that the group exists.  This can occur when
            # the session-leading wrapper exits before a surviving child and
            # the probe no longer owns permission to inspect that group.
            pass
        await asyncio.sleep(0.02)


def _signal_stdio_mcp_process(proc: asyncio.subprocess.Process, *, force: bool) -> None:
    """Stop the uvx wrapper and the MCP child that inherits its stdio pipes."""
    if os.name != "posix":  # pragma: no cover - exercised by Windows CI/runtime
        if force:
            proc.kill()
        else:
            proc.terminate()
        return

    try:
        os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        return
