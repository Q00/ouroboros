"""Anthropic SDK adapter for direct Claude API access.

This module provides the AnthropicAdapter class that implements the LLMAdapter
protocol using the official Anthropic Python SDK. This is the recommended default
for Ouroboros MCP server — no OpenRouter or LiteLLM dependency required.
"""

import os
from typing import Any

import structlog

from ouroboros.core.errors import ProviderError
from ouroboros.core.security import MAX_LLM_RESPONSE_LENGTH, InputValidator
from ouroboros.core.types import Result
from ouroboros.providers.base import (
    CompletionConfig,
    CompletionResponse,
    Message,
    MessageRole,
    UsageInfo,
)

log = structlog.get_logger()

DEFAULT_MODEL = "claude-sonnet-4-20250514"


class AnthropicAdapter:
    """LLM adapter using the official Anthropic Python SDK.

    Calls the Claude API directly without LiteLLM or OpenRouter intermediaries.
    API key is resolved from constructor param or ANTHROPIC_API_KEY env var.

    Example:
        adapter = AnthropicAdapter()
        result = await adapter.complete(
            messages=[Message(role=MessageRole.USER, content="Hello!")],
            config=CompletionConfig(model="claude-sonnet-4-20250514"),
        )
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 120.0,
        max_retries: int = 2,
        default_model: str = DEFAULT_MODEL,
    ) -> None:
        """Initialize the Anthropic adapter.

        Args:
            api_key: Optional API key (overrides ANTHROPIC_API_KEY env var).
            timeout: Request timeout in seconds. Default 120.0.
            max_retries: Max retries for transient errors (handled by SDK). Default 2.
            default_model: Fallback model when config.model is empty or generic.
        """
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._timeout = timeout
        self._max_retries = max_retries
        self._default_model = default_model
        self._client: Any = None

    def _get_client(self) -> Any:
        """Lazy-initialize the Anthropic async client.

        Returns:
            An AsyncAnthropic client instance.

        Raises:
            ImportError: If the anthropic package is not installed.
        """
        if self._client is None:
            try:
                import anthropic
            except ImportError as e:
                msg = "anthropic package not installed. Install with: uv add anthropic"
                raise ImportError(msg) from e

            self._client = anthropic.AsyncAnthropic(
                api_key=self._api_key,
                timeout=self._timeout,
                max_retries=self._max_retries,
            )
        return self._client

    def _resolve_model(self, model: str) -> str:
        """Resolve the model identifier for the Anthropic API.

        Strips provider prefixes (e.g. 'anthropic/claude-...') and falls back
        to the default model for non-Claude model strings.

        Args:
            model: Raw model identifier from CompletionConfig.

        Returns:
            A clean model identifier suitable for the Anthropic API.
        """
        # Strip common prefixes
        if model.startswith("anthropic/"):
            model = model[len("anthropic/") :]
        if model.startswith("openrouter/anthropic/"):
            model = model[len("openrouter/anthropic/") :]

        # If it looks like a Claude model, use it directly
        if model.startswith("claude"):
            return model

        # For non-Claude models, fall back to default
        return self._default_model

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Make a completion request to the Anthropic API.

        Handles system messages separately (Anthropic API requires them as
        a top-level parameter, not in the messages array).

        Args:
            messages: The conversation messages to send.
            config: Configuration for the completion request.

        Returns:
            Result containing either the completion response or a ProviderError.
        """
        try:
            client = self._get_client()
        except ImportError as e:
            return Result.err(ProviderError(str(e), provider="anthropic"))

        if not self._api_key:
            return Result.err(
                ProviderError(
                    "ANTHROPIC_API_KEY not set. Export it or pass api_key= to AnthropicAdapter.",
                    provider="anthropic",
                    status_code=401,
                )
            )

        model = self._resolve_model(config.model)

        # Separate system messages from conversation messages.
        # Anthropic API takes system as a top-level param.
        system_parts: list[str] = []
        api_messages: list[dict[str, str]] = []

        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                system_parts.append(msg.content)
            else:
                api_messages.append(msg.to_dict())

        # Ensure at least one user message exists
        if not api_messages:
            api_messages.append({"role": "user", "content": "(empty)"})

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": api_messages,
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
            "top_p": config.top_p,
        }

        if system_parts:
            kwargs["system"] = "\n\n".join(system_parts)

        if config.stop:
            kwargs["stop_sequences"] = config.stop

        log.debug(
            "anthropic.request.started",
            model=model,
            message_count=len(api_messages),
            has_system=bool(system_parts),
        )

        try:
            response = await client.messages.create(**kwargs)
            return Result.ok(self._parse_response(response, model))

        except Exception as e:
            return self._handle_error(e, model)

    def _parse_response(
        self,
        response: Any,
        model: str,
    ) -> CompletionResponse:
        """Parse the Anthropic API response into CompletionResponse.

        Args:
            response: The raw Anthropic Message response.
            model: The model identifier used for the request.

        Returns:
            Parsed CompletionResponse.
        """
        # Extract text content from content blocks
        content = ""
        for block in response.content:
            if block.type == "text":
                content += block.text

        # Security: Validate response length
        is_valid, _ = InputValidator.validate_llm_response(content)
        if not is_valid:
            log.warning(
                "anthropic.response.truncated",
                model=model,
                original_length=len(content),
                max_length=MAX_LLM_RESPONSE_LENGTH,
            )
            content = content[:MAX_LLM_RESPONSE_LENGTH]

        usage = response.usage

        return CompletionResponse(
            content=content,
            model=response.model or model,
            usage=UsageInfo(
                prompt_tokens=usage.input_tokens if usage else 0,
                completion_tokens=usage.output_tokens if usage else 0,
                total_tokens=((usage.input_tokens + usage.output_tokens) if usage else 0),
            ),
            finish_reason=response.stop_reason or "end_turn",
        )

    def _handle_error(
        self,
        exc: Exception,
        model: str,
    ) -> Result[CompletionResponse, ProviderError]:
        """Convert Anthropic SDK exceptions to Result.err(ProviderError).

        Args:
            exc: The caught exception.
            model: The model identifier for logging context.

        Returns:
            Result.err with an appropriate ProviderError.
        """
        try:
            import anthropic
        except ImportError:
            return Result.err(ProviderError(f"Unexpected error: {exc}", provider="anthropic"))

        if isinstance(exc, anthropic.AuthenticationError):
            log.warning("anthropic.request.failed.auth", model=model, error=str(exc))
            return Result.err(
                ProviderError(
                    "Authentication failed — check ANTHROPIC_API_KEY",
                    provider="anthropic",
                    status_code=401,
                )
            )

        if isinstance(exc, anthropic.RateLimitError):
            log.warning("anthropic.request.failed.rate_limit", model=model)
            return Result.err(
                ProviderError(
                    "Rate limit exceeded",
                    provider="anthropic",
                    status_code=429,
                )
            )

        if isinstance(exc, anthropic.BadRequestError):
            log.warning("anthropic.request.failed.bad_request", model=model, error=str(exc))
            return Result.err(
                ProviderError(
                    f"Bad request: {exc}",
                    provider="anthropic",
                    status_code=400,
                )
            )

        if isinstance(exc, anthropic.APIError):
            log.warning("anthropic.request.failed.api_error", model=model, error=str(exc))
            return Result.err(
                ProviderError(
                    f"API error: {exc}",
                    provider="anthropic",
                    status_code=getattr(exc, "status_code", 500),
                )
            )

        # Unexpected
        log.exception("anthropic.request.failed.unexpected", model=model, error=str(exc))
        return Result.err(
            ProviderError(
                f"Unexpected error: {exc}",
                provider="anthropic",
                details={"original_exception": type(exc).__name__},
            )
        )
