"""Security utilities for Ouroboros.

This module provides security-related utilities including:
- API key validation and masking
- Input sanitization
- Size limits for external inputs

Security Level: MEDIUM
- API keys are masked in logs and error messages
- Basic format validation for API keys
- Size limits to prevent DoS attacks
"""

import re
from typing import Any

# Maximum sizes for external inputs (DoS prevention)
MAX_INITIAL_CONTEXT_LENGTH = 50_000  # 50KB for initial interview context
MAX_USER_RESPONSE_LENGTH = 10_000  # 10KB for interview responses
MAX_SEED_FILE_SIZE = 1_000_000  # 1MB for seed YAML files
MAX_LLM_RESPONSE_LENGTH = 100_000  # 100KB for LLM responses

# API key patterns for validation (not exhaustive, basic format check)
_API_KEY_PATTERNS: dict[str, re.Pattern[str]] = {
    "openai": re.compile(r"^sk-[a-zA-Z0-9_-]{20,}$"),
    "anthropic": re.compile(r"^sk-ant-[a-zA-Z0-9_-]{20,}$"),
    "openrouter": re.compile(r"^sk-or-[a-zA-Z0-9_-]{20,}$"),
    "google": re.compile(r"^AIza[a-zA-Z0-9_-]{35}$"),
}

# Sensitive field names that should be masked
SENSITIVE_FIELD_NAMES = frozenset(
    {
        "password",
        "api_key",
        "apikey",
        "api-key",
        "secret",
        "token",
        "credential",
        "auth",
        "key",
        "private",
        "bearer",
        "authorization",
    }
)

# Sensitive value prefixes that indicate secrets
SENSITIVE_PREFIXES = (
    "sk-",
    "pk-",
    "api-",
    "bearer ",
    "token ",
    "secret_",
    "AIza",
)


def mask_api_key(api_key: str, visible_chars: int = 4) -> str:
    """Mask an API key for safe logging/display.

    Shows only the last few characters to help identify which key is being used.

    Args:
        api_key: The API key to mask.
        visible_chars: Number of characters to show at the end (default 4).

    Returns:
        Masked API key like "sk-...xxxx" or "<empty>" if key is empty.

    Example:
        >>> mask_api_key("sk-1234567890abcdef")
        'sk-...cdef'
    """
    if not api_key:
        return "<empty>"

    if len(api_key) <= visible_chars + 4:
        # Key is too short to meaningfully mask
        return "*" * len(api_key)

    # Show prefix (like "sk-") and last few chars
    if "-" in api_key[:6]:
        prefix_end = api_key.index("-") + 1
        prefix = api_key[:prefix_end]
        return f"{prefix}...{api_key[-visible_chars:]}"

    return f"...{api_key[-visible_chars:]}"


def validate_api_key_format(api_key: str, provider: str | None = None) -> bool:
    """Validate API key format (basic check, not authorization).

    This performs a basic format validation. It does NOT verify that the key
    is actually valid or authorized - that requires an API call.

    Args:
        api_key: The API key to validate.
        provider: Optional provider name for specific validation.

    Returns:
        True if the key has a valid format.

    Note:
        This is a security convenience check, not a comprehensive validation.
        Keys may be properly formatted but still invalid/expired.
    """
    if not api_key or len(api_key) < 10:
        return False

    # If provider specified, use specific pattern
    if provider and provider.lower() in _API_KEY_PATTERNS:
        pattern = _API_KEY_PATTERNS[provider.lower()]
        return bool(pattern.match(api_key))

    # Generic validation: must look like an API key
    # Should have letters, numbers, possibly dashes/underscores
    if not re.match(r"^[a-zA-Z0-9_-]{10,}$", api_key):
        # Check if it's a prefixed key
        return any(pattern.match(api_key) for pattern in _API_KEY_PATTERNS.values())

    return True


def is_sensitive_field(field_name: str) -> bool:
    """Check if a field name indicates sensitive data.

    Args:
        field_name: The field name to check.

    Returns:
        True if the field likely contains sensitive data.
    """
    if not field_name:
        return False

    field_lower = field_name.lower()
    return any(sensitive in field_lower for sensitive in SENSITIVE_FIELD_NAMES)


def is_sensitive_value(value: Any) -> bool:
    """Check if a value looks like sensitive data.

    Args:
        value: The value to check.

    Returns:
        True if the value appears to be sensitive (API key, token, etc).
    """
    if not isinstance(value, str):
        return False

    value_lower = value.lower()
    return any(value_lower.startswith(prefix.lower()) for prefix in SENSITIVE_PREFIXES)


