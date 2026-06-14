"""Async ACP (Agent Client Protocol) stdio client for ourocode.

Ouroboros uses this to drive ``ourocode --acp`` — a sibling tool that streams a
Claude Pro/Max **OAuth** ``/v1/messages`` turn internally. That path uses
**neither** ``claude_agent_sdk`` **nor** ``claude -p`` (ourocode even forbids
spawning the ``claude`` CLI), so it is the SDK-free Claude backend Ouroboros
wants.

The wire protocol is newline-delimited JSON-RPC 2.0 over stdio
(https://agentclientprotocol.com/protocol/transports) — one compact JSON object
per line. A single turn is: ``initialize`` → ``session/new`` (absolute cwd) →
``session/prompt`` (text blocks), accumulating ``session/update`` /
``agent_message_chunk`` notifications until the ``session/prompt`` result carries
a ``stopReason``.

This client runs **one full turn per process** (spawn → handshake → prompt →
close). Per-call spawn keeps each completion isolated and sidesteps ourocode's
"one prompt turn at a time" constraint without any pool/lifecycle machinery.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
import contextlib
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
from typing import Any

from ouroboros.observability.logging import get_logger

log = get_logger(__name__)

# JSON-RPC error codes the ourocode ACP server emits.
_PARSE_ERROR = -32700
_INVALID_PARAMS = -32602
_METHOD_NOT_FOUND = -32601
_INTERNAL_ERROR = -32603

_MAX_LINE_BYTES = 16 * 1024 * 1024  # 16 MB guard per JSON-RPC frame


@dataclass(frozen=True, slots=True)
class AcpTurnResult:
    """The outcome of one ACP ``session/prompt`` turn."""

    text: str
    stop_reason: str
    session_id: str


class AcpClientError(RuntimeError):
    """Raised when an ACP turn cannot complete.

    ``error_type`` classifies the failure for the adapter so it can map it to a
    typed ``ProviderError`` (e.g. surface the not-signed-in case distinctly).
    """

    def __init__(
        self,
        message: str,
        *,
        error_type: str = "acp_error",
        code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_type = error_type
        self.code = code
        self.details = details or {}


# A substring the ourocode ACP server uses when the Claude backend has no
# signed-in session. Surfaced distinctly so callers can prompt the user to run
# ``ourocode`` interactively and sign in.
_NOT_SIGNED_IN_MARKER = "model backend unavailable"


class OurocodeAcpClient:
    """Run a single Claude turn through ``ourocode --acp``.

    Args:
        cli_path: Path to the ``ourocode`` executable. Defaults to ``ourocode``
            on PATH.
        cwd: Working directory for the ACP session. ``session/new`` requires an
            **absolute** path; a relative value is resolved against the process
            cwd.
        model: ourocode backend selector passed via ``OUROCODE_MODEL`` (``claude``
            → ourocode's ``:claude_api`` OAuth-streamed Claude).
        startup_timeout: Seconds to wait for the first ACP frame.
        turn_timeout: Seconds to wait for the ``session/prompt`` result.
    """

    _PROTOCOL_VERSION = 1

    def __init__(
        self,
        *,
        cli_path: str | Path | None = None,
        cwd: str | Path | None = None,
        model: str = "claude",
        startup_timeout: float | None = 30.0,
        turn_timeout: float | None = 600.0,
    ) -> None:
        self._cli_path = self._resolve_cli_path(cli_path)
        self._cwd = str(Path(cwd).expanduser().resolve()) if cwd is not None else os.getcwd()
        self._model = model
        # A ``None`` timeout would make ``asyncio.wait_for`` wait forever, which
        # is dangerous for a long-lived subprocess on a blocking readline.
        # Coalesce to bounded defaults so a stalled turn always fails closed.
        self._startup_timeout = 30.0 if startup_timeout is None else startup_timeout
        self._turn_timeout = 600.0 if turn_timeout is None else turn_timeout

    @staticmethod
    def _resolve_cli_path(cli_path: str | Path | None) -> str:
        if cli_path is not None:
            candidate = Path(cli_path).expanduser()
            return str(candidate) if candidate.exists() else str(cli_path)
        return shutil.which("ourocode") or "ourocode"

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        # The ACP process must not inherit Ouroboros runtime/backend selectors
        # (they would confuse a nested invocation), and OUROCODE_MODEL selects
        # the Claude backend for the whole ACP process.
        for key in ("OUROBOROS_AGENT_RUNTIME", "OUROBOROS_LLM_BACKEND"):
            env.pop(key, None)
        env["OUROCODE_MODEL"] = self._model
        return env

    async def run_turn(
        self,
        prompt_text: str,
        *,
        on_chunk: Callable[[str], None] | None = None,
    ) -> AcpTurnResult:
        """Execute one Claude turn and return the accumulated assistant text.

        Spawns ``ourocode --acp``, performs the initialize/session-new/prompt
        handshake, streams ``agent_message_chunk`` deltas (forwarded to
        ``on_chunk`` if given), and tears the process down before returning.
        """
        try:
            process = await asyncio.create_subprocess_exec(
                self._cli_path,
                "--acp",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
                env=self._build_env(),
                limit=_MAX_LINE_BYTES,
            )
        except (FileNotFoundError, OSError) as exc:
            raise AcpClientError(
                f"ourocode not found or not launchable: {exc}. "
                "Install ourocode and ensure it is on PATH (or set "
                "OUROBOROS_OUROCODE_CLI_PATH).",
                error_type="cli_unavailable",
            ) from exc

        assert process.stdin is not None
        assert process.stdout is not None
        try:
            await self._request(
                process, request_id=1, method="initialize", params={}, timeout=self._startup_timeout
            )
            session_result = await self._request(
                process,
                request_id=2,
                method="session/new",
                params={"cwd": self._cwd},
                timeout=self._startup_timeout,
            )
            session_id = session_result.get("sessionId")
            if not isinstance(session_id, str) or not session_id:
                raise AcpClientError(
                    "ourocode session/new returned no sessionId",
                    error_type="malformed_response",
                    details={"result": session_result},
                )

            chunks: list[str] = []

            def _collect(method: str, params: Mapping[str, Any]) -> None:
                text = self._chunk_text(method, params, session_id)
                if text:
                    chunks.append(text)
                    if on_chunk is not None:
                        on_chunk(text)

            prompt_result = await self._request(
                process,
                request_id=3,
                method="session/prompt",
                params={
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": prompt_text}],
                },
                timeout=self._turn_timeout,
                on_notification=_collect,
            )
            stop_reason = prompt_result.get("stopReason")
            if not isinstance(stop_reason, str):
                stop_reason = "end_turn"
            return AcpTurnResult(
                text="".join(chunks), stop_reason=stop_reason, session_id=session_id
            )
        finally:
            await self._terminate(process)

    async def _request(
        self,
        process: asyncio.subprocess.Process,
        *,
        request_id: int,
        method: str,
        params: Mapping[str, Any],
        timeout: float,
        on_notification: Callable[[str, Mapping[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Send one JSON-RPC request and read frames until its result/error.

        Notifications received before the matching result are dispatched to
        ``on_notification`` (used to stream ``agent_message_chunk`` deltas).
        """
        assert process.stdin is not None
        frame = json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)},
            separators=(",", ":"),
        )
        try:
            process.stdin.write(frame.encode("utf-8") + b"\n")
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise AcpClientError(
                f"ourocode ACP process closed stdin during {method}: {exc}",
                error_type="process_died",
            ) from exc

        try:
            return await asyncio.wait_for(
                self._read_until_result(process, request_id, method, on_notification),
                timeout=timeout,
            )
        except TimeoutError as exc:
            raise AcpClientError(
                f"ourocode ACP {method} timed out after {timeout}s",
                error_type="timeout",
            ) from exc

    async def _read_until_result(
        self,
        process: asyncio.subprocess.Process,
        request_id: int,
        method: str,
        on_notification: Callable[[str, Mapping[str, Any]], None] | None,
    ) -> dict[str, Any]:
        assert process.stdout is not None
        while True:
            raw = await process.stdout.readline()
            if not raw:
                stderr = await self._read_stderr_tail(process)
                raise AcpClientError(
                    f"ourocode ACP closed stdout before answering {method}",
                    error_type="process_died",
                    details={"stderr": stderr},
                )
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AcpClientError(
                    f"ourocode ACP emitted a malformed JSON frame: {exc.msg}",
                    error_type="malformed_frame",
                    details={"line": line[:240]},
                ) from exc
            if not isinstance(message, dict):
                continue

            if message.get("id") == request_id and ("result" in message or "error" in message):
                if "error" in message:
                    err = message["error"] if isinstance(message["error"], dict) else {}
                    raise self._error_from_rpc(method, err)
                result = message["result"]
                return result if isinstance(result, dict) else {}

            # A notification (no id) — stream chunks; ignore unrelated frames.
            if "id" not in message and isinstance(message.get("method"), str):
                if on_notification is not None:
                    params = message.get("params")
                    on_notification(message["method"], params if isinstance(params, dict) else {})

    def _chunk_text(self, method: str, params: Mapping[str, Any], session_id: str) -> str | None:
        """Extract the text delta from a ``session/update`` agent-message chunk.

        ourocode shape:
        ``{"method":"session/update","params":{"sessionId":...,"update":
        {"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":...}}}}``
        """
        if method != "session/update":
            return None
        if params.get("sessionId") not in (None, session_id):
            return None
        update = params.get("update")
        if not isinstance(update, dict) or update.get("sessionUpdate") != "agent_message_chunk":
            return None
        content = update.get("content")
        if isinstance(content, dict):
            text = content.get("text")
            return text if isinstance(text, str) else None
        # Tolerate a bare-string content for forward/backward compatibility.
        return content if isinstance(content, str) else None

    def _error_from_rpc(self, method: str, err: Mapping[str, Any]) -> AcpClientError:
        message = err.get("message")
        message = message if isinstance(message, str) else "unknown ACP error"
        code = err.get("code")
        code = code if isinstance(code, int) else None
        if _NOT_SIGNED_IN_MARKER in message.lower():
            return AcpClientError(
                "ourocode's Claude backend is not signed in. Run `ourocode` "
                "interactively and sign in (its /login-claude flow), then retry.",
                error_type="not_signed_in",
                code=code,
                details={"method": method, "rpc_message": message},
            )
        return AcpClientError(
            f"ourocode ACP {method} failed: {message}",
            error_type="rpc_error",
            code=code,
            details={"method": method},
        )

    @staticmethod
    async def _read_stderr_tail(process: asyncio.subprocess.Process) -> str:
        if process.stderr is None:
            return ""
        try:
            data = await asyncio.wait_for(process.stderr.read(), timeout=1.0)
        except Exception:  # noqa: BLE001 - best-effort diagnostics only
            return ""
        return data.decode("utf-8", errors="replace")[-4000:]

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except TimeoutError:
            process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
        except ProcessLookupError:
            pass


__all__ = ["AcpClientError", "AcpTurnResult", "OurocodeAcpClient"]
