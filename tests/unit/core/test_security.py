"""Tests for security utilities module.

Tests cover:
- API key masking
- API key format validation
- Sensitive field/value detection
- Input validation
- Sanitization for logging
"""

from enum import StrEnum
from time import perf_counter

from ouroboros.core.security import (
    MAX_INITIAL_CONTEXT_LENGTH,
    MAX_LLM_RESPONSE_LENGTH,
    MAX_SEED_FILE_SIZE,
    MAX_USER_RESPONSE_LENGTH,
    InputValidator,
    is_credential_shaped,
    is_sensitive_field,
    is_sensitive_value,
    is_stable_authority_identity,
    mask_api_key,
    mask_sensitive_value,
    sanitize_for_logging,
    truncate_input,
    validate_api_key_format,
)


class TestMaskApiKey:
    """Tests for mask_api_key function."""

    def test_mask_empty_key(self) -> None:
        """Empty key returns <empty>."""
        assert mask_api_key("") == "<empty>"

    def test_mask_short_key(self) -> None:
        """Short keys are fully masked."""
        assert mask_api_key("abc") == "***"
        assert mask_api_key("12345678") == "********"

    def test_mask_openai_key(self) -> None:
        """OpenAI key shows prefix and last chars."""
        result = mask_api_key("sk-1234567890abcdef")
        assert result == "sk-...cdef"

    def test_mask_anthropic_key(self) -> None:
        """Anthropic key shows prefix and last chars."""
        result = mask_api_key("sk-ant-1234567890abcdef")
        assert result == "sk-...cdef"

    def test_mask_key_without_prefix(self) -> None:
        """Key without dash prefix shows only last chars."""
        result = mask_api_key("AIzaSyBxxxxxxxxxxxxxxxxxx")
        assert result.endswith("xxxx")
        assert result.startswith("...")

    def test_mask_custom_visible_chars(self) -> None:
        """Custom visible_chars parameter works."""
        result = mask_api_key("sk-1234567890abcdef", visible_chars=6)
        assert result.endswith("abcdef")


class TestValidateApiKeyFormat:
    """Tests for validate_api_key_format function."""

    def test_empty_key_invalid(self) -> None:
        """Empty key is invalid."""
        assert validate_api_key_format("") is False

    def test_short_key_invalid(self) -> None:
        """Key shorter than 10 chars is invalid."""
        assert validate_api_key_format("sk-123") is False

    def test_openai_key_format(self) -> None:
        """Valid OpenAI key format."""
        assert validate_api_key_format("sk-12345678901234567890", provider="openai") is True

    def test_anthropic_key_format(self) -> None:
        """Valid Anthropic key format."""
        assert validate_api_key_format("sk-ant-12345678901234567890", provider="anthropic") is True

    def test_generic_key_format(self) -> None:
        """Generic alphanumeric key is valid."""
        assert validate_api_key_format("abcdefghij1234567890") is True

    def test_key_with_special_chars_invalid(self) -> None:
        """Key with special characters (not underscore/dash) is invalid."""
        assert validate_api_key_format("abc!@#defghij") is False


