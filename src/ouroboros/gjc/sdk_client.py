"""Typed GJC Coordinator MCP client for Ouroboros backends.

GJC removed its legacy ``--mode rpc`` transport. This module is the single
translation boundary between Ouroboros's backend-neutral interfaces and GJC's
supported Broker -> SessionRouter -> AgentSession SDK lifecycle.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from ouroboros.mcp.client.adapter import MCPClientAdapter
from ouroboros.mcp.types import MCPServerConfig, MCPToolResult, TransportType

_SERVER_NAME = "gjc-coordinator"
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "superseded"})


class GjcCoordinatorError(RuntimeError):
    """A public-safe failure returned by the GJC Coordinator MCP boundary."""

    def __init__(
        self, message: str, *, code: str = "unavailable", details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class GjcCoordinatorSession:
    session_id: str
    turn_id: str


@dataclass(frozen=True, slots=True)
class GjcCoordinatorQuestion:
    session_id: str
    turn_id: str
    question_id: str
    answer_binding: str
    prompt: str
    options: tuple[str, ...]
    multi: bool


@dataclass(frozen=True, slots=True)
class GjcCoordinatorTurn:
    session_id: str
    turn_id: str
    status: str
    text: str
    error: str | None
    question: GjcCoordinatorQuestion | None
    raw: dict[str, Any]

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    @property
    def succeeded(self) -> bool:
        return self.status == "completed" and self.error is None


AdapterFactory = Callable[[], MCPClientAdapter]


class GjcCoordinatorClient:
    """Drive one GJC backend connection through the supported Coordinator MCP."""

    def __init__(
        self,
        *,
        cli_path: str | Path,
        cwd: str | Path,
        timeout: float = 600.0,
        adapter_factory: AdapterFactory = MCPClientAdapter,
    ) -> None:
        self._cli_path = str(cli_path)
        self._cwd = str(Path(cwd).expanduser().resolve())
        self._timeout = max(timeout, 1.0)
        self._adapter = adapter_factory()
        self._connected = False

    async def __aenter__(self) -> GjcCoordinatorClient:
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    def _server_config(self) -> MCPServerConfig:
        repo_digest = hashlib.sha256(self._cwd.encode("utf-8")).hexdigest()[:16]
        state_root = Path(self._cwd) / ".gjc" / "state" / "ouroboros-coordinator" / repo_digest
        env = os.environ.copy()
        env.update(
            {
                "GJC_COORDINATOR_MCP_WORKDIR_ROOTS": self._cwd,
                "GJC_COORDINATOR_MCP_MUTATIONS": "sessions,questions",
                "GJC_COORDINATOR_MCP_SESSION_COMMAND": "gjc",
                "GJC_COORDINATOR_MCP_FORCE_STOP": "true",
                "GJC_COORDINATOR_MCP_PROFILE": "ouroboros",
                "GJC_COORDINATOR_MCP_REPO": repo_digest,
                "GJC_COORDINATOR_MCP_STATE_ROOT": str(state_root),
            }
        )
        return MCPServerConfig(
            name=_SERVER_NAME,
            transport=TransportType.STDIO,
            command=self._cli_path,
            args=("mcp-serve", "coordinator"),
            env=env,
            timeout=min(self._timeout, 60.0),
        )

    async def connect(self) -> None:
        if self._connected:
            return
        result = await self._adapter.connect(self._server_config())
        if result.is_err:
            raise GjcCoordinatorError(
                f"Could not connect to GJC Coordinator MCP: {result.error}",
                code="connection_failed",
            )
        self._connected = True

    async def close(self) -> None:
        if not self._connected:
            return
        try:
            async with asyncio.timeout(5.0):
                result = await self._adapter.disconnect()
        except TimeoutError as exc:
            self._connected = False
            raise GjcCoordinatorError(
                "Timed out closing GJC Coordinator MCP.", code="close_failed"
            ) from exc
        self._connected = False
        if result.is_err:
            raise GjcCoordinatorError(
                f"Could not close GJC Coordinator MCP: {result.error}",
                code="close_failed",
            )

    @staticmethod
    def _result_payload(result: MCPToolResult) -> dict[str, Any]:
        structured = result.structured_content
        if isinstance(structured, dict):
            return dict(structured)
        text = result.text_content.strip()
        if not text:
            raise GjcCoordinatorError(
                "GJC Coordinator MCP returned an empty tool result.", code="empty_result"
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GjcCoordinatorError(
                "GJC Coordinator MCP returned malformed JSON.",
                code="malformed_result",
                details={"preview": text[:240]},
            ) from exc
        if not isinstance(payload, dict):
            raise GjcCoordinatorError(
                "GJC Coordinator MCP result is not an object.", code="malformed_result"
            )
        return payload

    async def _call(
        self, tool: str, arguments: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        if not self._connected:
            await self.connect()
        try:
            async with asyncio.timeout(timeout or self._timeout):
                result = await self._adapter.call_tool(tool, arguments)
        except TimeoutError as exc:
            raise GjcCoordinatorError(
                f"GJC Coordinator MCP tool timed out: {tool}",
                code="timeout",
                details={"tool": tool},
            ) from exc
        if result.is_err:
            raise GjcCoordinatorError(
                f"GJC Coordinator MCP tool failed: {result.error}",
                code="tool_failed",
                details={"tool": tool},
            )
        payload = self._result_payload(result.value)
        if payload.get("ok") is not True:
            error = payload.get("error")
            code = (
                payload.get("reason") if isinstance(payload.get("reason"), str) else "unavailable"
            )
            message = f"GJC Coordinator tool {tool} failed"
            if isinstance(error, dict):
                if isinstance(error.get("code"), str):
                    code = error["code"]
                if isinstance(error.get("message"), str):
                    message = error["message"]
            elif isinstance(payload.get("reason"), str):
                message = payload["reason"]
            raise GjcCoordinatorError(
                message, code=code, details={"tool": tool, "payload": payload}
            )
        return payload

    @staticmethod
    def _key(prefix: str) -> str:
        return f"ouroboros-{prefix}-{uuid4().hex}"

    async def start_session(
        self,
        prompt: str,
        *,
        model: str | None = None,
        mpreset: str | None = None,
    ) -> GjcCoordinatorSession:
        arguments: dict[str, Any] = {
            "cwd": self._cwd,
            "prompt": prompt,
            "idempotency_key": self._key("start"),
            "allow_mutation": True,
        }
        if model and model != "default":
            arguments["model"] = model
        if mpreset:
            arguments["mpreset"] = mpreset
        payload = await self._call("gjc_coordinator_start_session", arguments)
        session = payload.get("session")
        session_id = session.get("session_id") if isinstance(session, dict) else None
        turn_id = payload.get("turn_id")
        if not isinstance(session_id, str) or not isinstance(turn_id, str):
            raise GjcCoordinatorError(
                "GJC Coordinator start response omitted session or turn identity.",
                code="malformed_result",
                details={"payload": payload},
            )
        return GjcCoordinatorSession(session_id=session_id, turn_id=turn_id)

    async def send_prompt(self, session_id: str, prompt: str, *, queue: bool = False) -> str:
        payload = await self._call(
            "gjc_coordinator_send_prompt",
            {
                "session_id": session_id,
                "prompt": prompt,
                "queue": queue,
                "idempotency_key": self._key("prompt"),
                "allow_mutation": True,
            },
        )
        turn_id = payload.get("turn_id")
        if not isinstance(turn_id, str):
            raise GjcCoordinatorError(
                "GJC prompt response omitted turn identity.", code="malformed_result"
            )
        return turn_id

    async def await_turn(self, session_id: str, turn_id: str) -> GjcCoordinatorTurn:
        payload = await self._call(
            "gjc_coordinator_await_turn",
            {
                "session_id": session_id,
                "turn_id": turn_id,
                "timeout_ms": min(int(self._timeout * 1000), 30 * 60 * 1000),
                "poll_interval_ms": 200,
            },
            timeout=self._timeout + 5.0,
        )
        turn = payload.get("turn")
        if not isinstance(turn, dict):
            raise GjcCoordinatorError("GJC turn response is missing.", code="malformed_result")
        status = turn.get("status")
        if not isinstance(status, str):
            raise GjcCoordinatorError("GJC turn status is missing.", code="malformed_result")
        final_response = turn.get("final_response")
        text = final_response.get("text") if isinstance(final_response, dict) else None
        error_payload = turn.get("error")
        error = error_payload.get("message") if isinstance(error_payload, dict) else None
        if status == "completed" and isinstance(text, str) and text.strip():
            error = None
        question = (
            await self.pending_question(session_id, turn_id)
            if status == "waiting_for_answer"
            else None
        )
        return GjcCoordinatorTurn(
            session_id=session_id,
            turn_id=turn_id,
            status=status,
            text=text if isinstance(text, str) else "",
            error=error if isinstance(error, str) else None,
            question=question,
            raw=payload,
        )

    async def read_last_assistant(self, session_id: str, *, lines: int = 400) -> str:
        payload = await self._call(
            "gjc_coordinator_read_tail",
            {"session_id": session_id, "lines": lines},
        )
        values = payload.get("lines")
        if not isinstance(values, list) or not all(isinstance(line, str) for line in values):
            raise GjcCoordinatorError("GJC assistant output is malformed.", code="malformed_result")
        return "\n".join(values).strip()

    async def pending_question(
        self, session_id: str, turn_id: str
    ) -> GjcCoordinatorQuestion | None:
        payload = await self._call(
            "gjc_coordinator_list_questions",
            {"session_id": session_id, "turn_id": turn_id, "status": "pending"},
        )
        questions = payload.get("questions")
        if not isinstance(questions, list) or not questions:
            return None
        question = questions[0]
        if not isinstance(question, dict):
            return None
        required = ("question_id", "answer_binding", "prompt")
        if not all(isinstance(question.get(key), str) for key in required):
            return None
        options = question.get("options")
        labels = (
            tuple(
                option["label"]
                for option in options
                if isinstance(option, dict) and isinstance(option.get("label"), str)
            )
            if isinstance(options, list)
            else ()
        )
        return GjcCoordinatorQuestion(
            session_id=session_id,
            turn_id=turn_id,
            question_id=question["question_id"],
            answer_binding=question["answer_binding"],
            prompt=question["prompt"],
            options=labels,
            multi=question.get("multi") is True,
        )

    async def submit_question_answer(self, question: GjcCoordinatorQuestion, answer: str) -> None:
        await self._call(
            "gjc_coordinator_submit_question_answer",
            {
                "session_id": question.session_id,
                "turn_id": question.turn_id,
                "question_id": question.question_id,
                "answer_binding": question.answer_binding,
                "answer": {"selected": [], "other": True, "custom": answer},
                "idempotency_key": self._key("answer"),
                "allow_mutation": True,
            },
        )

    async def stop_session(self, session_id: str) -> None:
        try:
            await self._call(
                "gjc_coordinator_stop_session",
                {"session_id": session_id, "force": True, "allow_mutation": True},
                timeout=min(self._timeout, 30.0),
            )
        except GjcCoordinatorError as exc:
            if exc.code not in {"not_found", "resource_gone"}:
                raise
