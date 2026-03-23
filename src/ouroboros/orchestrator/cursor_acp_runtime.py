"""Cursor ACP-based agent runtime for Ouroboros orchestrator execution.

Uses the shared :class:`CursorACPClient` to execute agent tasks via
persistent ``cursor-agent acp`` sessions.  Unlike the CLI-based
:class:`CursorAgentRuntime`, this runtime:

- Reuses a single ``cursor-agent`` process (no per-task spawn overhead).
- Streams ``session/update`` notifications as :class:`AgentMessage` yields.
- Handles tool calls (file edits, shell commands) via ACP protocol.
- Supports session resume via ``session/load``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.orchestrator.adapter import (
    AgentMessage,
    RuntimeHandle,
    TaskResult,
)
from ouroboros.providers.cursor_acp_client import CursorACPClient, get_shared_acp_client

log = structlog.get_logger(__name__)


class CursorACPRuntime:
    """Agent runtime using Cursor ACP for task execution.

    Satisfies the ``AgentRuntime`` protocol used by the orchestrator.
    """

    def __init__(
        self,
        cli_path: str | Path | None = None,
        permission_mode: str | None = None,
        model: str | None = None,
        cwd: str | Path | None = None,
        skills_dir: str | Path | None = None,
        skill_dispatcher: Any = None,
        llm_backend: str | None = None,
    ) -> None:
        self._cwd = str(cwd) if cwd else str(Path.cwd())
        self._permission_mode = permission_mode or "acceptEdits"
        self._model = model
        self._client: CursorACPClient = get_shared_acp_client(
            str(cli_path) if cli_path else None,
        )

    # ── AgentRuntime protocol properties ──────────────────────────────

    @property
    def runtime_backend(self) -> str:
        return "cursor_acp"

    @property
    def working_directory(self) -> str | None:
        return self._cwd

    @property
    def permission_mode(self) -> str | None:
        return self._permission_mode

    # ── Execute task ──────────────────────────────────────────────────

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> AsyncIterator[AgentMessage]:
        """Execute a task via ACP and stream AgentMessage updates."""
        await self._client.ensure_started()

        # Resume or create session
        native_sid = (
            (resume_handle.native_session_id if resume_handle else None)
            or resume_session_id
        )
        if native_sid:
            try:
                session_id = await self._resume_session(native_sid)
            except ProviderError:
                log.warning(
                    "cursor_acp.resume_failed_creating_new",
                    native_session_id=native_sid,
                )
                session_id = (await self._client.create_session(self._cwd)).session_id
        else:
            session_id = (await self._client.create_session(self._cwd)).session_id

        # Apply model selection if specified
        if self._model:
            try:
                await self._client.set_model(session_id, self._model)
            except ProviderError:
                log.warning(
                    "cursor_acp.model_set_failed",
                    session_id=session_id,
                    model=self._model,
                )

        handle = self._build_handle(session_id, resume_handle)

        # Build full prompt
        full_prompt = self._compose_prompt(prompt, system_prompt, tools)

        # Yield initial system message
        yield AgentMessage(
            type="system",
            content=f"ACP session {session_id} started",
            resume_handle=handle,
            data={"session_id": session_id, "subtype": "init"},
        )

        # Stream ACP updates
        text_chunks: list[str] = []
        try:
            async for update in self._client.prompt_stream(
                session_id, full_prompt, timeout=600,
                permission_mode=self._permission_mode,
            ):
                msg = self._convert_update(update, handle)
                if msg:
                    yield msg
                    if msg.type == "assistant":
                        text_chunks.append(msg.content)

            # Final result
            final_text = "".join(text_chunks).strip() or "Task completed"
            yield AgentMessage(
                type="result",
                content=final_text,
                resume_handle=handle,
                data={
                    "subtype": "success",
                    "session_id": session_id,
                },
            )
        except Exception as exc:
            yield AgentMessage(
                type="result",
                content=str(exc),
                resume_handle=handle,
                data={
                    "subtype": "error",
                    "session_id": session_id,
                    "error": str(exc),
                },
            )

    async def execute_task_to_result(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> Result[TaskResult, ProviderError]:
        """Execute a task and collect the final result."""
        messages: list[AgentMessage] = []
        final_handle: RuntimeHandle | None = None

        async for msg in self.execute_task(
            prompt=prompt,
            tools=tools,
            system_prompt=system_prompt,
            resume_handle=resume_handle,
            resume_session_id=resume_session_id,
        ):
            messages.append(msg)
            if msg.resume_handle:
                final_handle = msg.resume_handle

        if not messages:
            return Result.err(
                ProviderError("No messages from ACP", provider="cursor_acp")
            )

        final = messages[-1]
        return Result.ok(
            TaskResult(
                success=not final.is_error,
                final_message=final.content,
                messages=tuple(messages),
                session_id=final.data.get("session_id"),
                resume_handle=final_handle,
            )
        )

    # ── Helpers ───────────────────────────────────────────────────────

    async def _resume_session(self, session_id: str) -> str:
        """Resume an existing ACP session."""
        result = await self._client.request("session/load", {
            "sessionId": session_id,
            "cwd": self._cwd,
            "mcpServers": [],
        })
        return result.get("sessionId", session_id)

    def _build_handle(
        self,
        session_id: str,
        previous: RuntimeHandle | None = None,
    ) -> RuntimeHandle:
        if previous:
            return replace(
                previous,
                backend="cursor_acp",
                native_session_id=session_id,
                cwd=self._cwd,
                approval_mode=self._permission_mode,
                updated_at=datetime.now(UTC).isoformat(),
            )
        return RuntimeHandle(
            backend="cursor_acp",
            kind="agent_runtime",
            native_session_id=session_id,
            cwd=self._cwd,
            approval_mode=self._permission_mode,
            updated_at=datetime.now(UTC).isoformat(),
        )

    @staticmethod
    def _compose_prompt(
        prompt: str,
        system_prompt: str | None,
        tools: list[str] | None,
    ) -> str:
        parts: list[str] = []
        if system_prompt:
            parts.append(system_prompt)
        if tools:
            parts.append(f"Available tools: {', '.join(tools)}")
        parts.append(prompt)
        return "\n\n".join(parts)

    @staticmethod
    def _convert_update(
        update: dict[str, Any],
        handle: RuntimeHandle,
    ) -> AgentMessage | None:
        """Convert an ACP session/update into an AgentMessage."""
        session_update = update.get("sessionUpdate", "")

        if session_update == "agent_message_chunk":
            content = update.get("content", {})
            if content.get("type") == "text" and content.get("text"):
                return AgentMessage(
                    type="assistant",
                    content=content["text"],
                    resume_handle=handle,
                )

        if session_update == "tool_call":
            return AgentMessage(
                type="tool",
                content=update.get("title", "Tool call"),
                tool_name=update.get("kind", "unknown"),
                resume_handle=handle,
                data={
                    "tool_call_id": update.get("toolCallId"),
                    "status": update.get("status"),
                },
            )

        if session_update == "tool_call_update":
            status = update.get("status", "")
            if status == "completed":
                contents = update.get("content", [])
                detail = ""
                for c in contents:
                    if c.get("type") == "diff":
                        detail = f"Modified {c.get('path', '?')}"
                    elif c.get("type") == "text":
                        detail = c.get("text", "")[:200]
                return AgentMessage(
                    type="tool",
                    content=detail or "Tool completed",
                    resume_handle=handle,
                    data={
                        "tool_call_id": update.get("toolCallId"),
                        "status": status,
                    },
                )

        return None
