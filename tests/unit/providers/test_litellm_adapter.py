"""Unit tests for ouroboros.providers.litellm_adapter module."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import litellm

from ouroboros.core.errors import ProviderError
from ouroboros.providers.base import (
    CompletionConfig,
    Message,
    MessageRole,
)
from ouroboros.providers.litellm_adapter import LiteLLMAdapter


def create_mock_response(
    content: str = "Hello!",
    model: str = "gpt-4",
    prompt_tokens: int = 10,
    completion_tokens: int = 20,
    finish_reason: str = "stop",
) -> MagicMock:
    """Create a mock LiteLLM response."""
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = content
    mock_response.choices[0].finish_reason = finish_reason
    mock_response.model = model
    mock_response.usage = MagicMock()
    mock_response.usage.prompt_tokens = prompt_tokens
    mock_response.usage.completion_tokens = completion_tokens
    mock_response.usage.total_tokens = prompt_tokens + completion_tokens
    mock_response.model_dump = MagicMock(return_value={"id": "test"})
    return mock_response


class TestLiteLLMAdapterInit:
    """Test LiteLLMAdapter initialization."""

    def test_init_defaults(self) -> None:
        """LiteLLMAdapter initializes with sensible defaults."""
        adapter = LiteLLMAdapter()

        assert adapter._api_key is None
        assert adapter._api_base is None
        assert adapter._timeout == 60.0
        assert adapter._max_retries == 3

    def test_init_custom_values(self) -> None:
        """LiteLLMAdapter accepts custom initialization values."""
        adapter = LiteLLMAdapter(
            api_key="test-key",
            api_base="https://api.example.com",
            timeout=30.0,
            max_retries=5,
        )

        assert adapter._api_key == "test-key"
        assert adapter._api_base == "https://api.example.com"
        assert adapter._timeout == 30.0
        assert adapter._max_retries == 5


class TestLiteLLMAdapterGetApiKey:
    """Test LiteLLMAdapter._get_api_key method."""

    def test_explicit_api_key_takes_priority(self) -> None:
        """Explicit api_key overrides environment variables."""
        adapter = LiteLLMAdapter(api_key="explicit-key")

        result = adapter._get_api_key("openrouter/openai/gpt-4")

        assert result == "explicit-key"

    def test_openrouter_model_uses_openrouter_key(self) -> None:
        """OpenRouter models use OPENROUTER_API_KEY."""
        adapter = LiteLLMAdapter()

        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "or-key"}):
            result = adapter._get_api_key("openrouter/openai/gpt-4")

        assert result == "or-key"

    def test_anthropic_model_uses_anthropic_key(self) -> None:
        """Anthropic models use ANTHROPIC_API_KEY."""
        adapter = LiteLLMAdapter()

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "ant-key"}):
            result = adapter._get_api_key("anthropic/claude-3-opus")

        assert result == "ant-key"

    def test_claude_prefix_uses_anthropic_key(self) -> None:
        """Models starting with 'claude' use ANTHROPIC_API_KEY."""
        adapter = LiteLLMAdapter()

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "ant-key"}):
            result = adapter._get_api_key("claude-3-opus-20240229")

        assert result == "ant-key"

    def test_openai_model_uses_openai_key(self) -> None:
        """OpenAI models use OPENAI_API_KEY."""
        adapter = LiteLLMAdapter()

        with patch.dict("os.environ", {"OPENAI_API_KEY": "oai-key"}):
            result = adapter._get_api_key("openai/gpt-4")

        assert result == "oai-key"

    def test_gpt_prefix_uses_openai_key(self) -> None:
        """Models starting with 'gpt' use OPENAI_API_KEY."""
        adapter = LiteLLMAdapter()

        with patch.dict("os.environ", {"OPENAI_API_KEY": "oai-key"}):
            result = adapter._get_api_key("gpt-4-turbo")

        assert result == "oai-key"

    def test_unknown_model_defaults_to_openrouter(self) -> None:
        """Unknown models default to OPENROUTER_API_KEY."""
        adapter = LiteLLMAdapter()

        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "or-key"}):
            result = adapter._get_api_key("some-unknown-model")

        assert result == "or-key"

    def test_missing_env_var_returns_none(self) -> None:
        """Returns None if no environment variable is set."""
        adapter = LiteLLMAdapter()

        with patch.dict("os.environ", {}, clear=True):
            result = adapter._get_api_key("openrouter/openai/gpt-4")

        assert result is None


class TestLiteLLMAdapterBuildCompletionKwargs:
    """Test LiteLLMAdapter._build_completion_kwargs method."""

    def test_builds_basic_kwargs(self) -> None:
        """Builds correct kwargs from messages and config."""
        adapter = LiteLLMAdapter()
        messages = [Message(role=MessageRole.USER, content="Hello")]
        config = CompletionConfig(model="gpt-4")

        with patch.dict("os.environ", {}, clear=True):
            kwargs = adapter._build_completion_kwargs(messages, config)

        assert kwargs["model"] == "gpt-4"
        assert kwargs["messages"] == [{"role": "user", "content": "Hello"}]
        assert kwargs["temperature"] == 0.7
        assert kwargs["max_tokens"] == 4096
        assert kwargs["top_p"] == 1.0
        assert kwargs["timeout"] == 60.0
        assert "stop" not in kwargs

    def test_includes_stop_sequences(self) -> None:
        """Includes stop sequences when provided."""
        adapter = LiteLLMAdapter()
        messages = [Message(role=MessageRole.USER, content="Hello")]
        config = CompletionConfig(model="gpt-4", stop=["###", "END"])

        with patch.dict("os.environ", {}, clear=True):
            kwargs = adapter._build_completion_kwargs(messages, config)

        assert kwargs["stop"] == ["###", "END"]

    def test_includes_api_key_when_available(self) -> None:
        """Includes api_key when available from environment."""
        adapter = LiteLLMAdapter()
        messages = [Message(role=MessageRole.USER, content="Hello")]
        config = CompletionConfig(model="openrouter/openai/gpt-4")

        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}):
            kwargs = adapter._build_completion_kwargs(messages, config)

        assert kwargs["api_key"] == "test-key"

    def test_includes_api_base_when_set(self) -> None:
        """Includes api_base when set in constructor."""
        adapter = LiteLLMAdapter(api_base="https://custom.api")
        messages = [Message(role=MessageRole.USER, content="Hello")]
        config = CompletionConfig(model="gpt-4")

        with patch.dict("os.environ", {}, clear=True):
            kwargs = adapter._build_completion_kwargs(messages, config)

        assert kwargs["api_base"] == "https://custom.api"


class TestLiteLLMAdapterParseResponse:
    """Test LiteLLMAdapter._parse_response method."""

    def test_parses_successful_response(self) -> None:
        """Parses LiteLLM response into CompletionResponse."""
        adapter = LiteLLMAdapter()
        mock_response = create_mock_response(
            content="Hello, world!",
            model="gpt-4-turbo",
            prompt_tokens=15,
            completion_tokens=25,
        )
        config = CompletionConfig(model="gpt-4")

        result = adapter._parse_response(mock_response, config)

        assert result.content == "Hello, world!"
        assert result.model == "gpt-4-turbo"
        assert result.usage.prompt_tokens == 15
        assert result.usage.completion_tokens == 25
        assert result.usage.total_tokens == 40
        assert result.finish_reason == "stop"

    def test_handles_empty_content(self) -> None:
        """Handles None content in response."""
        adapter = LiteLLMAdapter()
        mock_response = create_mock_response()
        mock_response.choices[0].message.content = None
        config = CompletionConfig(model="gpt-4")

        result = adapter._parse_response(mock_response, config)

        assert result.content == ""

    def test_handles_missing_usage(self) -> None:
        """Handles missing usage information."""
        adapter = LiteLLMAdapter()
        mock_response = create_mock_response()
        mock_response.usage = None
        config = CompletionConfig(model="gpt-4")

        result = adapter._parse_response(mock_response, config)

        assert result.usage.prompt_tokens == 0
        assert result.usage.completion_tokens == 0
        assert result.usage.total_tokens == 0

    def test_uses_config_model_when_response_model_missing(self) -> None:
        """Uses config model when response model is None."""
        adapter = LiteLLMAdapter()
        mock_response = create_mock_response()
        mock_response.model = None
        config = CompletionConfig(model="fallback-model")

        result = adapter._parse_response(mock_response, config)

        assert result.model == "fallback-model"


class TestLiteLLMAdapterExtractProvider:
    """Test LiteLLMAdapter._extract_provider method."""

    def test_extracts_openrouter_provider(self) -> None:
        """Extracts 'openrouter' from OpenRouter model strings."""
        adapter = LiteLLMAdapter()

        result = adapter._extract_provider("openrouter/openai/gpt-4")

        assert result == "openrouter"

    def test_extracts_anthropic_provider(self) -> None:
        """Extracts 'anthropic' from Anthropic model strings."""
        adapter = LiteLLMAdapter()

        result = adapter._extract_provider("anthropic/claude-3-opus")

        assert result == "anthropic"

    def test_infers_openai_from_gpt_prefix(self) -> None:
        """Infers 'openai' from gpt- prefix."""
        adapter = LiteLLMAdapter()

        result = adapter._extract_provider("gpt-4-turbo")

        assert result == "openai"

    def test_infers_anthropic_from_claude_prefix(self) -> None:
        """Infers 'anthropic' from claude prefix."""
        adapter = LiteLLMAdapter()

        result = adapter._extract_provider("claude-3-opus")

        assert result == "anthropic"

    def test_unknown_model_returns_unknown(self) -> None:
        """Returns 'unknown' for unrecognized model strings."""
        adapter = LiteLLMAdapter()

        result = adapter._extract_provider("some-custom-model")

        assert result == "unknown"


class TestLiteLLMAdapterComplete:
    """Test LiteLLMAdapter.complete method."""

    async def test_successful_completion(self) -> None:
        """Returns Result.ok on successful completion."""
        adapter = LiteLLMAdapter()
        messages = [Message(role=MessageRole.USER, content="Hello")]
        config = CompletionConfig(model="gpt-4")
        mock_response = create_mock_response(content="Hi there!")

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_acompletion:
            mock_acompletion.return_value = mock_response

            result = await adapter.complete(messages, config)

        assert result.is_ok
        assert result.value.content == "Hi there!"

    async def test_rate_limit_error_returns_result_err(self) -> None:
        """Returns Result.err on rate limit error after retries."""
        adapter = LiteLLMAdapter(max_retries=1)
        messages = [Message(role=MessageRole.USER, content="Hello")]
        config = CompletionConfig(model="gpt-4")

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_acompletion:
            mock_acompletion.side_effect = litellm.RateLimitError(
                message="Rate limited",
                llm_provider="openai",
                model="gpt-4",
            )

            result = await adapter.complete(messages, config)

        assert result.is_err
        assert isinstance(result.error, ProviderError)
        assert "Rate limited" in result.error.message

    async def test_api_error_returns_result_err(self) -> None:
        """Returns Result.err on API error."""
        adapter = LiteLLMAdapter(max_retries=1)
        messages = [Message(role=MessageRole.USER, content="Hello")]
        config = CompletionConfig(model="gpt-4")

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_acompletion:
            mock_acompletion.side_effect = litellm.APIError(
                message="Internal server error",
                status_code=500,
                llm_provider="openai",
                model="gpt-4",
            )

            result = await adapter.complete(messages, config)

        assert result.is_err
        assert isinstance(result.error, ProviderError)

    async def test_auth_error_returns_result_err(self) -> None:
        """Returns Result.err on authentication error."""
        adapter = LiteLLMAdapter()
        messages = [Message(role=MessageRole.USER, content="Hello")]
        config = CompletionConfig(model="gpt-4")

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_acompletion:
            mock_acompletion.side_effect = litellm.AuthenticationError(
                message="Invalid API key",
                llm_provider="openai",
                model="gpt-4",
            )

            result = await adapter.complete(messages, config)

        assert result.is_err
        assert isinstance(result.error, ProviderError)
        assert result.error.status_code == 401
        assert "Authentication failed" in result.error.message

    async def test_bad_request_error_returns_result_err(self) -> None:
        """Returns Result.err on bad request error."""
        adapter = LiteLLMAdapter()
        messages = [Message(role=MessageRole.USER, content="Hello")]
        config = CompletionConfig(model="gpt-4")

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_acompletion:
            mock_acompletion.side_effect = litellm.BadRequestError(
                message="Invalid model",
                llm_provider="openai",
                model="gpt-4",
            )

            result = await adapter.complete(messages, config)

        assert result.is_err
        assert isinstance(result.error, ProviderError)

    async def test_unexpected_error_returns_result_err(self) -> None:
        """Returns Result.err on unexpected error."""
        adapter = LiteLLMAdapter()
        messages = [Message(role=MessageRole.USER, content="Hello")]
        config = CompletionConfig(model="gpt-4")

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_acompletion:
            mock_acompletion.side_effect = RuntimeError("Something unexpected")

            result = await adapter.complete(messages, config)

        assert result.is_err
        assert isinstance(result.error, ProviderError)
        assert "Unexpected error" in result.error.message

    async def test_openrouter_model_routing(self) -> None:
        """Correctly routes OpenRouter model strings."""
        adapter = LiteLLMAdapter()
        messages = [Message(role=MessageRole.USER, content="Hello")]
        config = CompletionConfig(model="openrouter/anthropic/claude-3-opus")
        mock_response = create_mock_response()

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_acompletion:
            mock_acompletion.return_value = mock_response
            with patch.dict("os.environ", {"OPENROUTER_API_KEY": "or-key"}):
                await adapter.complete(messages, config)

        # Verify the model was passed correctly
        call_kwargs = mock_acompletion.call_args.kwargs
        assert call_kwargs["model"] == "openrouter/anthropic/claude-3-opus"
        assert call_kwargs["api_key"] == "or-key"

    async def test_result_contains_usage_info(self) -> None:
        """Result contains correct usage information."""
        adapter = LiteLLMAdapter()
        messages = [Message(role=MessageRole.USER, content="Hello")]
        config = CompletionConfig(model="gpt-4")
        mock_response = create_mock_response(
            prompt_tokens=50,
            completion_tokens=100,
        )

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_acompletion:
            mock_acompletion.return_value = mock_response

            result = await adapter.complete(messages, config)

        assert result.is_ok
        assert result.value.usage.prompt_tokens == 50
        assert result.value.usage.completion_tokens == 100
        assert result.value.usage.total_tokens == 150


class TestLiteLLMAdapterRetryBehavior:
    """Test retry behavior using stamina."""

    async def test_retries_on_rate_limit(self) -> None:
        """Retries on rate limit error before giving up."""
        adapter = LiteLLMAdapter(max_retries=3)
        messages = [Message(role=MessageRole.USER, content="Hello")]
        config = CompletionConfig(model="gpt-4")
        mock_response = create_mock_response()

        call_count = 0

        async def side_effect(**kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise litellm.RateLimitError(
                    message="Rate limited",
                    llm_provider="openai",
                    model="gpt-4",
                )
            return mock_response

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_acompletion:
            mock_acompletion.side_effect = side_effect

            result = await adapter.complete(messages, config)

        assert result.is_ok
        assert call_count == 3  # 2 failures + 1 success

    async def test_retries_on_service_unavailable(self) -> None:
        """Retries on service unavailable error."""
        adapter = LiteLLMAdapter(max_retries=2)
        messages = [Message(role=MessageRole.USER, content="Hello")]
        config = CompletionConfig(model="gpt-4")
        mock_response = create_mock_response()

        call_count = 0

        async def side_effect(**kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise litellm.ServiceUnavailableError(
                    message="Service unavailable",
                    llm_provider="openai",
                    model="gpt-4",
                )
            return mock_response

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_acompletion:
            mock_acompletion.side_effect = side_effect

            result = await adapter.complete(messages, config)

        assert result.is_ok
        assert call_count == 2

    async def test_retries_on_timeout(self) -> None:
        """Retries on timeout error."""
        adapter = LiteLLMAdapter(max_retries=2)
        messages = [Message(role=MessageRole.USER, content="Hello")]
        config = CompletionConfig(model="gpt-4")
        mock_response = create_mock_response()

        call_count = 0

        async def side_effect(**kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise litellm.Timeout(
                    message="Request timed out",
                    llm_provider="openai",
                    model="gpt-4",
                )
            return mock_response

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_acompletion:
            mock_acompletion.side_effect = side_effect

            result = await adapter.complete(messages, config)

        assert result.is_ok
        assert call_count == 2

    async def test_retries_on_connection_error(self) -> None:
        """Retries on API connection error."""
        adapter = LiteLLMAdapter(max_retries=2)
        messages = [Message(role=MessageRole.USER, content="Hello")]
        config = CompletionConfig(model="gpt-4")
        mock_response = create_mock_response()

        call_count = 0

        async def side_effect(**kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise litellm.APIConnectionError(
                    message="Connection failed",
                    llm_provider="openai",
                    model="gpt-4",
                )
            return mock_response

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_acompletion:
            mock_acompletion.side_effect = side_effect

            result = await adapter.complete(messages, config)

        assert result.is_ok
        assert call_count == 2

    async def test_gives_up_after_max_retries(self) -> None:
        """Returns error after max retries exhausted."""
        adapter = LiteLLMAdapter(max_retries=2)
        messages = [Message(role=MessageRole.USER, content="Hello")]
        config = CompletionConfig(model="gpt-4")

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_acompletion:
            mock_acompletion.side_effect = litellm.RateLimitError(
                message="Rate limited",
                llm_provider="openai",
                model="gpt-4",
            )

            result = await adapter.complete(messages, config)

        assert result.is_err
        assert isinstance(result.error, ProviderError)


class TestLiteLLMAdapterProtocolCompliance:
    """Test that LiteLLMAdapter implements LLMAdapter protocol."""

    def test_implements_llm_adapter_protocol(self) -> None:
        """LiteLLMAdapter implements the LLMAdapter protocol."""
        from ouroboros.providers.base import LLMAdapter

        adapter = LiteLLMAdapter()

        # Runtime check - has complete method
        assert hasattr(adapter, "complete")
        assert callable(adapter.complete)

        # Type check - can be assigned to protocol type
        _: LLMAdapter = adapter  # This should not raise type errors
