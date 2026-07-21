"""Tests for security utilities module.

Tests cover:
- API key masking
- API key format validation
- Sensitive field/value detection
- Input validation
- Sanitization for logging
"""

import pytest

from ouroboros.core.security import (
    MAX_INITIAL_CONTEXT_LENGTH,
    MAX_LLM_RESPONSE_LENGTH,
    MAX_SEED_FILE_SIZE,
    MAX_USER_RESPONSE_LENGTH,
    InputValidator,
    is_sensitive_credential_field,
    is_sensitive_field,
    is_sensitive_value,
    mask_api_key,
    mask_sensitive_value,
    redact_sensitive_text,
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

    @pytest.mark.parametrize(
        "field_name",
        ("apiKeyValue", "accessTokenValue", "api_key", "auth", "authentication", "bearer"),
    )
    def test_sensitive_credential_field_aliases(self, field_name: str) -> None:
        """Durable-contract screening recognizes common credential aliases."""
        assert is_sensitive_credential_field(field_name) is True

    @pytest.mark.parametrize(
        "field_name",
        ("token_estimator", "resume_token", "resumeTokenValue", "key_value"),
    )
    def test_semantic_or_protocol_fields_are_not_credential_aliases(self, field_name: str) -> None:
        assert is_sensitive_credential_field(field_name) is False

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
        assert is_sensitive_value("ghp_abcdefghijklmnopqrstuvwxyz1234567890") is True
        assert is_sensitive_value("rk_live_" + "a" * 32) is True
        assert is_sensitive_value("rk_test_" + "b" * 32) is True
        assert is_sensitive_value("AIzaXXXXXXXXXXXXXXX") is True

    @pytest.mark.parametrize(
        "value",
        (
            "rk_live_",
            "rk_test_",
            "rk_live_fixture_name",
            "rk_test_fixture_name",
            "A note about rk_live_ keys",
            "A note about rk_test_ keys",
        ),
    )
    def test_short_restricted_key_labels_and_prose_are_not_sensitive(self, value: str) -> None:
        """A restricted-key prefix needs an opaque payload before it is a credential."""
        assert is_sensitive_value(value) is False
        assert redact_sensitive_text(value) == value

    @pytest.mark.parametrize("separator", (":", "/", "_", "\u200b"))
    def test_sensitive_values_embedded_after_a_label(self, separator: str) -> None:
        """A provider label cannot hide a credential-shaped token."""
        secret = "ghp_" + "a" * 36
        assert is_sensitive_value(f"claude{separator}{secret}") is True

    @pytest.mark.parametrize(
        "prefix",
        (" \t", "\x00", "\u200b", "\ufeff", "\u2060", "\u034f", "\ufe0f"),
    )
    def test_sensitive_values_remain_sensitive_after_prefix_normalization(
        self,
        prefix: str,
    ) -> None:
        """Whitespace and invisible format prefixes cannot bypass detection."""
        secret = "ghp_" + "a" * 36
        assert is_sensitive_value(f"{prefix}{secret}\n") is True

    @pytest.mark.parametrize(
        "credential",
        (
            "glpat-sentinel-not-a-real-token-123456",
            "hf_abcdefghijklmnopqrstuvwxyz123456",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzZW50aW5lbCJ9.c2lnbmF0dXJl",
        ),
    )
    def test_structured_sensitive_values(self, credential: str) -> None:
        """Common provider/JWT forms are sensitive even under benign keys."""
        assert is_sensitive_value(credential) is True
        assert is_sensitive_value(f"provider:{credential}") is True

    def test_non_sensitive_values(self) -> None:
        """Normal values are not flagged."""
        assert is_sensitive_value("hello world") is False
        assert is_sensitive_value("model-gpt-4") is False
        assert is_sensitive_value("quoted_secret_preview") is False
        assert is_sensitive_value("API-first design") is False
        assert is_sensitive_value("Implement token budget accounting") is False
        assert is_sensitive_value("token budget accounting") is False
        assert is_sensitive_value("Adopt an API-first design") is False
        assert is_sensitive_value("Adopt API-first-design-for-internal-services") is False
        assert is_sensitive_value(123) is False

    def test_generic_credential_prefixes_require_structured_opaque_payloads(self) -> None:
        payload = "a" * 14 + "123456"
        assert is_sensitive_value("api-" + "a" * 20) is True
        assert is_sensitive_value("token " + payload) is True
        assert is_sensitive_value("token internationalization") is False

    def test_hyphenated_api_prose_remains_non_sensitive(self) -> None:
        """A long API-first goal is semantic data, not a generic credential."""
        value = "Adopt API-first-design-for-internal-services"
        assert is_sensitive_value(value) is False
        assert redact_sensitive_text(value) == value

    @pytest.mark.parametrize(
        "value",
        ("PK-based authentication should remain durable", "secret_rotation_workflow"),
    )
    def test_ambiguous_security_prefix_prose_remains_non_sensitive(self, value: str) -> None:
        """Bare ``pk-`` and ``secret_`` prefixes do not identify credentials."""
        assert is_sensitive_value(value) is False
        assert redact_sensitive_text(value) == value

    @pytest.mark.parametrize(
        ("value", "expected"),
        (
            ("retry with token " + "a" * 14 + "123456", "retry with token [redacted]"),
            ("retry with api-" + "a" * 20, "retry with api-[redacted]"),
            (
                "retry with api-abcdefghij-klmnopqrst",
                "retry with api-[redacted]",
            ),
            (
                "retry with token abcdefghijklmnopqrst/uvwxyz",
                "retry with token [redacted]",
            ),
            (
                "retry with token abcdefghijklmnopqrst+uvwxyz",
                "retry with token [redacted]",
            ),
            (
                "retry with token \u200b" + "a" * 14 + "123456",
                "retry with token \u200b[redacted]",
            ),
        ),
    )
    def test_embedded_generic_credential_text_is_sensitive_and_redacted(
        self,
        value: str,
        expected: str,
    ) -> None:
        """A generic opaque token cannot evade detection inside diagnostic prose."""

        assert is_sensitive_value(value) is True
        assert redact_sensitive_text(value) == expected

    @pytest.mark.parametrize(
        "value",
        (
            "Implement token internationalization",
            "Implement token compartmentalization",
            "Discuss token hyperparameterization",
        ),
    )
    def test_long_semantic_token_words_remain_non_sensitive(self, value: str) -> None:
        """Long natural-language words must survive replay unchanged."""
        assert is_sensitive_value(value) is False
        assert redact_sensitive_text(value) == value

    def test_diagnostic_text_with_embedded_secret_remains_sensitive(self) -> None:
        """Global credential detection remains fail-closed for diagnostics."""
        text = "command: deploy --api-key sk-live-SECRET123"
        assert is_sensitive_value(text) is True
        assert redact_sensitive_text(text) == "command: deploy --api-key [redacted]"

    def test_embedded_stripe_and_pem_private_key_values_are_redacted(self) -> None:
        """Credential shapes beyond prefix-only forms cannot reach diagnostics."""
        stripe = "sk_live_" + "a" * 24
        restricted = "rk_live_" + "b" * 24
        restricted_test = "rk_test_" + "c" * 24
        pem = "-----BEGIN PRIVATE KEY-----\nprivate material\n-----END PRIVATE KEY-----"

        assert is_sensitive_value(f"retry with {stripe}") is True
        assert redact_sensitive_text(f"retry with {stripe}") == "retry with [redacted]"
        assert is_sensitive_value(f"retry with {restricted}") is True
        assert redact_sensitive_text(f"retry with {restricted}") == "retry with [redacted]"
        assert is_sensitive_value(f"retry with {restricted_test}") is True
        assert redact_sensitive_text(f"retry with {restricted_test}") == "retry with [redacted]"
        assert is_sensitive_value(f"retry with {pem}") is True
        assert redact_sensitive_text(f"retry with {pem}") == "retry with [redacted]"

    def test_compact_assignment_with_embedded_secret_remains_sensitive(self) -> None:
        """Identity/configuration consumers must reject compact secret values."""
        value = "key=" + "AIza" + "A" * 35
        assert is_sensitive_value(value) is True
        assert redact_sensitive_text(value) == "key=[redacted]"


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

    def test_sanitize_whitespace_prefixed_sensitive_value(self) -> None:
        """The full credential never survives a whitespace-prefixed value."""
        secret = "ghp_" + "a" * 36
        result = sanitize_for_logging({"some_field": f" \t{secret}"})
        assert secret not in str(result)


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
