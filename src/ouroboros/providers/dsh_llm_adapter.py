"""LLM adapter that delegates completions to DeepSeek Harness over ACP.

DeepSeek Harness (``dsh``) is DeepSeek AI's open-source agent harness. Its
published ``dsh-acp-demo`` bin exposes an automation-only Agent Client
Protocol server, and this adapter drives it exactly like the ourocode ACP
backend: one spawned process per completion, one streamed text turn, no tool
use. See :mod:`ouroboros.providers.dsh_acp_client` for the launch contract.

Scope: in-process **completion** path only (interview / seed / qa / evaluate /
wonder / reflect). dsh's ACP server is deliberately narrow — text-only prompts,
committed assistant text, fresh sessions — so it does not back the agentic
tool-using orchestrator runtime.

Model selection: the ACP wire has no model parameter. Provider and model are
fixed by the dsh Cordis composition file (``--config``), so ``config.model`` is
recorded as advisory metadata only and the response reports the composition
sentinel — the adapter must not claim a model id the child process never
honored.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from ouroboros.core.errors import ProviderError
from ouroboros.core.json_utils import extract_json_payload
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
from ouroboros.providers.dsh_acp_client import AcpClientError, DshAcpClient
from ouroboros.providers.profiles import resolve_completion_profile_result
from ouroboros.providers.response_format import (
    build_response_format_directive,
    validate_response_format_payload,
)

if TYPE_CHECKING:
    from ouroboros.events.io_recorder import IOJournalRecorder

log = structlog.get_logger()

# ACP turns carry no token usage, so journal/usage counts are honestly zero
# (same posture as the ourocode and claude_code SDK paths).
_ZERO_USAGE = UsageInfo(prompt_tokens=0, completion_tokens=0, total_tokens=0)

_ERROR_STATUS: dict[str, int] = {
    "cli_unavailable": 503,
    "timeout": 504,
}

_STRUCTURED_RESPONSE_ATTEMPTS = 3

# The model id reported on responses. The dsh composition file owns the real
# provider/model choice and the ACP wire never echoes it back, so anything more
# specific here would be fabricated audit metadata.
_COMPOSITION_MODEL = "dsh-composition"


class DshLLMAdapter:
    """LLM completion adapter backed by DeepSeek Harness's ACP server."""

    def __init__(
        self,
        *,
        cli_path: str | Path | None = None,
        cwd: str | Path | None = None,
        config_path: str | Path | None = None,
        timeout: float | None = 600.0,
        startup_timeout: float = 30.0,
        io_recorder: IOJournalRecorder | None = None,
        **_ignored: Any,
    ) -> None:
        """Initialize the adapter.

        Args:
            cli_path: Path to ``dsh-acp-demo`` (PATH lookup if None).
            cwd: Working directory for the ACP session (resolved to absolute).
            config_path: absolute trusted dsh Cordis composition file forwarded
                as ``--config``; ``None`` or a relative path fails closed.
            timeout: Per-turn wall-clock timeout in seconds.
            startup_timeout: Timeout for the initialize/session-new handshake.
            io_recorder: Optional IO journal recorder (#517). ``None`` records
                nothing, matching every other adapter's default.
            _ignored: Extra factory kwargs (api_key, permission_mode, ...) that
                do not apply to the ACP backend are accepted and ignored so the
                shared ``create_llm_adapter`` call site stays uniform.
        """
        self._cli_path = cli_path
        self._cwd = cwd
        self._config_path = config_path
        # The shared factory passes ``timeout=None`` (its signature default).
        # Never let that disable the per-turn guard — a stalled dsh turn would
        # otherwise hang the completion forever while holding a live
        # subprocess. Coalesce to a bounded default.
        self._timeout = 600.0 if timeout is None else timeout
        self._startup_timeout = startup_timeout
        self._io_recorder = io_recorder

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Run one completion turn through dsh and return its text.

        ``reasoning_effort`` and sampling params have no ACP equivalent (the
        dsh composition file owns them) and are ignored. Structured
        ``response_format`` requests are cooperatively enforced through prompt
        instructions plus adapter-side JSON extraction and validation.
        """
        profile_result = resolve_completion_profile_result(config, backend="dsh")
        if profile_result.is_err:
            return Result.err(profile_result.error)
        config = profile_result.value.config

        response_format = config.response_format
        if response_format:
            directive = build_response_format_directive(response_format)
            if not directive:
                return Result.err(
                    ProviderError(
                        message="Unsupported dsh structured response_format request",
                        provider="dsh",
                        details={
                            "response_format_type": response_format.get("type"),
                        },
                    )
                )
            prompt_text = self._compose_prompt(
                [Message(role=MessageRole.SYSTEM, content=directive), *messages]
            )
        else:
            prompt_text = self._compose_prompt(messages)

        client = DshAcpClient(
            cli_path=self._cli_path,
            cwd=self._cwd,
            config_path=self._config_path,
            startup_timeout=self._startup_timeout,
            turn_timeout=self._timeout,
        )

        recorder = get_current_io_journal_recorder() or self._io_recorder
        try:
            if recorder is not None and recorder.is_active:
                async with recorder.record_llm_call(
                    model_id=_COMPOSITION_MODEL,
                    prompt_text=prompt_text,
                    caller="dsh_acp",
                    max_tokens=config.max_tokens,
                    temperature=config.temperature,
                    extra={"backend": "dsh", "configured_model": config.model},
                ) as call:
                    result = await self._run_result(client, prompt_text, config)
                    if result.is_ok:
                        parsed = result.value
                        call.record_completion(
                            completion_text=parsed.content,
                            finish_reason=parsed.finish_reason,
                            token_count_in=None,
                            token_count_out=None,
                        )
                return result

            return await self._run_result(client, prompt_text, config)
        except AcpClientError as exc:
            return Result.err(self._provider_error(exc))

    async def _run_result(
        self,
        client: DshAcpClient,
        prompt_text: str,
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        if not config.response_format:
            parsed = await self._run(client, prompt_text)
            if not parsed.content.strip():
                # A turn that produced no text is a failed turn, whatever it
                # stopped for. Returning it as `ok` hands the caller an empty
                # string where an answer belongs; every sibling adapter fails
                # closed here instead.
                return Result.err(
                    ProviderError(
                        message="Empty response from dsh",
                        provider="dsh",
                        details={"stop_reason": parsed.raw_response["stop_reason"]},
                    )
                )
            return Result.ok(parsed)

        last_response_preview = ""
        for _attempt in range(_STRUCTURED_RESPONSE_ATTEMPTS):
            parsed = await self._run(client, prompt_text)
            last_response_preview = parsed.content[:240]
            extracted = extract_json_payload(parsed.content)
            if not extracted:
                continue
            validation_error = validate_response_format_payload(
                extracted,
                config.response_format,
            )
            if validation_error is None:
                return Result.ok(replace(parsed, content=extracted))

        return Result.err(
            ProviderError(
                message="JSON format required but dsh returned non-conforming output",
                provider="dsh",
                details={"last_response_preview": last_response_preview},
            )
        )

    async def _run(
        self,
        client: DshAcpClient,
        prompt_text: str,
    ) -> CompletionResponse:
        result = await client.run_turn(prompt_text)
        content = result.text
        is_valid, _ = InputValidator.validate_llm_response(content)
        if not is_valid:
            log.warning(
                "dsh.response.truncated",
                original_length=len(content),
                max_length=MAX_LLM_RESPONSE_LENGTH,
            )
            content = content[:MAX_LLM_RESPONSE_LENGTH]
        return CompletionResponse(
            content=content,
            model=_COMPOSITION_MODEL,
            usage=_ZERO_USAGE,
            finish_reason=self._finish_reason(result.stop_reason),
            raw_response={
                "backend": "dsh",
                "stop_reason": result.stop_reason,
            },
        )

    @staticmethod
    def _finish_reason(stop_reason: str) -> str:
        """Translate an ACP ``stopReason`` into this codebase's ``finish_reason``.

        ``max_tokens`` has to become ``"length"``: that is the marker callers
        test for, and an untranslated one reads as an unremarkable stop rather
        than a truncation. Unknown reasons pass through unchanged so a new ACP
        value is visible rather than silently recoded as a clean stop.
        """
        return {
            "end_turn": "stop",
            "cancelled": "cancelled",
            "max_tokens": "length",
            "max_turn_requests": "length",
        }.get(stop_reason, stop_reason)

    @staticmethod
    def _compose_prompt(messages: list[Message]) -> str:
        """Flatten system + conversation into one ACP prompt turn.

        dsh tracks conversation per session, but the adapter spawns a fresh
        session per call, so the full context is composed into the single
        prompt text (mirroring the ourocode and SDK completion paths).
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
            provider="dsh",
            status_code=_ERROR_STATUS.get(exc.error_type),
            details={"error_type": exc.error_type, **exc.details},
        )


__all__ = ["DshLLMAdapter"]