class TestSensitiveDetection:
    """Tests for sensitive field/value detection."""

    def test_sensitive_field_names(self) -> None:
        """Common sensitive field names are detected."""
        assert is_sensitive_field("api_key") is True
        assert is_sensitive_field("password") is True
        assert is_sensitive_field("secret") is True
        assert is_sensitive_field("token") is True
        assert is_sensitive_field("ANTHROPIC_API_KEY") is True
        assert is_sensitive_field("my_secret_value") is True

    def test_non_sensitive_field_names(self) -> None:
        """Normal field names are not flagged."""
        assert is_sensitive_field("name") is False
        assert is_sensitive_field("email") is False
        assert is_sensitive_field("model") is False
        assert is_sensitive_field("") is False

    def test_sensitive_values(self) -> None:
        """Values that look like secrets are detected."""
        assert is_sensitive_value("sk-1234567890") is True
        assert is_sensitive_value("Bearer token123") is True
        assert is_sensitive_value("AIzaXXXXXXXXXXXXXXX") is True

    def test_non_sensitive_values(self) -> None:
        """Normal values are not flagged."""
        assert is_sensitive_value("hello world") is False
        assert is_sensitive_value("model-gpt-4") is False
        assert is_sensitive_value(123) is False

    def test_credential_shapes_cover_common_provider_formats(self) -> None:
        for value in (
            "ghp_credential-shaped-value",
            "sk_live_credential-shaped-value",
            "AIza" + "A" * 35,
            "AKIA" + "A" * 16,
            "ASIA" + "A" * 16,
            "github:ghp_namespaced-credential",
            "api_key:opaque-provider-credential",
            "access_token/opaque-provider-credential",
            "accessToken:opaqueProviderSecret",
            "clientSecret/opaqueProviderSecret",
            "api_key.opaqueProviderSecret",
            "access_token_opaque-provider-credential",
            "access-token-opaque-provider-credential",
            "credentials:opaquevalue",
            "credentials_opaquevalue",
            "passwd:opaquevalue",
            "glpat-opaque-provider-credential",
            "hf_opaque-provider-credential",
            *(
                f"runtime:auth{label}value"
                for label in (
                    "bearer",
                    "credential",
                    "key",
                    "opaque",
                    "password",
                    "secret",
                    "token",
                    "value",
                )
            ),
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature",
            "SG." + "A" * 22 + "." + "B" * 43,
            "hvs." + "A" * 24,
        ):
            assert is_credential_shaped(value) is True
        assert is_credential_shaped("authority-session-123") is False
        assert is_credential_shaped("github:read") is False
        assert is_credential_shaped("token-budget") is False
        assert is_credential_shaped("auth-plane:default") is False
        assert is_credential_shaped("keycloak") is False
        assert is_credential_shaped("tokenizer") is False
        assert is_credential_shaped("privateer") is False

        class HostileString(str):
            def strip(self) -> str:
                raise RuntimeError("hostile normalization")

        # A hostile subclass must not raise, and must fail *closed*: because a
        # False verdict means "safe to copy/log", a credential-shaped payload
        # wrapped in a subclass is reported as credential-shaped.
        assert is_credential_shaped(HostileString("runtime:apikey123")) is True
        # Authority identity fails closed in the opposite direction: a
        # caller-controlled subclass is rejected outright.
        assert is_stable_authority_identity(HostileString("runtime:claude")) is False

    def test_stable_authority_identity_is_allowlisted_and_non_secret(self) -> None:
        for value in (
            "authority-default",
            "session-a",
            "runtime:claude",
            "runtime:claude_code",
            "runtime:claude_mcp",
            "runtime:codex_cli",
            "runtime:codex_mcp",
            "runtime:gemini_cli",
            "runtime:keycloak",
            "runtime:tokenizer",
            "runtime:privateer",
            "runtime:token-budget",
            "runtime:auth-plane:default",
            "workspace/project-1",
            "default",
        ):
            assert is_stable_authority_identity(value) is True
        for value in (
            "opaque-provider-id",
            "token-budget",
            "SG." + "A" * 22 + "." + "B" * 43,
            "hvs." + "A" * 24,
            "runtime:SG." + "A" * 22 + "." + "B" * 43,
            "runtime:wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "runtime.hvs." + "A" * 24,
            "runtime.SG." + "A" * 22 + "." + "B" * 43,
            "runtime:access_token-abc123",
            "runtime:api_key-abc123",
            "runtime:client_secret-abc123",
            "runtime:apikey123",
            "runtime:accesstokenabc123",
            "runtime:password123",
            "runtime:keyabc123",
            "runtime:openai-api-key-sk-abcdefghijklmnopqrstuvwxyz",
            "runtime:prod-ghp_abcdefghijklmnopqrstuvwxyz",
            "runtime:prod-sk_live_abcdefghijklmnopqrstuvwxyz",
            "runtime:prod-hvs.abcdefghijklmnopqrstuvwxyz",
            "runtime:secretopaquevalue",
            "runtime:tokenopaquevalue",
            "runtime:credentialopaquevalue",
            *(
                f"runtime:auth{label}value"
                for label in (
                    "bearer",
                    "credential",
                    "key",
                    "opaque",
                    "password",
                    "secret",
                    "token",
                    "value",
                )
            ),
            "runtime:clientsecret:opaquevalue",
            "runtime:accesstoken:opaquevalue",
            "runtime:accesskey:opaquevalue",
            "runtime:privatekey:opaquevalue",
            "runtime:" + "x" * 200,
            "runtime:dop_v1_" + "x" * 64,
            "runtime:opaqueprovider",
        ):
            assert is_stable_authority_identity(value) is False

    def test_stable_authority_identity_rejects_hostile_input_in_linear_time(self) -> None:
        hostile = "runtime:" + "a-" * 24 + ":"

        started = perf_counter()
        assert is_stable_authority_identity(hostile) is False
        assert perf_counter() - started < 0.25


