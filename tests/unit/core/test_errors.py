"""Unit tests for ouroboros.core.errors module."""

import pytest

from ouroboros.core.errors import (
    ConfigError,
    OuroborosError,
    PersistenceError,
    ProviderError,
    ValidationError,
)


class TestOuroborosError:
    """Test OuroborosError base class."""

    def test_ouroboros_error_is_exception(self) -> None:
        """OuroborosError inherits from Exception."""
        error = OuroborosError("test error")
        assert isinstance(error, Exception)

    def test_ouroboros_error_stores_message(self) -> None:
        """OuroborosError stores the message."""
        error = OuroborosError("test message")
        assert error.message == "test message"
        assert str(error) == "test message"

    def test_ouroboros_error_stores_details(self) -> None:
        """OuroborosError stores optional details."""
        details = {"key": "value", "count": 42}
        error = OuroborosError("test", details=details)
        assert error.details == details

    def test_ouroboros_error_default_empty_details(self) -> None:
        """OuroborosError defaults to empty details."""
        error = OuroborosError("test")
        assert error.details == {}

    def test_ouroboros_error_str_with_details(self) -> None:
        """OuroborosError string representation includes details."""
        error = OuroborosError("test", details={"key": "value"})
        assert "test" in str(error)
        assert "key" in str(error)


class TestProviderError:
    """Test ProviderError for LLM provider failures."""

    def test_provider_error_inherits_from_base(self) -> None:
        """ProviderError inherits from OuroborosError."""
        error = ProviderError("api failed")
        assert isinstance(error, OuroborosError)
        assert isinstance(error, Exception)

    def test_provider_error_stores_provider(self) -> None:
        """ProviderError stores the provider name."""
        error = ProviderError("rate limited", provider="openai")
        assert error.provider == "openai"

    def test_provider_error_stores_status_code(self) -> None:
        """ProviderError stores the HTTP status code."""
        error = ProviderError("not found", status_code=404)
        assert error.status_code == 404

    def test_provider_error_from_exception(self) -> None:
        """ProviderError.from_exception wraps another exception."""

        class MockAPIError(Exception):
            status_code = 429

        original = MockAPIError("too many requests")
        error = ProviderError.from_exception(original, provider="anthropic")

        assert error.provider == "anthropic"
        assert error.status_code == 429
        assert "too many requests" in str(error)
        assert error.details["original_exception"] == "MockAPIError"

    def test_provider_error_from_exception_without_status_code(self) -> None:
        """ProviderError.from_exception handles exceptions without status_code."""
        original = ValueError("generic error")
        error = ProviderError.from_exception(original)

        assert error.status_code is None
        assert "generic error" in str(error)


class TestConfigError:
    """Test ConfigError for configuration issues."""

    def test_config_error_inherits_from_base(self) -> None:
        """ConfigError inherits from OuroborosError."""
        error = ConfigError("missing key")
        assert isinstance(error, OuroborosError)

    def test_config_error_stores_config_key(self) -> None:
        """ConfigError stores the config key."""
        error = ConfigError("invalid value", config_key="api_key")
        assert error.config_key == "api_key"

    def test_config_error_stores_config_file(self) -> None:
        """ConfigError stores the config file path."""
        error = ConfigError("parse error", config_file="/path/to/config.yaml")
        assert error.config_file == "/path/to/config.yaml"


class TestPersistenceError:
    """Test PersistenceError for database issues."""

    def test_persistence_error_inherits_from_base(self) -> None:
        """PersistenceError inherits from OuroborosError."""
        error = PersistenceError("connection failed")
        assert isinstance(error, OuroborosError)

    def test_persistence_error_stores_operation(self) -> None:
        """PersistenceError stores the operation."""
        error = PersistenceError("failed", operation="insert")
        assert error.operation == "insert"

    def test_persistence_error_stores_table(self) -> None:
        """PersistenceError stores the table name."""
        error = PersistenceError("failed", table="events")
        assert error.table == "events"


class TestValidationError:
    """Test ValidationError for schema failures."""

    def test_validation_error_inherits_from_base(self) -> None:
        """ValidationError inherits from OuroborosError."""
        error = ValidationError("invalid input")
        assert isinstance(error, OuroborosError)

    def test_validation_error_stores_field(self) -> None:
        """ValidationError stores the field name."""
        error = ValidationError("must be positive", field="count")
        assert error.field == "count"

    def test_validation_error_stores_value(self) -> None:
        """ValidationError stores the invalid value."""
        error = ValidationError("invalid", field="status", value="unknown")
        assert error.value == "unknown"


class TestErrorHierarchy:
    """Test that all errors form a proper hierarchy."""

    def test_all_errors_catchable_as_ouroboros_error(self) -> None:
        """All error types can be caught as OuroborosError."""
        errors = [
            ProviderError("test"),
            ConfigError("test"),
            PersistenceError("test"),
            ValidationError("test"),
        ]

        for error in errors:
            with pytest.raises(OuroborosError):
                raise error

    def test_all_errors_catchable_as_exception(self) -> None:
        """All error types can be caught as Exception."""
        errors = [
            OuroborosError("test"),
            ProviderError("test"),
            ConfigError("test"),
            PersistenceError("test"),
            ValidationError("test"),
        ]

        for error in errors:
            with pytest.raises(Exception):
                raise error
