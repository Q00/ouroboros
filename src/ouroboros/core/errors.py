"""Error hierarchy for Ouroboros.

This module defines the exception hierarchy for Ouroboros. These exceptions
are used for unexpected errors (programming bugs) and as error types in
Result for expected failures.

Exception Hierarchy:
    OuroborosError (base)
    ├── ProviderError     - LLM provider failures (rate limits, API errors)
    ├── ConfigError       - Configuration and credentials issues
    ├── PersistenceError  - Database and storage issues
    └── ValidationError   - Schema and data validation failures
"""

from typing import Any


class OuroborosError(Exception):
    """Base exception for all Ouroboros errors.

    All Ouroboros-specific exceptions inherit from this class.
    This allows catching all Ouroboros errors with a single except clause.

    Attributes:
        message: Human-readable error description.
        details: Optional dict with additional context.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        """Initialize the error.

        Args:
            message: Human-readable error description.
            details: Optional dict with additional context about the error.
        """
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        """Return string representation of the error."""
        if self.details:
            return f"{self.message} (details: {self.details})"
        return self.message


class ProviderError(OuroborosError):
    """Error from LLM provider operations.

    Raised when LLM provider calls fail (rate limits, API errors, timeouts).
    Can be converted from provider-specific exceptions.

    Attributes:
        provider: Name of the provider (e.g., "openai", "anthropic").
        status_code: HTTP status code if applicable.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialize provider error.

        Args:
            message: Human-readable error description.
            provider: Name of the LLM provider.
            status_code: HTTP status code if applicable.
            details: Optional dict with additional context.
        """
        super().__init__(message, details)
        self.provider = provider
        self.status_code = status_code

    @classmethod
    def from_exception(
        cls, exc: Exception, *, provider: str | None = None
    ) -> ProviderError:
        """Create ProviderError from a provider exception.

        Args:
            exc: The original exception from the provider.
            provider: Name of the LLM provider.

        Returns:
            A ProviderError wrapping the original exception with __cause__ set
            for proper traceback preservation.
        """
        status_code = getattr(exc, "status_code", None)
        error = cls(
            str(exc),
            provider=provider,
            status_code=status_code,
            details={"original_exception": type(exc).__name__},
        )
        error.__cause__ = exc  # Preserve original traceback
        return error


class ConfigError(OuroborosError):
    """Error from configuration operations.

    Raised when configuration loading, parsing, or validation fails.

    Attributes:
        config_key: The configuration key that caused the error.
        config_file: Path to the config file if applicable.
    """

    def __init__(
        self,
        message: str,
        *,
        config_key: str | None = None,
        config_file: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialize config error.

        Args:
            message: Human-readable error description.
            config_key: The configuration key that caused the error.
            config_file: Path to the config file if applicable.
            details: Optional dict with additional context.
        """
        super().__init__(message, details)
        self.config_key = config_key
        self.config_file = config_file


class PersistenceError(OuroborosError):
    """Error from database and storage operations.

    Raised when database queries, event storage, or checkpoint operations fail.

    Attributes:
        operation: The operation that failed (e.g., "insert", "query").
        table: The database table involved if applicable.
    """

    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        table: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialize persistence error.

        Args:
            message: Human-readable error description.
            operation: The operation that failed.
            table: The database table involved.
            details: Optional dict with additional context.
        """
        super().__init__(message, details)
        self.operation = operation
        self.table = table


class ValidationError(OuroborosError):
    """Error from data validation operations.

    Raised when input data fails schema validation or business rule checks.

    Attributes:
        field: The field that failed validation.
        value: The invalid value if safe to include.

    Security Note:
        Use safe_value property instead of value when logging to avoid
        exposing sensitive data like API keys or credentials.
    """

    # Fields that should never have their values exposed
    _SENSITIVE_FIELDS = frozenset({
        "password", "api_key", "secret", "token", "credential",
        "auth", "key", "private", "apikey", "api-key",
    })

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        value: Any | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialize validation error.

        Args:
            message: Human-readable error description.
            field: The field that failed validation.
            value: The invalid value (only include if safe).
            details: Optional dict with additional context.
        """
        super().__init__(message, details)
        self.field = field
        self.value = value

    @property
    def safe_value(self) -> str:
        """Return a safe representation of the value for logging.

        Masks potentially sensitive values based on field name or value type.
        Always use this instead of value when logging or displaying errors.

        Returns:
            A safe string representation: masked for sensitive fields,
            truncated for long values, or type info for complex objects.
        """
        if self.value is None:
            return "<None>"

        # Check if field name suggests sensitive data
        if self.field:
            field_lower = self.field.lower()
            if any(sensitive in field_lower for sensitive in self._SENSITIVE_FIELDS):
                return "<REDACTED>"

        # Check if value looks like a secret (starts with common prefixes)
        if isinstance(self.value, str):
            value_str = self.value
            secret_prefixes = ("sk-", "pk-", "api-", "bearer ", "token ", "secret_")
            if any(value_str.lower().startswith(p) for p in secret_prefixes):
                return "<REDACTED>"
            # Truncate long strings
            if len(value_str) > 50:
                return f"{value_str[:20]}...({len(value_str)} chars)"
            return repr(value_str)

        # For other types, show type info
        return f"<{type(self.value).__name__}>"

    def __str__(self) -> str:
        """Return string representation using safe_value."""
        base = self.message
        if self.field:
            base = f"{base} (field: {self.field}, value: {self.safe_value})"
        if self.details:
            base = f"{base} (details: {self.details})"
        return base