class TestMaskSensitiveValue:
    """Tests for mask_sensitive_value function."""

    def test_mask_none(self) -> None:
        """None returns <None>."""
        assert mask_sensitive_value(None) == "<None>"

    def test_mask_by_field_name(self) -> None:
        """Value is masked based on sensitive field name."""
        assert mask_sensitive_value("any_value", "api_key") == "<REDACTED>"

    def test_mask_by_value_pattern(self) -> None:
        """Value is masked based on its pattern."""
        result = mask_sensitive_value("sk-1234567890abcdef")
        assert "REDACTED" not in result  # Pattern detection uses mask_api_key
        assert "sk-" in result

        compact_auth_result = mask_sensitive_value("runtime:authsecretvalue")
        assert compact_auth_result != "runtime:authsecretvalue"

    def test_truncate_long_string(self) -> None:
        """Long strings are truncated."""
        long_string = "x" * 200
        result = mask_sensitive_value(long_string)
        assert "200 chars" in result


class TestSanitizeForLogging:
    """Tests for sanitize_for_logging function."""

    def test_sanitize_simple_dict(self) -> None:
        """Sensitive keys are redacted."""
        data = {"api_key": "sk-secret", "name": "test"}
        result = sanitize_for_logging(data)
        assert result["api_key"] == "<REDACTED>"
        assert result["name"] == "test"

    def test_sanitize_nested_dict(self) -> None:
        """Nested dictionaries are sanitized recursively."""
        data = {"config": {"provider": {"api_key": "sk-secret"}}, "name": "test"}
        result = sanitize_for_logging(data)
        assert result["config"]["provider"]["api_key"] == "<REDACTED>"
        assert result["name"] == "test"

    def test_sanitize_sensitive_value_pattern(self) -> None:
        """Values matching sensitive patterns are masked."""
        data = {"some_field": "sk-1234567890abcdef"}
        result = sanitize_for_logging(data)
        assert "sk-" in result["some_field"]
        assert "abcdef" not in result["some_field"]  # Fully masked

    def test_sanitize_dict_inside_list(self) -> None:
        """Secrets nested inside a list are not leaked verbatim."""
        data = {"providers": [{"api_key": "sk-live-abc123"}]}
        result = sanitize_for_logging(data)
        assert result["providers"][0]["api_key"] == "<REDACTED>"
        assert "sk-live-abc123" not in repr(result)

    def test_sanitize_dict_inside_tuple_preserves_type(self) -> None:
        """Tuples recurse and stay tuples; lists stay lists."""
        data = {"providers": ({"api_key": "sk-live-abc123"},), "names": [{"name": "ok"}]}
        result = sanitize_for_logging(data)
        assert isinstance(result["providers"], tuple)
        assert isinstance(result["names"], list)
        assert result["providers"][0]["api_key"] == "<REDACTED>"
        assert result["names"][0]["name"] == "ok"

    def test_sanitize_nested_lists_of_lists(self) -> None:
        """Lists of lists recurse to arbitrary depth."""
        data = {"batches": [[{"token": "sk-live-abc123"}], [[{"api_key": "sk-live-xyz789"}]]]}
        result = sanitize_for_logging(data)
        assert result["batches"][0][0]["token"] == "<REDACTED>"
        assert result["batches"][1][0][0]["api_key"] == "<REDACTED>"
        assert "sk-live-abc123" not in repr(result)
        assert "sk-live-xyz789" not in repr(result)

    def test_sanitize_raw_secret_string_inside_list(self) -> None:
        """A bare credential string inside a list is masked like a scalar."""
        secret = "sk-1234567890abcdef"
        data = {"values": [secret, "harmless"]}
        result = sanitize_for_logging(data)
        assert result["values"][0] != secret
        assert "abcdef" not in repr(result)
        assert result["values"][1] == "harmless"

    def test_sanitize_returns_dict_for_dict_input(self) -> None:
        """Dict input still yields a dict, and non-sequence values pass through."""
        data = {"count": 3, "enabled": True, "ratio": 1.5, "nothing": None}
        result = sanitize_for_logging(data)
        assert isinstance(result, dict)
        assert result == data


