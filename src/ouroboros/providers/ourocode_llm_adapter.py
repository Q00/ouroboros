"""LLM adapter that delegates Claude completions to ``ourocode --acp``.

This is the SDK-free Claude completion backend by default. Instead of
``claude_agent_sdk`` or ``claude -p``, it speaks the Agent Client Protocol to
ourocode, whose ``:claude_api`` model streams a Claude Pro/Max OAuth
``/v1/messages`` turn. Advanced callers may select another ourocode ACP backend
selector (``codex`` / ``gemini``), but raw model IDs are rejected because
``OUROCODE_MODEL`` maps backend selectors, not provider slugs. See
:mod:`ouroboros.providers.ourocode_acp_client` for the wire protocol.

Scope: this implements the in-process **completion** path (interview / seed / qa
/ evaluate / wonder / reflect) — the single-turn LLM calls that today depend on
``claude_agent_sdk``. The ACP turn is a plain streamed text turn with no tool
use, so it intentionally does not back the agentic tool-using orchestrator
runtime.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
import structlog

from ouroboros.config._model_defaults import (
    DEFAULT_OPUS_MODEL,
    DEFAULT_SONNET_MODEL,
    recognized_shipped_defaults,
)
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

_STRUCTURED_RESPONSE_ATTEMPTS = 3
_OUROCODE_MODEL_SELECTORS = frozenset({"claude", "claude_api", "codex", "gemini"})
_SHIPPED_CLAUDE_MODELS = frozenset(
    (
        *recognized_shipped_defaults(DEFAULT_OPUS_MODEL),
        *recognized_shipped_defaults(DEFAULT_SONNET_MODEL),
    )
)


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

        ``reasoning_effort`` and sampling params have no ACP equivalent on
        ourocode's ``:claude_api`` turn and are ignored. Structured
        ``response_format`` requests are cooperatively enforced through prompt
        instructions plus adapter-side JSON extraction and validation.
        """
        profile_result = resolve_completion_profile_result(config, backend="ourocode")
        if profile_result.is_err:
            return Result.err(profile_result.error)
        config = profile_result.value.config

        model_result = self._resolve_ourocode_model(config.model)
        if model_result.is_err:
            return Result.err(model_result.error)
        ourocode_model = model_result.value

        response_format = config.response_format
        if response_format:
            directive = self._build_response_format_directive(response_format)
            if not directive:
                return Result.err(
                    ProviderError(
                        message="Unsupported ourocode structured response_format request",
                        provider="ourocode",
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
        client = OurocodeAcpClient(
            cli_path=self._cli_path,
            cwd=self._cwd,
            model=ourocode_model,
            startup_timeout=self._startup_timeout,
            turn_timeout=self._timeout,
        )
        response_model = ourocode_model

        recorder = get_current_io_journal_recorder() or self._io_recorder
        try:
            if recorder is not None and recorder.is_active:
                async with recorder.record_llm_call(
                    model_id=response_model,
                    prompt_text=prompt_text,
                    caller="ourocode_acp",
                    max_tokens=config.max_tokens,
                    temperature=config.temperature,
                    extra={"backend": "ourocode", "ourocode_model": ourocode_model},
                ) as call:
                    result = await self._run_result(
                        client, prompt_text, config, response_model=response_model
                    )
                    if result.is_ok:
                        parsed = result.value
                        call.record_completion(
                            completion_text=parsed.content,
                            finish_reason=parsed.finish_reason,
                            token_count_in=None,
                            token_count_out=None,
                        )
                return result

            return await self._run_result(
                client, prompt_text, config, response_model=response_model
            )
        except AcpClientError as exc:
            return Result.err(self._provider_error(exc))

    async def _run_result(
        self,
        client: OurocodeAcpClient,
        prompt_text: str,
        config: CompletionConfig,
        *,
        response_model: str,
    ) -> Result[CompletionResponse, ProviderError]:
        if not config.response_format:
            return Result.ok(
                await self._run(client, prompt_text, config, response_model=response_model)
            )

        last_response_preview = ""
        for _attempt in range(_STRUCTURED_RESPONSE_ATTEMPTS):
            parsed = await self._run(client, prompt_text, config, response_model=response_model)
            last_response_preview = parsed.content[:240]
            extracted = extract_json_payload(parsed.content)
            if not extracted:
                continue
            validation_error = self._validate_response_format_payload(
                extracted,
                config.response_format,
            )
            if validation_error is None:
                return Result.ok(replace(parsed, content=extracted))

        return Result.err(
            ProviderError(
                message="JSON format required but ourocode returned non-conforming output",
                provider="ourocode",
                details={"last_response_preview": last_response_preview},
            )
        )

    async def _run(
        self,
        client: OurocodeAcpClient,
        prompt_text: str,
        config: CompletionConfig,
        *,
        response_model: str,
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
            model=response_model,
            usage=_ZERO_USAGE,
            finish_reason=self._finish_reason(result.stop_reason),
            raw_response={"ourocode_model": response_model},
        )

    def _resolve_ourocode_model(
        self,
        configured_model: str,
    ) -> Result[str, ProviderError]:
        """Resolve a CompletionConfig model into an ``OUROCODE_MODEL`` selector.

        ourocode's ACP server currently maps known backend selectors only
        (``claude``/``claude_api``/``codex``/``gemini``). Shipped Anthropic model
        pins and the ``default`` sentinel mean "use ourocode's Claude OAuth
        backend" here; arbitrary raw model ids would make audit metadata claim a
        selector the child process cannot actually honor, so reject them.
        """
        candidate = configured_model.strip()
        if not candidate or candidate == "default" or candidate in _SHIPPED_CLAUDE_MODELS:
            return self._normalize_ourocode_model_selector(
                self._model,
                details_key="adapter_model",
            )

        return self._normalize_ourocode_model_selector(
            configured_model,
            details_key="model",
        )

    @staticmethod
    def _normalize_ourocode_model_selector(
        model: str,
        *,
        details_key: str,
    ) -> Result[str, ProviderError]:
        """Normalize a public model value into an ``OUROCODE_MODEL`` selector."""
        candidate = model.strip()
        if not candidate or candidate == "default" or candidate in _SHIPPED_CLAUDE_MODELS:
            return Result.ok("claude")
        normalized = candidate.lower().replace("-", "_")
        if normalized in _OUROCODE_MODEL_SELECTORS:
            return Result.ok(normalized)

        return Result.err(
            ProviderError(
                message=(
                    "Unsupported ourocode model selector. Use one of: "
                    "claude, claude_api, codex, gemini"
                ),
                provider="ourocode",
                details={
                    details_key: model,
                    "supported_selectors": sorted(_OUROCODE_MODEL_SELECTORS),
                },
            )
        )

    @staticmethod
    def _build_response_format_directive(
        response_format: dict[str, object] | None,
    ) -> str | None:
        """Translate response_format into cooperative ourocode prompt instructions."""
        if not response_format:
            return None
        fmt_type = response_format.get("type")
        if fmt_type == "json_object":
            return (
                "Respond with ONLY a valid JSON object. Do not use markdown fences, "
                "headers, or explanatory text."
            )
        if fmt_type == "json_schema":
            schema = response_format.get("json_schema")
            if not isinstance(schema, dict):
                return None
            schema_payload = (
                schema.get("schema") if isinstance(schema.get("schema"), dict) else schema
            )
            top_type = (
                schema_payload.get("type", "object")
                if isinstance(schema_payload, dict)
                else "object"
            )
            type_noun = {"array": "JSON array", "object": "JSON object"}.get(
                str(top_type), "JSON value"
            )
            try:
                rendered = json.dumps(schema_payload, indent=2, sort_keys=True)
            except (TypeError, ValueError):
                rendered = str(schema_payload)
            return (
                f"Respond with ONLY a valid {type_noun} that matches this schema. "
                "Do not use markdown fences, headers, or explanatory text.\n\n"
                f"JSON schema:\n{rendered}"
            )
        return None

    @staticmethod
    def _validate_response_format_payload(
        payload: str,
        response_format: dict[str, object],
    ) -> str | None:
        """Validate extracted JSON against the requested response_format."""
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            return f"invalid JSON: {exc}"

        fmt_type = response_format.get("type")
        if fmt_type == "json_object":
            return None if isinstance(parsed, dict) else "expected a JSON object"
        if fmt_type == "json_schema":
            schema = response_format.get("json_schema")
            if not isinstance(schema, dict):
                return "json_schema response_format is missing a schema object"
            schema_payload = (
                schema.get("schema") if isinstance(schema.get("schema"), dict) else schema
            )
            try:
                Draft202012Validator(schema_payload).validate(parsed)
            except JsonSchemaValidationError as exc:
                return exc.message
        return None

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
