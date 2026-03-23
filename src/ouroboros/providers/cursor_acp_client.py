"""Shared Cursor ACP (Agent Client Protocol) client.

Manages a single ``cursor-agent acp`` child process and provides
low-level JSON-RPC communication.  Both :class:`CursorACPAdapter`
(LLM completions) and ``CursorACPRuntime`` (agent execution) share
the same client instance so that only one ``cursor-agent`` process
is needed per MCP server.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import structlog

from ouroboros.core.errors import ProviderError

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ACPModel:
    """An available model in an ACP session."""

    model_id: str
    name: str


@dataclass(frozen=True, slots=True)
class ACPSession:
    """Result of creating an ACP session."""

    session_id: str
    available_models: tuple[ACPModel, ...]
    current_model_id: str


class CursorACPClient:
    """Low-level ACP JSON-RPC client over stdin/stdout.

    Manages the ``cursor-agent acp`` child process lifecycle and
    provides request/response and streaming notification primitives.
    """

    def __init__(self, cli_path: str | None = None) -> None:
        self._cli_path = cli_path or self._resolve_cli_path()
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._lock = asyncio.Lock()

    @staticmethod
    def _resolve_cli_path() -> str:
        import shutil

        path = shutil.which("cursor-agent")
        if path:
            return path
        home = Path.home()
        for candidate in (
            home / ".local" / "bin" / "cursor-agent",
            Path("/usr/local/bin/cursor-agent"),
        ):
            if candidate.exists():
                return str(candidate)
        return "cursor-agent"

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def ensure_started(self) -> None:
        """Start the ACP process if not already running."""
        if self.is_alive:
            return
        await self._start_process()
        await self._initialize()

    async def _start_process(self) -> None:
        if self._process and self._process.returncode is None:
            self._process.terminate()
            await self._process.wait()

        self._process = await asyncio.create_subprocess_exec(
            self._cli_path,
            "acp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=10 * 1024 * 1024,  # 10MB buffer for large edit responses
        )
        self._request_id = 0
        log.info("cursor_acp.process_started", pid=self._process.pid)

    async def _initialize(self) -> None:
        await self.request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {},
            "clientInfo": {"name": "ouroboros", "version": "0.26.0"},
        })

    # ── Low-level JSON-RPC ────────────────────────────────────────────

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _write(self, msg: dict[str, Any]) -> None:
        assert self._process and self._process.stdin
        self._process.stdin.write((json.dumps(msg) + "\n").encode())
        await self._process.stdin.drain()

    async def _readline(self, timeout: float = 30) -> dict[str, Any]:
        assert self._process and self._process.stdout
        line = await asyncio.wait_for(
            self._process.stdout.readline(), timeout=timeout
        )
        if not line:
            raise ProviderError(
                "cursor-agent ACP process closed unexpectedly",
                provider="cursor_acp",
            )
        return json.loads(line.decode())

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a request and wait for the matching response.

        Acquires the I/O lock so that concurrent callers cannot
        steal each other's JSON-RPC frames.
        """
        async with self._lock:
            req_id = self._next_id()
            await self._write({
                "jsonrpc": "2.0", "id": req_id,
                "method": method, "params": params,
            })
            while True:
                data = await self._readline()
                if data.get("id") == req_id:
                    if "error" in data:
                        raise ProviderError(
                            data["error"].get("message", "ACP error"),
                            provider="cursor_acp",
                            details=data["error"],
                        )
                    return data.get("result", {})

    # ── Session management ────────────────────────────────────────────

    async def create_session(self, cwd: str, *, mode: str = "agent") -> ACPSession:
        """Create a new ACP session. Returns session info with available models.

        Args:
            cwd: Working directory.
            mode: Session mode — ``agent`` (full tool access, default) or
                  ``ask`` (Q&A only, no tool use).
        """
        result = await self.request("session/new", {
            "cwd": cwd,
            "mcpServers": [],
        })

        # Set mode (default: ask — pure text, no tool calls)
        session_id_tmp = result["sessionId"]
        try:
            await self.request("session/set_config_option", {
                "sessionId": session_id_tmp,
                "configId": "mode",
                "value": mode,
            })
        except ProviderError:
            pass  # Mode setting not critical
        session_id = result["sessionId"]
        raw_models = result.get("models", {}).get("availableModels", [])
        current = result.get("models", {}).get("currentModelId", "default[]")

        models = tuple(
            ACPModel(model_id=m["modelId"], name=m["name"])
            for m in raw_models
        )

        log.info("cursor_acp.session_created", session_id=session_id)
        return ACPSession(
            session_id=session_id,
            available_models=models,
            current_model_id=current,
        )

    async def set_model(self, session_id: str, model_id: str) -> None:
        """Change the model for an ACP session via ``session/set_config_option``."""
        await self.request("session/set_config_option", {
            "sessionId": session_id,
            "configId": "model",
            "value": model_id,
        })
        log.info("cursor_acp.model_set", session_id=session_id, model=model_id)

    # ── Prompt streaming ──────────────────────────────────────────────

    async def prompt_stream(
        self,
        session_id: str,
        text: str,
        *,
        timeout: float = 300,
        permission_mode: str = "bypass",
    ) -> AsyncIterator[dict[str, Any]]:
        """Send a prompt and yield ACP update notifications until completion.

        Acquires the I/O lock for the entire prompt lifecycle so that
        concurrent readers cannot steal frames.

        Args:
            session_id: ACP session to prompt.
            text: The prompt text.
            timeout: Maximum seconds to wait for the full response.
            permission_mode: How to handle server-initiated permission
                requests.  ``"bypass"`` auto-approves everything.
                ``"default"`` / ``"acceptEdits"`` approve read/write
                but deny shell commands.  Any other value denies all.
        """
        async with self._lock:
            req_id = self._next_id()
            await self._write({
                "jsonrpc": "2.0", "id": req_id,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
            })

            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise ProviderError(
                        f"ACP prompt timed out after {timeout}s",
                        provider="cursor_acp",
                    )
                data = await self._readline(timeout=min(remaining, 30))

                if "method" in data and data["method"] == "session/update":
                    update = data.get("params", {}).get("update", {})
                    yield update
                    continue

                # Permission request — respect permission_mode
                if "method" in data and "id" in data:
                    approved = self._evaluate_permission(
                        data, permission_mode,
                    )
                    await self._write({
                        "jsonrpc": "2.0", "id": data["id"],
                        "result": {"approved": approved},
                    })
                    continue

                if data.get("id") == req_id:
                    if "error" in data:
                        raise ProviderError(
                            data["error"].get("message", "ACP prompt error"),
                            provider="cursor_acp",
                            details=data["error"],
                        )
                    return

    @staticmethod
    def _evaluate_permission(
        data: dict[str, Any], mode: str,
    ) -> bool:
        """Decide whether to approve an ACP permission request.

        ``bypass`` approves everything.  ``default`` / ``acceptEdits``
        approve file reads/writes but deny shell execution.  Any other
        mode denies all requests.
        """
        if mode == "bypass":
            return True

        method = data.get("method", "")
        params = data.get("params", {})
        kind = params.get("type", method)

        # Accept file operations, deny shell/command execution
        if mode in ("default", "acceptEdits"):
            deny_kinds = {"command", "terminal", "shell", "execute"}
            return not any(k in kind.lower() for k in deny_kinds)

        return False

    # ── Cleanup ───────────────────────────────────────────────────────

    async def close(self) -> None:
        """Terminate the ACP child process."""
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
            log.info("cursor_acp.process_closed")

    def __del__(self) -> None:
        if self._process and self._process.returncode is None:
            self._process.terminate()


# ── Module-level singleton ────────────────────────────────────────────

_shared_client: CursorACPClient | None = None


def get_shared_acp_client(cli_path: str | None = None) -> CursorACPClient:
    """Return the module-level shared ACP client, creating it if needed."""
    global _shared_client
    if _shared_client is None or not _shared_client.is_alive:
        _shared_client = CursorACPClient(cli_path=cli_path)
    return _shared_client