class TestStrSubclassGuardBypass:
    """Guards must inspect ``str`` subclasses such as ``enum.StrEnum`` members."""

    def test_credential_shaped_detects_str_enum_member(self) -> None:
        """A StrEnum member carrying a credential is not waved through."""

        class Mode(StrEnum):
            LEAKY = "sk-live-abc123def456ghi789"

        assert is_credential_shaped("sk-live-abc123def456ghi789") is True
        assert is_credential_shaped(Mode.LEAKY) is True

    def test_credential_shaped_still_rejects_non_strings(self) -> None:
        """Non-string inputs remain non-credential-shaped."""
        assert is_credential_shaped(None) is False  # type: ignore[arg-type]
        assert is_credential_shaped(123) is False  # type: ignore[arg-type]
        assert is_credential_shaped(b"sk-live-abc123def456") is False  # type: ignore[arg-type]

    def test_stable_authority_identity_rejects_str_subclasses_fail_closed(self) -> None:
        """Authority identity fails closed: subclasses are rejected, not accepted."""

        class Authority(StrEnum):
            SAFE = "runtime:claude"
            LEAKY = "sk-live-abc123def456ghi789"

        # A False verdict here means rejection, so declining a subclass is the
        # safe direction. Callers normalize to a built-in ``str`` first.
        assert is_stable_authority_identity(Authority.SAFE) is False
        assert is_stable_authority_identity(str(Authority.SAFE)) is True
        assert is_stable_authority_identity(Authority.LEAKY) is False
        assert is_stable_authority_identity(str(Authority.LEAKY)) is False
        assert is_stable_authority_identity(None) is False  # type: ignore[arg-type]

    def test_sensitive_value_detects_str_enum_member(self) -> None:
        """The log-sanitization path also sees through StrEnum members."""

        class Mode(StrEnum):
            LEAKY = "sk-live-abc123def456ghi789"

        assert is_sensitive_value(Mode.LEAKY) is True
        result = sanitize_for_logging({"mode": Mode.LEAKY})
        assert "abc123def456ghi789" not in repr(result)


class TestTruncateInput:
    """Tests for truncate_input function."""

    def test_short_text_unchanged(self) -> None:
        """Text within limit is unchanged."""
        text = "short text"
        assert truncate_input(text, 100) == text

    def test_long_text_truncated(self) -> None:
        """Long text is truncated with suffix."""
        text = "this is a long text that should be truncated"
        result = truncate_input(text, 20)
        assert len(result) == 20
        assert result.endswith("...")

    def test_custom_suffix(self) -> None:
        """Custom suffix works."""
        text = "this is a long text"
        result = truncate_input(text, 15, suffix="[more]")
        assert result.endswith("[more]")


class TestInputValidator:
    """Tests for InputValidator class."""

    def test_validate_initial_context_empty(self) -> None:
        """Empty context is invalid."""
        is_valid, error = InputValidator.validate_initial_context("")
        assert is_valid is False
        assert "empty" in error.lower()

    def test_validate_initial_context_whitespace(self) -> None:
        """Whitespace-only context is invalid."""
        is_valid, error = InputValidator.validate_initial_context("   \n\t  ")
        assert is_valid is False
        assert "whitespace" in error.lower()

    def test_validate_initial_context_valid(self) -> None:
        """Valid context passes."""
        is_valid, error = InputValidator.validate_initial_context("Build a CLI tool")
        assert is_valid is True
        assert error == ""

    def test_validate_initial_context_too_long(self) -> None:
        """Context exceeding limit is invalid."""
        long_context = "x" * (MAX_INITIAL_CONTEXT_LENGTH + 1)
        is_valid, error = InputValidator.validate_initial_context(long_context)
        assert is_valid is False
        assert "maximum length" in error.lower()

    def test_validate_user_response_empty(self) -> None:
        """Empty response is invalid."""
        is_valid, error = InputValidator.validate_user_response("")
        assert is_valid is False

    def test_validate_user_response_valid(self) -> None:
        """Valid response passes."""
        is_valid, error = InputValidator.validate_user_response("Yes, I want feature X")
        assert is_valid is True

    def test_validate_user_response_too_long(self) -> None:
        """Response exceeding limit is invalid."""
        long_response = "x" * (MAX_USER_RESPONSE_LENGTH + 1)
        is_valid, error = InputValidator.validate_user_response(long_response)
        assert is_valid is False

    def test_validate_seed_file_size_empty(self) -> None:
        """Empty file is invalid."""
        is_valid, error = InputValidator.validate_seed_file_size(0)
        assert is_valid is False
        assert "empty" in error.lower()

    def test_validate_seed_file_size_valid(self) -> None:
        """Valid file size passes."""
        is_valid, error = InputValidator.validate_seed_file_size(1024)  # 1KB
        assert is_valid is True

    def test_validate_seed_file_size_too_large(self) -> None:
        """File exceeding limit is invalid."""
        is_valid, error = InputValidator.validate_seed_file_size(MAX_SEED_FILE_SIZE + 1)
        assert is_valid is False
        assert "maximum size" in error.lower()

    def test_validate_llm_response_empty(self) -> None:
        """Empty LLM response is valid (model may return empty)."""
        is_valid, error = InputValidator.validate_llm_response("")
        assert is_valid is True

    def test_validate_llm_response_valid(self) -> None:
        """Normal response passes."""
        is_valid, error = InputValidator.validate_llm_response("This is a response")
        assert is_valid is True

    def test_validate_llm_response_too_long(self) -> None:
        """Response exceeding limit is invalid."""
        long_response = "x" * (MAX_LLM_RESPONSE_LENGTH + 1)
        is_valid, error = InputValidator.validate_llm_response(long_response)
        assert is_valid is False
