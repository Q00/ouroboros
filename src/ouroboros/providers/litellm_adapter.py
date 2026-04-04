"""LiteLLM adapter for unified LLM provider access.

This module provides the LiteLLMAdapter class that implements the LLMAdapter
protocol using LiteLLM for multi-provider support including OpenRouter.
"""

import os
from typing import Any

import litellm
import structlog

from ouroboros.core.errors import ProviderError
from ouroboros.core.retry import retry_async
from ouroboros.core.security import MAX_LLM_RESPONSE_LENGTH, InputValidator
from ouroboros.core.types import Result
from ouroboros.providers.base import (
    CompletionConfig,
    CompletionResponse,
    Message,
    UsageInfo,
)

log = structlog.get_logger()
_CREDENTIALS_UNSET = object()
_PLACEHOLDER_API_KEY_PREFIX = "YOUR_"
_PLACEHOLDER_API_KEY_SUFFIX = "_API_KEY"

# LiteLLM exceptions that should trigger retries
RETRIABLE_EXCEPTIONS = (
    litellm.RateLimitError,
    litellm.ServiceUnavailableError,
    litellm.Timeout,
    litellm.APIConnectionError,
)


class LiteLLMAdapter:
    """LLM adapter using LiteLLM for unified provider access.

    This adapter supports multiple LLM providers through LiteLLM's unified
    interface, including OpenRouter for model routing.

    API keys are loaded from environment variables with the following priority:
    1. Environment variables: OPENROUTER_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY
    2. Explicit api_key parameter (overrides environment)

    Example:
        # Using environment variables (recommended)
        adapter = LiteLLMAdapter()

        # Or with explicit API key
        adapter = LiteLLMAdapter(api_key="sk-...")

        result = await adapter.complete(
            messages=[Message(role=MessageRole.USER, content="Hello!")],
            config=CompletionConfig(model="openrouter/openai/gpt-4"),
        )
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_base: str | None = None,
        timeout: float = 60.0,
        max_retries: int = 3,
    ) -> None:
        """Initialize the LiteLLM adapter.

        Args:
            api_key: Optional API key (overrides environment variables).
            api_base: Optional API base URL for custom endpoints.
            timeout: Request timeout in seconds. Default 60.0.
            max_retries: Maximum number of retries for transient errors. Default 3.
        """
        self._api_key = api_key
        self._api_base = api_base
        self._timeout = timeout
        self._max_retries = max_retries
        self._credentials_cache: object = _CREDENTIALS_UNSET

    def _load_credentials_config(self):
        """Load credentials.yaml once, caching missing-config cases."""
        if self._credentials_cache is not _CREDENTIALS_UNSET:
            return self._credentials_cache

        try:
            from ouroboros.config import load_credentials
            from ouroboros.core.errors import ConfigError

            self._credentials_cache = load_credentials()
        except ConfigError:
            self._credentials_cache = None
        return self._credentials_cache

    def _get_configured_provider_credentials(self, model: str):
        """Load provider credentials for a model from credentials.yaml."""
        credentials = self._load_credentials_config()
        if credentials is None:
            return None

        provider_name = self._extract_provider(model)
        return credentials.providers.get(provider_name)

    @staticmethod
    def _normalize_api_key(value: str | None) -> str | None:
        """Treat blank and template placeholder API keys as unset."""
        if value is None:
            return None

        candidate = value.strip()
        if not candidate:
            return None
        if candidate.startswith(_PLACEHOLDER_API_KEY_PREFIX) and candidate.endswith(
            _PLACEHOLDER_API_KEY_SUFFIX
        ):
            return None
        return candidate

    def _get_api_key(self, model: str) -> str | None:
        """Get the appropriate API key for the model.

        Priority:
        1. Explicit api_key from constructor
        2. Environment variables based on model prefix
        3. credentials.yaml provider entry

        Args:
            model: The model identifier.

        Returns:
            The API key or None if not found.
        """
        explicit_api_key = self._normalize_api_key(self._api_key)
        if explicit_api_key:
            return explicit_api_key

        # Check environment variables based on model prefix
        if model.startswith("openrouter/"):
            env_key = self._normalize_api_key(os.environ.get("OPENROUTER_API_KEY"))
            if env_key:
                return env_key
        if model.startswith("anthropic/") or model.startswith("claude"):
            env_key = self._normalize_api_key(os.environ.get("ANTHROPIC_API_KEY"))
            if env_key:
                return env_key
        if model.startswith("openai/") or model.startswith("gpt"):
            env_key = self._normalize_api_key(os.environ.get("OPENAI_API_KEY"))
            if env_key:
                return env_key
        if model.startswith("google/") or model.startswith("gemini"):
            env_key = self._normalize_api_key(os.environ.get("GOOGLE_API_KEY"))
            if env_key:
                return env_key

        configured = self._get_configured_provider_credentials(model)
        if configured is not None:
            configured_api_key = self._normalize_api_key(configured.api_key)
            if configured_api_key:
                return configured_api_key

        # Default to OpenRouter for unknown models
        return self._normalize_api_key(os.environ.get("OPENROUTER_API_KEY"))

    def _get_api_base(self, model: str) -> str | None:
        """Get the appropriate API base URL for the model."""
        if self._api_base:
            return self._api_base

        configured = self._get_configured_provider_credentials(model)
        if configured is not None:
            return configured.base_url

        return None

    def _build_completion_kwargs(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> dict[str, Any]:
        """Build the kwargs for litellm.acompletion.

        Args:
            messages: The conversation messages.
            config: The completion configuration.

        Returns:
            Dictionary of kwargs for litellm.acompletion.
        """
        kwargs: dict[str, Any] = {
            "model": config.model,
            "messages": [m.to_dict() for m in messages],
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "timeout": self._timeout,
        }

        # Anthropic models don't accept both temperature and top_p together
        # Other providers (OpenAI, OpenRouter) support both
        model_lower = config.model.lower()
        if not ("anthropic" in model_lower or "claude" in model_lower):
            kwargs["top_p"] = config.top_p

        if config.stop:
            kwargs["stop"] = config.stop

        if config.response_format:
            kwargs["response_format"] = config.response_format

        api_key = self._get_api_key(config.model)
        if api_key:
            kwargs["api_key"] = api_key

        api_base = self._get_api_base(config.model)
        if api_base:
            kwargs["api_base"] = api_base

        return kwargs

    async def _raw_complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> litellm.ModelResponse:
        """Make the raw completion call.

        Args:
            messages: The conversation messages.
            config: The completion configuration.

        Returns:
            The raw LiteLLM response.

        Raises:
            litellm exceptions for API errors.
        """
        kwargs = self._build_completion_kwargs(messages, config)

        log.debug(
            "llm.request.started",
            model=config.model,
            message_count=len(messages),
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )

        response = await litellm.acompletion(**kwargs)

        log.debug(
            "llm.request.completed",
            model=config.model,
            finish_reason=response.choices[0].finish_reason,
        )

        return response

    def _parse_response(
        self,
        response: litellm.ModelResponse,
        config: CompletionConfig,
    ) -> CompletionResponse:
        """Parse the LiteLLM response into CompletionResponse.

        Args:
            response: The raw LiteLLM response.
            config: The completion configuration.

        Returns:
            Parsed CompletionResponse.
        """
        choice = response.choices[0]
        usage = response.usage
        content = choice.message.content or ""

        # Security: Validate LLM response length to prevent DoS
        is_valid, error_msg = InputValidator.validate_llm_response(content)
        if not is_valid:
            log.warning(
                "llm.response.truncated",
                model=config.model,
                original_length=len(content),
                max_length=MAX_LLM_RESPONSE_LENGTH,
            )
            # Truncate oversized responses instead of failing
            content = content[:MAX_LLM_RESPONSE_LENGTH]

        return CompletionResponse(
            content=content,
            model=response.model or config.model,
            usage=UsageInfo(
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
                total_tokens=usage.total_tokens if usage else 0,
            ),
            finish_reason=choice.finish_reason or "stop",
            raw_response=response.model_dump() if hasattr(response, "model_dump") else {},
        )

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Make a completion request to the LLM provider.

        This method handles retries internally and converts
        all expected failures to Result.err(ProviderError).

        Args:
            messages: The conversation messages to send.
            config: Configuration for the completion request.

        Returns:
            Result containing either the completion response or a ProviderError.
        """

        # Create the retry-decorated function with instance's max_retries
        @retry_async(
            on=RETRIABLE_EXCEPTIONS,
            attempts=self._max_retries,
            wait_initial=1.0,
            wait_max=10.0,
            wait_jitter=1.0,
        )
        async def _with_retry() -> litellm.ModelResponse:
            return await self._raw_complete(messages, config)

        try:
            response = await _with_retry()
            return Result.ok(self._parse_response(response, config))
        except RETRIABLE_EXCEPTIONS as e:
            # All retries exhausted
            log.warning(
                "llm.request.failed.retries_exhausted",
                model=config.model,
                error=str(e),
                max_retries=self._max_retries,
            )
            return Result.err(
                ProviderError.from_exception(e, provider=self._extract_provider(config.model))
            )
        except litellm.APIError as e:
            # Non-retriable API error
            log.warning(
                "llm.request.failed.api_error",
                model=config.model,
                error=str(e),
                status_code=getattr(e, "status_code", None),
            )
            return Result.err(
                ProviderError.from_exception(e, provider=self._extract_provider(config.model))
            )
        except litellm.AuthenticationError as e:
            log.warning(
                "llm.request.failed.auth_error",
                model=config.model,
                error=str(e),
            )
            return Result.err(
                ProviderError(
                    "Authentication failed - check API key",
                    provider=self._extract_provider(config.model),
                    status_code=401,
                    details={"original_exception": type(e).__name__},
                )
            )
        except litellm.BadRequestError as e:
            log.warning(
                "llm.request.failed.bad_request",
                model=config.model,
                error=str(e),
            )
            return Result.err(
                ProviderError.from_exception(e, provider=self._extract_provider(config.model))
            )
        except Exception as e:
            # Unexpected error - log and convert to ProviderError
            log.exception(
                "llm.request.failed.unexpected",
                model=config.model,
                error=str(e),
            )
            return Result.err(
                ProviderError(
                    f"Unexpected error: {e!s}",
                    provider=self._extract_provider(config.model),
                    details={"original_exception": type(e).__name__},
                )
            )

    def _extract_provider(self, model: str) -> str:
        """Extract the provider name from a model string.

        Args:
            model: The model identifier (e.g., 'openrouter/openai/gpt-4').

        Returns:
            The provider name (e.g., 'openrouter').
        """
        if "/" in model:
            return model.split("/")[0]
        # Common model prefixes
        if (
            model.startswith("gpt")
            or model.startswith("o1")
            or model.startswith("o3")
            or model.startswith("o4")
        ):
            return "openai"
        if model.startswith("claude"):
            return "anthropic"
        if model.startswith("gemini"):
            return "google"
        return "unknown"
