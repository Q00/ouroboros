"""Copilot's preview ACP protocol boundary: one process, one streamed turn.

No ACP SDK dependency is required. JSON-RPC requests, notifications, and
server-to-client requests share a bounded NDJSON channel. Only stdio is used.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
import contextlib
import json
import math
import os
import re
from typing import Any

from ouroboros.providers.codex_cli_stream import terminate_runtime_process
from ouroboros.providers.ourocode_acp_client import AcpClientError

_MAX_FRAME_BYTES = 8 * 1024 * 1024
_AUTH_MARKERS = (
    "authentication",
    "not authenticated",
    "not signed in",
    "not logged in",
    "unauthenticated",
    "not authorized",
    "authorization",
    "login required",
    "unauthorized",
    "forbidden",
    "copilot login",
    "missing token",
    "invalid token",
    "access denied",
    "permission denied",
    "organization policy",
    "enterprise policy",
)


def _failure(
    message: str,
    error_type: str = "protocol_error",
    *,
    code: int | None = None,
    auth_context: str = "",
) -> AcpClientError:
    context = f"{message}\n{auth_context}".lower()
    if (
        code in {-32000, 401, 403}
        or any(word in context for word in _AUTH_MARKERS)
        or re.search(r"\b(?:401|403)\b", context)
    ):
        error_type = "authentication_error"
        if auth_context:
            message += " Authentication/authorization failed; check Copilot login and policy."
    return AcpClientError(message, error_type=error_type, code=code)


class CopilotAcpClient:
    """Own a Copilot child and stream protocol frames without buffering a turn."""

    def __init__(
        self,
        command: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        permission_handler: Callable[[Mapping[str, Any]], dict[str, Any]],
        startup_timeout: float = 60.0,
        idle_timeout: float = 300.0,
        shutdown_timeout: float = 5.0,
    ) -> None:
        for value in (startup_timeout, idle_timeout, shutdown_timeout):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("ACP timeouts must be finite positive seconds")
        self.command = command
        self.cwd = cwd
        self.env = env
        self.permission_handler = permission_handler
        self.startup_timeout = startup_timeout
        self.idle_timeout = idle_timeout
        self.shutdown_timeout = shutdown_timeout
        self.process: asyncio.subprocess.Process | None = None
        self.session_id: str | None = None
        self.prompt_sent = False
        self.completed = False
        self.startup_updates: list[dict[str, Any]] = []
        self._next_id = 0
        self._stderr: deque[bytes] = deque(maxlen=8)
        self._stderr_task: asyncio.Task[None] | None = None
        self._closed = False

    async def __aenter__(self) -> CopilotAcpClient:
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *self.command,
                cwd=self.cwd,
                env=self.env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
                limit=_MAX_FRAME_BYTES,
            )
        )
        try:
            self.process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            # Cancellation must not lose ownership between spawn and assignment.
            with contextlib.suppress(OSError):
                self.process = await spawn
                await self.close()
            raise
        except OSError as exc:
            raise _failure(f"Cannot start Copilot ACP: {exc}", "cli_unavailable") from exc
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while chunk := await self.process.stderr.read(4096):
            self._stderr.append(chunk)

    async def _send(self, frame: dict[str, Any]) -> None:
        assert self.process is not None and self.process.stdin is not None
        try:
            data = json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(data) > _MAX_FRAME_BYTES:
                raise _failure("ACP request exceeds the frame limit", "invalid_request")
            self.process.stdin.write(data + b"\n")
            await asyncio.wait_for(self.process.stdin.drain(), self.startup_timeout)
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise _failure("Copilot ACP closed its input", "process_exited") from exc
        except TimeoutError as exc:
            raise _failure("Copilot ACP input stalled", "timeout") from exc

    async def _read(self) -> dict[str, Any]:
        assert self.process is not None and self.process.stdout is not None
        try:
            line = await asyncio.wait_for(self.process.stdout.readline(), self.idle_timeout)
        except TimeoutError as exc:
            raise _failure("Copilot ACP stopped sending activity", "timeout") from exc
        except (ValueError, asyncio.LimitOverrunError) as exc:
            raise _failure(
                "Copilot ACP frame exceeds the size limit", "malformed_response"
            ) from exc
        if not line:
            # Let the independent stderr reader publish already-buffered diagnostics.
            if self._stderr_task is not None:
                await asyncio.wait({self._stderr_task}, timeout=0.1)
            detail = b"".join(self._stderr).decode("utf-8", errors="replace")[-4096:].strip()
            raise _failure(
                f"Copilot ACP closed stdout before completing the request. {detail}".strip(),
                "process_exited",
            )
        if not line.endswith(b"\n"):
            raise _failure(
                "Copilot ACP returned a truncated NDJSON frame",
                "malformed_response",
                auth_context=line.decode("utf-8", errors="replace"),
            )
        try:
            frame = json.loads(line)
        except (ValueError, UnicodeDecodeError) as exc:
            raise _failure(
                "Copilot ACP returned invalid JSON",
                "malformed_response",
                auth_context=line.decode("utf-8", errors="replace"),
            ) from exc
        if not isinstance(frame, dict) or frame.get("jsonrpc") != "2.0":
            raise _failure(
                "Copilot ACP returned an invalid JSON-RPC envelope",
                "malformed_response",
                auth_context=line.decode("utf-8", errors="replace"),
            )
        frame.pop("_permission_result", None)
        return frame

    async def _exchange(self, method: str, params: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        self._next_id += 1
        request_id = self._next_id
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            frame = await self._read()
            if "method" in frame:
                if not isinstance(frame["method"], str) or not isinstance(
                    frame.get("params", {}), dict
                ):
                    raise _failure("Invalid ACP notification or request", "malformed_response")
                if "id" in frame:
                    if type(frame["id"]) not in (str, int):
                        raise _failure("Invalid ACP server request id", "malformed_response")
                    response: dict[str, Any] = {"jsonrpc": "2.0", "id": frame["id"]}
                    request_params = frame.get("params", {})
                    if frame["method"] == "session/request_permission":
                        decision = {"outcome": {"outcome": "cancelled"}}
                        if self.session_id and request_params.get("sessionId") == self.session_id:
                            decision = self.permission_handler(request_params)
                        response["result"] = decision
                        # Client-owned audit data, not part of Copilot's wire schema.
                        frame = {**frame, "_permission_result": decision}
                    else:
                        response["error"] = {
                            "code": -32601,
                            "message": "This ACP client does not provide that capability",
                        }
                    await self._send(response)
                yield frame
                continue
            if type(frame.get("id")) is not int or frame["id"] != request_id:
                raise _failure(
                    "Copilot ACP returned an unexpected response id", "malformed_response"
                )
            if ("error" in frame) == ("result" in frame):
                raise _failure("Copilot ACP response needs result or error", "malformed_response")
            if "error" in frame:
                error = frame["error"]
                if not isinstance(error, dict):
                    raise _failure("Malformed ACP error response", "malformed_response")
                code = error.get("code")
                detail = error.get("message", "Unknown error")
                raise _failure(
                    f"Copilot ACP {method} failed: {str(detail)[:4096]}",
                    "rpc_error",
                    code=code if type(code) is int else None,
                    auth_context=json.dumps(error.get("data", "")),
                )
            if not isinstance(frame["result"], dict):
                raise _failure("Copilot ACP returned a non-object result", "malformed_response")
            yield frame
            return

    async def _startup_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            async with asyncio.timeout(self.startup_timeout):
                async for frame in self._exchange(method, params):
                    if "method" not in frame:
                        return frame["result"]
                    if len(self.startup_updates) >= 256:
                        raise _failure("Too many ACP startup notifications", "malformed_response")
                    self.startup_updates.append(frame)
        except TimeoutError as exc:
            raise _failure(f"Copilot ACP {method} timed out", "timeout") from exc
        raise _failure(f"Copilot ACP {method} returned no result", "malformed_response")

    async def initialize(self) -> dict[str, Any]:
        result = await self._startup_request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "ouroboros", "version": "1"},
            },
        )
        if type(result.get("protocolVersion")) is not int or result["protocolVersion"] != 1:
            raise _failure("Copilot ACP protocol version is incompatible", "protocol_incompatible")
        if not isinstance(result.get("agentCapabilities"), dict):
            raise _failure("Copilot ACP initialize omitted agentCapabilities", "malformed_response")
        return result

    async def new_session(self) -> dict[str, Any]:
        result = await self._startup_request("session/new", {"cwd": self.cwd, "mcpServers": []})
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not session_id.strip():
            raise _failure("Copilot ACP session/new omitted sessionId", "malformed_response")
        self.session_id = session_id
        return result

    async def prompt(self, text: str) -> AsyncIterator[dict[str, Any]]:
        if not self.session_id or self.prompt_sent:
            raise _failure("ACP requires a fresh initialized session", "invalid_request")
        # Once writing may have begun, retry/fallback could execute the task twice.
        self.prompt_sent = True
        async for frame in self._exchange(
            "session/prompt",
            {"sessionId": self.session_id, "prompt": [{"type": "text", "text": text}]},
        ):
            if "method" not in frame:
                stop_reason = frame["result"].get("stopReason")
                if not isinstance(stop_reason, str) or not stop_reason:
                    raise _failure("Copilot ACP prompt omitted stopReason", "malformed_response")
                self.completed = True
            yield frame

    async def cancel(self) -> None:
        if self.session_id and self.prompt_sent and not self.completed:
            with contextlib.suppress(AcpClientError, TimeoutError):
                async with asyncio.timeout(1):
                    await self._send(
                        {
                            "jsonrpc": "2.0",
                            "method": "session/cancel",
                            "params": {"sessionId": self.session_id},
                        }
                    )

    async def close(self) -> None:
        """Cancel unfinished work, close stdin, and reap the owned process group."""
        if self._closed or self.process is None:
            return
        self._closed = True
        process = self.process
        pgid = process.pid if os.name == "posix" else None
        try:
            await self.cancel()
            if process.stdin is not None:
                process.stdin.close()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError, TimeoutError):
                    await asyncio.wait_for(process.stdin.wait_closed(), 1)
            try:
                await asyncio.wait_for(process.wait(), self.shutdown_timeout)
            except TimeoutError:
                await terminate_runtime_process(
                    process,
                    shutdown_timeout=self.shutdown_timeout,
                    terminate_process_group=pgid is not None,
                    process_group_id=pgid,
                )
        finally:
            # Also reap companion shells if the server exited before its children.
            await terminate_runtime_process(
                process,
                shutdown_timeout=0.2,
                terminate_process_group=pgid is not None,
                process_group_id=pgid,
            )
            if self._stderr_task is not None:
                self._stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._stderr_task


__all__ = ["AcpClientError", "CopilotAcpClient"]