def mask_sensitive_value(value: Any, field_name: str | None = None) -> str:
    """Mask a potentially sensitive value for safe logging.

    Args:
        value: The value to potentially mask.
        field_name: Optional field name for context.

    Returns:
        Masked string if sensitive, otherwise string representation.
    """
    if value is None:
        return "<None>"

    # Check if field name indicates sensitivity
    if field_name and is_sensitive_field(field_name):
        return "<REDACTED>"

    # Check if value looks sensitive
    if isinstance(value, str):
        if is_sensitive_value(value):
            return mask_api_key(value)

        # Truncate long strings
        if len(value) > 100:
            return f"{value[:50]}...({len(value)} chars)"

        return value

    # For other types, show type info
    if isinstance(value, (dict, list)):
        return f"<{type(value).__name__} with {len(value)} items>"

    return str(value)


def sanitize_for_logging(data: dict[str, Any]) -> dict[str, Any]:
    """Create a copy of data with sensitive values masked.

    Use this before logging dictionaries that might contain sensitive data.

    Args:
        data: Dictionary that might contain sensitive data.

    Returns:
        New dictionary with sensitive values masked.

    Example:
        >>> sanitize_for_logging({"api_key": "sk-secret123", "name": "test"})
        {'api_key': '<REDACTED>', 'name': 'test'}
    """
    result = {}
    for key, value in data.items():
        if is_sensitive_field(key):
            result[key] = "<REDACTED>"
        elif isinstance(value, str) and is_sensitive_value(value):
            result[key] = mask_api_key(value)
        elif isinstance(value, dict):
            result[key] = sanitize_for_logging(value)
        else:
            result[key] = value
    return result


def truncate_input(text: str, max_length: int, suffix: str = "...") -> str:
    """Truncate text to maximum length with suffix.

    Args:
        text: Text to truncate.
        max_length: Maximum length including suffix.
        suffix: Suffix to add if truncated (default "...").

    Returns:
        Truncated text or original if within limit.
    """
    if len(text) <= max_length:
        return text

    return text[: max_length - len(suffix)] + suffix


class InputValidator:
    """Validator for external inputs with size limits.

    Provides validation methods for different types of external inputs
    to prevent DoS attacks and ensure data quality.
    """

    @staticmethod
    def validate_initial_context(context: str) -> tuple[bool, str]:
        """Validate initial interview context.

        Args:
            context: The initial context string.

        Returns:
            Tuple of (is_valid, error_message). error_message is empty if valid.
        """
        if not context:
            return False, "Initial context cannot be empty"

        stripped = context.strip()
        if not stripped:
            return False, "Initial context cannot be only whitespace"

        if len(stripped) > MAX_INITIAL_CONTEXT_LENGTH:
            return (
                False,
                f"Initial context exceeds maximum length ({MAX_INITIAL_CONTEXT_LENGTH} chars)",
            )

        return True, ""

    @staticmethod
    def validate_user_response(response: str) -> tuple[bool, str]:
        """Validate user response in interview.

        Args:
            response: The user's response string.

        Returns:
            Tuple of (is_valid, error_message). error_message is empty if valid.
        """
        if not response:
            return False, "Response cannot be empty"

        stripped = response.strip()
        if not stripped:
            return False, "Response cannot be only whitespace"

        if len(stripped) > MAX_USER_RESPONSE_LENGTH:
            return False, f"Response exceeds maximum length ({MAX_USER_RESPONSE_LENGTH} chars)"

        return True, ""

    @staticmethod
    def validate_seed_file_size(file_size: int) -> tuple[bool, str]:
        """Validate seed file size.

        Args:
            file_size: Size of the seed file in bytes.

        Returns:
            Tuple of (is_valid, error_message). error_message is empty if valid.
        """
        if file_size <= 0:
            return False, "Seed file is empty"

        if file_size > MAX_SEED_FILE_SIZE:
            return False, f"Seed file exceeds maximum size ({MAX_SEED_FILE_SIZE // 1024}KB)"

        return True, ""

    @staticmethod
    def validate_llm_response(response: str) -> tuple[bool, str]:
        """Validate LLM response length.

        Args:
            response: The LLM response content.

        Returns:
            Tuple of (is_valid, error_message). error_message is empty if valid.
        """
        if not response:
            return True, ""  # Empty response is valid (model may return empty)

        if len(response) > MAX_LLM_RESPONSE_LENGTH:
            return False, f"LLM response exceeds maximum length ({MAX_LLM_RESPONSE_LENGTH} chars)"

        return True, ""
