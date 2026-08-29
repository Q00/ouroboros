"""GJC LLM adapter backed by the supported Coordinator MCP / SDK lifecycle."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from ouroboros.config import get_gjc_cli_path
from ouroboros.core.errors import ProviderError
from ouroboros.core.json_utils import extract_json_payload
from ouroboros.core.types import Result
from ouroboros.gjc.sdk_client import GjcCoordinatorClient, GjcCoordinatorError
from ouroboros.providers.base import (
    CompletionConfig,
    CompletionResponse,
    Message,
    MessageRole,
    UsageInfo,
)
from ouroboros.providers.codex_cli_adapter import CodexCliLLMAdapter
from ouroboros.providers.profiles import resolve_completion_profile_result
from ouroboros.providers.response_format import (
    build_response_format_directive,
    validate_response_format_payload,
)

CoordinatorClientFactory = Callable[..., GjcCoordinatorClient]


class GjcLLMAdapter(CodexCliLLMAdapter):
    """LLM adapter that runs one ephemeral GJC SDK session per completion."""

    _provider_name = "gjc"
    _display_name = "GJC SDK"
    _default_cli_name = "gjc"
    _log_namespace = "gjc_llm_adapter"
    _completion_profile_backend = "gjc"

    def __init__(
        self,
        *,
        cli_path: str | Path | None = None,
        cwd: str | Path | None = None,
        permission_mode: str | None = None,
        allowed_tools: list[str] | None = None,
        max_turns: int = 1,
        on_message: Any | None = None,
        max_retries: int = 3,
        ephemeral: bool = True,
        timeout: float | None = None,
        runtime_profile: str | None = None,
        coordinator_client_factory: CoordinatorClientFactory = GjcCoordinatorClient,
    ) -> None:
        del runtime_profile, ephemeral
        super().__init__(
            cli_path=cli_path,
            cwd=cwd,
            permission_mode=permission_mode,
            allowed_tools=allowed_tools,
            max_turns=max_turns,
            on_message=on_message,
            max_retries=max_retries,
            ephemeral=True,
            timeout=timeout,
            runtime_profile=None,
        )
        self._coordinator_client_factory = coordinator_client_factory

    def _get_configured_cli_path(self) -> str | None:
        return get_gjc_cli_path()

    def _resolve_permission_mode(self, permission_mode: str | None) -> str:
        return (permission_mode or "default").strip() or "default"

    def _build_permission_args(self) -> list[str]:
        return []

    def _build_response_format_directive(
        self,
        response_format: dict[str, object] | None,
    ) -> str | None:
        return build_response_format_directive(response_format)

    def _validate_response_format_payload(
        self,
        payload: str,
        response_format: dict[str, object],
    ) -> str | None:
        return validate_response_format_payload(payload, response_format)

    def _compose_prompt(self, messages: list[Message]) -> str:
        return "\n\n".join(f"{message.role.value}: {message.content}" for message in messages)

    async def _complete_once(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        prompt = self._compose_prompt(messages)
        timeout_seconds = self._effective_timeout_seconds()
        client = self._coordinator_client_factory(
            cli_path=self._cli_path,
            cwd=self._cwd,
            timeout=timeout_seconds,
        )
        session_id: str | None = None
        try:
            await client.connect()
            session = await client.start_session(
                prompt,
                model=config.model if config.model_is_explicit else None,
            )
            session_id = session.session_id
            turn = await client.await_turn(session.session_id, session.turn_id)
            if turn.status == "waiting_for_answer":
                return Result.err(
                    ProviderError(
                        message="GJC completion requires user input",
                        provider=self._provider_name,
                        details={
                            "session_id": session.session_id,
                            "turn_id": session.turn_id,
                            "question": turn.question.prompt if turn.question else None,
                        },
                    )
                )
            if not turn.succeeded:
                return Result.err(
                    ProviderError(
                        message=turn.error or f"GJC turn ended with status {turn.status}",
                        provider=self._provider_name,
                        details={
                            "session_id": session.session_id,
                            "turn_id": session.turn_id,
                            "status": turn.status,
                        },
                    )
                )
            content = turn.text or await client.read_last_assistant(session.session_id)
            if not content:
                return Result.err(
                    ProviderError(
                        message="Empty response from GJC SDK",
                        provider=self._provider_name,
                        details={"session_id": session.session_id, "turn_id": session.turn_id},
                    )
                )
            return Result.ok(
                CompletionResponse(
                    content=content,
                    model=config.model or "default",
                    usage=UsageInfo(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                    finish_reason="stop",
                    raw_response={
                        "transport": "gjc-coordinator-mcp",
                        "session_id": session.session_id,
                        "turn_id": session.turn_id,
                    },
                )
            )
        except GjcCoordinatorError as exc:
            return Result.err(
                ProviderError(
                    message=str(exc),
                    provider=self._provider_name,
                    details={"error_type": type(exc).__name__, "code": exc.code, **exc.details},
                )
            )
        finally:
            if session_id is not None:
                try:
                    await client.stop_session(session_id)
                except GjcCoordinatorError:
                    pass
            try:
                await client.close()
            except GjcCoordinatorError:
                pass

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Make a GJC SDK completion request with soft structured-output validation."""
        if not config.response_format:
            return await self._complete_once(messages, config)

        profile_result = resolve_completion_profile_result(
            config,
            backend=self._completion_profile_backend,
        )
        if profile_result.is_err:
            return Result.err(profile_result.error)
        effective_config = profile_result.value.config
        directive = self._build_response_format_directive(effective_config.response_format)
        if not directive:
            return Result.err(
                ProviderError(
                    message="Unsupported GJC structured response_format request",
                    provider=self._provider_name,
                    details={"response_format_type": effective_config.response_format.get("type")},
                )
            )

        patched_messages = [Message(role=MessageRole.SYSTEM, content=directive), *messages]
        patched_config = replace(effective_config, response_format=None)
        attempts = max(1, self._max_retries)
        last_response_preview = ""
        for _attempt in range(attempts):
            result = await self._complete_once(patched_messages, patched_config)
            if result.is_err:
                return result
            last_response_preview = result.value.content[:240]
            extracted = extract_json_payload(result.value.content)
            if not extracted:
                continue
            validation_error = self._validate_response_format_payload(
                extracted,
                effective_config.response_format,
            )
            if validation_error is None:
                return Result.ok(replace(result.value, content=extracted))

        return Result.err(
            ProviderError(
                message="JSON format required but GJC returned non-conforming output",
                provider=self._provider_name,
                details={"last_response_preview": last_response_preview},
            )
        )


__all__ = ["GjcLLMAdapter"]
