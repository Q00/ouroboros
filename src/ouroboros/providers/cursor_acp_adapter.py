"""Cursor ACP adapter for LLM completions.

Uses the shared :class:`CursorACPClient` to send prompts via a
persistent ``cursor-agent acp`` session.  Dramatically faster than
the one-shot ``cursor-agent -p`` adapter for multi-call workflows.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import structlog

from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.providers.base import (
    CompletionConfig,
    CompletionResponse,
    Message,
    MessageRole,
    UsageInfo,
)
from ouroboros.providers.cursor_acp_client import CursorACPClient, get_shared_acp_client

log = structlog.get_logger(__name__)


class CursorACPAdapter:
    """LLM adapter using a persistent cursor-agent ACP session.

    Reuses a shared :class:`CursorACPClient` process across calls.
    Each ``complete()`` sends ``session/prompt`` and collects
    ``agent_message_chunk`` updates into a single response.
    """

    def __init__(
        self,
        cli_path: str | None = None,
        cwd: str | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> None:
        self._cwd = str(cwd) if cwd else str(Path.cwd())
        self._client: CursorACPClient = get_shared_acp_client(cli_path)
        self._session_id: str | None = None
        self._pending_model: str | None = model
        self._lock = asyncio.Lock()

    async def _ensure_session(self) -> str:
        await self._client.ensure_started()
        if not self._session_id:
            session = await self._client.create_session(self._cwd)
            self._session_id = session.session_id
            self._available_models = session.available_models
            # Apply model if set during init
            if self._pending_model:
                try:
                    await self._client.set_model(self._session_id, self._pending_model)
                except ProviderError:
                    pass
        return self._session_id

    async def get_available_models(self) -> list:
        """Return available ACP models as ACPModel instances."""
        await self._ensure_session()
        return list(getattr(self, "_available_models", ()))

    async def set_model(self, model_id: str) -> None:
        """Set the model for the current ACP session."""
        session_id = await self._ensure_session()
        await self._client.set_model(session_id, model_id)

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        async with self._lock:
            try:
                session_id = await self._ensure_session()
                prompt_text = self._build_prompt(messages)

                text_chunks: list[str] = []
                async for update in self._client.prompt_stream(
                    session_id, prompt_text, timeout=120,
                ):
                    if update.get("sessionUpdate") == "agent_message_chunk":
                        content = update.get("content", {})
                        if content.get("type") == "text":
                            text_chunks.append(content["text"])

                result_text = "".join(text_chunks).strip()
                if not result_text:
                    return Result.err(
                        ProviderError("Empty ACP response", provider="cursor_acp")
                    )

                return Result.ok(
                    CompletionResponse(
                        content=result_text,
                        model=config.model or "cursor-acp",
                        usage=UsageInfo(
                            prompt_tokens=0,
                            completion_tokens=0,
                            total_tokens=0,
                        ),
                    )
                )
            except ProviderError as e:
                return Result.err(e)
            except Exception as e:
                self._session_id = None
                return Result.err(
                    ProviderError(str(e), provider="cursor_acp")
                )

    @staticmethod
    def _build_prompt(messages: list[Message]) -> str:
        parts: list[str] = []
        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                parts.append(msg.content)
            elif msg.role == MessageRole.USER:
                parts.append(msg.content)
            elif msg.role == MessageRole.ASSISTANT:
                parts.append(f"[Previous response]: {msg.content}")
        return "\n\n".join(parts)

    async def close(self) -> None:
        await self._client.close()
