"""LLM adapter that delegates Claude completions to ``ourocode --acp``.

This is the SDK-free Claude completion backend. Instead of ``claude_agent_sdk``
or ``claude -p``, it speaks the Agent Client Protocol to ourocode, whose
``:claude_api`` model streams a Claude Pro/Max OAuth ``/v1/messages`` turn. See
:mod:`ouroboros.providers.ourocode_acp_client` for the wire protocol.

Scope: this implements the in-process **completion** path (interview / seed / qa
/ evaluate / wonder / reflect) — the single-turn LLM calls that today depend on
``claude_agent_sdk``. ourocode's ACP ``:claude_api`` is a plain streamed text
turn with no tool use, so it intentionally does not back the agentic
tool-using orchestrator runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from ouroboros.core.errors import ProviderError
from ouroboros.core.security import MAX_LLM_RESPONSE_LENGTH, InputValidator
from ouroboros.core.types import Result
from ouroboros.events.io_recorder import get_current_io_journal_recorder
from ouroboros.providers.base import (
    CompletionConfig,
    CompletionResponse,
    Message,
    MessageRole,
    UsageInfo,
)
from ouroboros.providers.ourocode_acp_client import AcpClientError, OurocodeAcpClient
from ouroboros.providers.profiles import resolve_completion_profile_result

if TYPE_CHECKING:
    from ouroboros.events.io_recorder import IOJournalRecorder

log = structlog.get_logger()

# ACP turns carry no token usage, so journal/usage counts are honestly zero
# (the same posture the claude_code SDK path takes).
_ZERO_USAGE = UsageInfo(prompt_tokens=0, completion_tokens=0, total_tokens=0)

_ERROR_STATUS: dict[str, int] = {
    "not_signed_in": 401,
    "cli_unavailable": 503,
    "timeout": 504,
}


class OurocodeLLMAdapter:
    """LLM completion adapter backed by ourocode's ACP Claude stream."""

    def __init__(
        self,
        *,
        cli_path: str | Path | None = None,
        cwd: str | Path | None = None,
        model: str = "claude",
        timeout: float | None = 600.0,
        startup_timeout: float = 30.0,
        io_recorder: IOJournalRecorder | None = None,
        **_ignored: Any,
    ) -> None:
        """Initialize the adapter.

        Args:
            cli_path: Path to the ``ourocode`` executable (PATH lookup if None).
            cwd: Working directory for the ACP session (resolved to absolute).
            model: ourocode backend selector (``claude`` → its OAuth Claude).
            timeout: Per-turn wall-clock timeout in seconds.
            startup_timeout: Timeout for the initialize/session-new handshake.
            io_recorder: Optional IO journal recorder (#517). ``None`` records
                nothing, matching every other adapter's default.
            _ignored: Extra factory kwargs (api_key, permission_mode, ...) that do
                not apply to the ACP backend are accepted and ignored so the
                shared ``create_llm_adapter`` call site stays uniform.
        """
        self._cli_path = cli_path
        self._cwd = cwd
        self._model = model
        # The shared factory passes ``timeout=None`` (its signature default).
        # Never let that disable the per-turn guard — a stalled ourocode turn
        # (OAuth refresh, network) would otherwise hang the completion forever
        # while holding a live subprocess. Coalesce to a bounded default.
        self._timeout = 600.0 if timeout is None else timeout
        self._startup_timeout = startup_timeout
        self._io_recorder = io_recorder

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Run one Claude turn through ourocode and return its text.

        ``reasoning_effort`` / ``response_format`` / sampling params have no ACP
        equivalent on ourocode's ``:claude_api`` turn and are ignored.
        """
        profile_result = resolve_completion_profile_result(config, backend="ourocode")
        if profile_result.is_err:
            return Result.err(profile_result.error)
        config = profile_result.value.config

        prompt_text = self._compose_prompt(messages)
        client = OurocodeAcpClient(
            cli_path=self._cli_path,
            cwd=self._cwd,
            model=self._model,
            startup_timeout=self._startup_timeout,
            turn_timeout=self._timeout,
        )

        recorder = get_current_io_journal_recorder() or self._io_recorder
        try:
            if recorder is not None and recorder.is_active:
                async with recorder.record_llm_call(
                    model_id=config.model,
                    prompt_text=prompt_text,
                    caller="ourocode_acp",
                    max_tokens=config.max_tokens,
                    temperature=config.temperature,
                    extra={"backend": "ourocode", "ourocode_model": self._model},
                ) as call:
                    parsed = await self._run(client, prompt_text, config)
                    call.record_completion(
                        completion_text=parsed.content,
                        finish_reason=parsed.finish_reason,
                        token_count_in=None,
                        token_count_out=None,
                    )
                return Result.ok(parsed)

            return Result.ok(await self._run(client, prompt_text, config))
        except AcpClientError as exc:
            return Result.err(self._provider_error(exc))

    async def _run(
        self,
        client: OurocodeAcpClient,
        prompt_text: str,
        config: CompletionConfig,
    ) -> CompletionResponse:
        result = await client.run_turn(prompt_text)
        content = result.text
        is_valid, _ = InputValidator.validate_llm_response(content)
        if not is_valid:
            log.warning(
                "ourocode.response.truncated",
                original_length=len(content),
                max_length=MAX_LLM_RESPONSE_LENGTH,
            )
            content = content[:MAX_LLM_RESPONSE_LENGTH]
        return CompletionResponse(
            content=content,
            model=config.model,
            usage=_ZERO_USAGE,
            finish_reason=self._finish_reason(result.stop_reason),
        )

    @staticmethod
    def _finish_reason(stop_reason: str) -> str:
        return {"end_turn": "stop", "cancelled": "cancelled"}.get(stop_reason, stop_reason)

    @staticmethod
    def _compose_prompt(messages: list[Message]) -> str:
        """Flatten system + conversation into one ACP prompt turn.

        ourocode's ``:claude_api`` injects its own required Claude-Code system
        block and tracks conversation per session, but the adapter spawns a
        fresh session per call, so the full context is composed into the single
        prompt text (mirroring the SDK completion path's prompt flattening).
        """
        system_parts = [m.content for m in messages if m.role == MessageRole.SYSTEM and m.content]
        turns = [m for m in messages if m.role != MessageRole.SYSTEM]

        sections: list[str] = []
        if system_parts:
            sections.append("## System Instructions\n" + "\n\n".join(system_parts))

        if len(turns) <= 1:
            # Single user message: send it directly under any system block.
            if turns:
                sections.append(turns[0].content)
        else:
            lines = []
            for msg in turns:
                speaker = "Assistant" if msg.role == MessageRole.ASSISTANT else "User"
                lines.append(f"{speaker}: {msg.content}")
            sections.append("## Conversation\n" + "\n\n".join(lines))

        composed = "\n\n".join(part for part in sections if part.strip())
        return composed or "(empty)"

    @staticmethod
    def _provider_error(exc: AcpClientError) -> ProviderError:
        return ProviderError(
            message=exc.message,
            provider="ourocode",
            status_code=_ERROR_STATUS.get(exc.error_type),
            details={"error_type": exc.error_type, **exc.details},
        )


__all__ = ["OurocodeLLMAdapter"]
