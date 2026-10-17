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

from pathlib import Path
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
        "credentials",
        "auth",
        "key",
        "private",
        "passwd",
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

_CREDENTIAL_SHAPE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # GitHub PAT/app/OAuth tokens and other opaque provider tokens.
    re.compile(r"^gh[oprsu]_"),
    re.compile(r"^github_pat_"),
    re.compile(r"^sk-"),
    re.compile(r"^sk_(?:live|test)_"),
    re.compile(r"^rk_(?:live|test)_"),
    re.compile(r"^pk-"),
    re.compile(r"^api-"),
    re.compile(r"^xox[bpa]-"),
    re.compile(r"^xapp-"),
    re.compile(r"^npm_"),
    re.compile(r"^pypi-"),
    re.compile(r"^glpat-"),
    re.compile(r"^hf_"),
    # SendGrid API keys and HashiCorp Vault service tokens.
    re.compile(r"^SG\.[A-Za-z0-9]{22}\.[A-Za-z0-9]{43}$"),
    re.compile(r"^hvs\.[A-Za-z0-9_-]{16,}$"),
    # Google API keys, AWS access-key IDs, and JWT-shaped bearer values.
    re.compile(r"^AIza[A-Za-z0-9_-]{35,}$"),
    re.compile(r"^AKIA[0-9A-Z]{16}$"),
    re.compile(r"^ASIA[0-9A-Z]{16}$"),
    re.compile(r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$"),
)

# The stable authority grammar permits ``-`` and ``_`` inside a descriptor,
# so anchored provider patterns must also be checked at every non-alphanumeric
# boundary.  Otherwise ``runtime:prod-ghp_...`` and ``runtime:prod-hvs....``
# would evade the segment-based checks below.
_EMBEDDED_CREDENTIAL_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![A-Za-z0-9])gh[oprsu]_"),
    re.compile(r"(?<![A-Za-z0-9])github_pat_"),
    re.compile(r"(?<![A-Za-z0-9])sk-"),
    re.compile(r"(?<![A-Za-z0-9])sk_(?:live|test)_"),
    re.compile(r"(?<![A-Za-z0-9])rk_(?:live|test)_"),
    re.compile(r"(?<![A-Za-z0-9])pk-"),
    re.compile(r"(?<![A-Za-z0-9])api-"),
    re.compile(r"(?<![A-Za-z0-9])xox[bpa]-"),
    re.compile(r"(?<![A-Za-z0-9])xapp-"),
    re.compile(r"(?<![A-Za-z0-9])npm_"),
    re.compile(r"(?<![A-Za-z0-9])pypi-"),
    re.compile(r"(?<![A-Za-z0-9])glpat-"),
    re.compile(r"(?<![A-Za-z0-9])hf_"),
    re.compile(r"(?<![A-Za-z0-9])SG\.[A-Za-z0-9]{22}\.[A-Za-z0-9]{43}"),
    re.compile(r"(?<![A-Za-z0-9])hvs\.[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?<![A-Za-z0-9])AIza[A-Za-z0-9_-]{35,}"),
    re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}"),
    re.compile(r"(?<![A-Za-z0-9])ASIA[0-9A-Z]{16}"),
    re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
)

_CREDENTIAL_NAMESPACE_LABELS = frozenset(
    (
        *SENSITIVE_FIELD_NAMES,
        "access_token",
        "client_secret",
    )
)
_COMPACT_CREDENTIAL_NAMESPACE_LABELS = frozenset(
    {
        "apikey",
        "accesstoken",
        "clientsecret",
        "accesskey",
        "privatekey",
        "password",
        "passwd",
    }
)
_COMPACT_CREDENTIAL_LABELS_WITH_VALUE_DIGITS = frozenset(
    {"secret", "token", "credential", "credentials", "authorization", "key", "private", "bearer"}
)
_COMPACT_AUTH_CREDENTIAL_PREFIXES = (
    "authbearer",
    "authcredential",
    "authkey",
    "authopaque",
    "authpassword",
    "authsecret",
    "authtoken",
    "authvalue",
)
_SAFE_COMPACT_AUTHORITY_IDENTIFIERS = frozenset({"keycloak", "tokenizer", "privateer"})
_SAFE_CREDENTIAL_LABEL_SUFFIXES = frozenset({"budget", "default", "id", "name", "plane", "scope"})

_CREDENTIAL_COMPOUND_PREFIX = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(?:password|api[-_]?key|apikey|secret|access[-_]?token|client[-_]?secret|"
    r"token|credential|credentials|auth|authorization|key|private|bearer|passwd)"
    r"[-_]"
    # A known-safe descriptor suffix (``token-budget`` or ``auth-plane``)
    # remains usable as metadata. Any other suffix is treated as an opaque
    # value, including embedded provider prefixes such as ``api-key-sk-...``.
    r"(?!(?:budget|default|id|name|plane|scope)(?:$|[-_:/\.]))"
)

# Authority identities are short typed descriptors, not opaque runtime values.
# Bound both their namespace and their label structure so an allowlisted prefix
# cannot be used as a container for an unenumerated provider token.
_STABLE_AUTHORITY_NAMESPACES = (
    "authority",
    "execution",
    "workspace",
    "process",
    "project",
    "runtime",
    "session",
    "system",
    "tenant",
    "default",
)
_STABLE_AUTHORITY_SEPARATORS = frozenset("-_:/.")
_STABLE_AUTHORITY_LABELS = frozenset(
    {
        "a",
        "auth",
        "b",
        "budget",
        "claude",
        "cli",
        "code",
        "codex",
        "copilot",
        "default",
        "dsh",
        "gemini",
        "gjc",
        "goose",
        "hermes",
        "keycloak",
        "kiro",
        "litellm",
        "local",
        "mcp",
        "opencode",
        "ourocode",
        "pi",
        "plane",
        "primary",
        "privateer",
        "project",
        "secondary",
        "shared",
        "token",
        "tokenizer",
        "worker",
        "zcode",
    }
)
_MAX_STABLE_AUTHORITY_SEGMENTS = 4
_MAX_STABLE_AUTHORITY_SEGMENT_CHARS = 32
_MAX_STABLE_AUTHORITY_ORDINAL_CHARS = 10


def _has_stable_authority_grammar(value: str) -> bool:
    """Validate a bounded typed descriptor with one linear scan."""

    namespace = next(
        (candidate for candidate in _STABLE_AUTHORITY_NAMESPACES if value.startswith(candidate)),
        None,
    )
    if namespace is None:
        return False
    tail = value[len(namespace) :]
    if not tail:
        return True
    if tail[0] not in _STABLE_AUTHORITY_SEPARATORS:
        return False

    segments: list[str] = []
    segment_chars: list[str] = []
    for character in tail[1:]:
        if character in _STABLE_AUTHORITY_SEPARATORS:
            if not segment_chars:
                return False
            segments.append("".join(segment_chars))
            if len(segments) >= _MAX_STABLE_AUTHORITY_SEGMENTS:
                return False
            segment_chars = []
            continue
        if not ("a" <= character <= "z" or "0" <= character <= "9"):
            return False
        segment_chars.append(character)
        if len(segment_chars) > _MAX_STABLE_AUTHORITY_SEGMENT_CHARS:
            return False
    if not segment_chars:
        return False
    segments.append("".join(segment_chars))
    return all(
        segment in _STABLE_AUTHORITY_LABELS
        or (segment.isdecimal() and len(segment) <= _MAX_STABLE_AUTHORITY_ORDINAL_CHARS)
        for segment in segments
    )


def _is_credential_namespace_label(value: str) -> bool:
    """Return whether a namespace segment labels a credential-bearing value."""

    label = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value.strip())
    label = label.lower().replace("-", "_")
    compact_label = label.replace("_", "")
    if compact_label.startswith(_COMPACT_AUTH_CREDENTIAL_PREFIXES):
        return True
    # Providers commonly omit separators in labels (``clientsecret``,
    # ``accesstoken``, ``privatekey``).  Treat those aliases like their
    # delimiter-bearing forms before any opaque payload can be serialized.
    for compact_credential_label in _COMPACT_CREDENTIAL_NAMESPACE_LABELS:
        if compact_label == compact_credential_label:
            return True
        # ``compact_label`` has separators removed, so a direct value suffix
        # must be checked without looking for an underscore.  For example,
        # ``accesstokenabc123`` and ``password123`` are credential-labelled
        # values even though no delimiter separates the label from its value.
        if compact_label.startswith(compact_credential_label):
            suffix = compact_label[len(compact_credential_label) :]
            safe_suffixes = {
                safe_suffix.replace("_", "") for safe_suffix in _SAFE_CREDENTIAL_LABEL_SUFFIXES
            }
            return suffix not in safe_suffixes
    # Generic one-word labels overlap with ordinary identifiers (``keycloak``
    # and ``tokenizer``). Treat them as compact credentials only when the
    # suffix has the stronger opaque-value signal of a digit.
    for compact_credential_label in _COMPACT_CREDENTIAL_LABELS_WITH_VALUE_DIGITS:
        if compact_label.startswith(compact_credential_label):
            suffix = compact_label[len(compact_credential_label) :]
            if suffix and any(character.isdigit() for character in suffix):
                return True
    if label in _CREDENTIAL_NAMESPACE_LABELS or label.endswith(
        ("_key", "_token", "_secret", "_credential")
    ):
        return True
    for credential_label in _CREDENTIAL_NAMESPACE_LABELS:
        prefix = f"{credential_label}_"
        if label.startswith(prefix):
            suffix = label[len(prefix) :]
            return suffix not in _SAFE_CREDENTIAL_LABEL_SUFFIXES
    return False


def _has_strict_compact_authority_label(value: str) -> bool:
    """Reject alphabetic compact credential labels at the authority boundary.

    Logging detection intentionally keeps a narrower heuristic to avoid
    redacting ordinary words. Authority identities are a fail-closed contract,
    so generic compact labels are rejected there unless they are explicitly
    recognized ordinary identifiers or safe metadata suffixes.
    """

    safe_suffixes = {
        safe_suffix.replace("_", "") for safe_suffix in _SAFE_CREDENTIAL_LABEL_SUFFIXES
    }
    for part in re.split(r"[:/.]", value):
        label = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", part.strip()).lower()
        compact_label = label.replace("-", "").replace("_", "")
        if compact_label in _SAFE_COMPACT_AUTHORITY_IDENTIFIERS:
            continue
        for credential_label in _COMPACT_CREDENTIAL_LABELS_WITH_VALUE_DIGITS:
            if compact_label.startswith(credential_label):
                suffix = compact_label[len(credential_label) :]
                if suffix and suffix not in safe_suffixes:
                    return True
    return False


def _normalize_credential_shape(value: str) -> tuple[str | None, bool]:
    """Normalize a credential candidate and flag values that must fail closed."""

    if not isinstance(value, str):
        return None, False
    try:
        # Never call a caller-controlled override (``strip``, ``__str__``, ...).
        normalized = str.strip(str.__str__(value))
    except Exception:
        return None, True
    if type(normalized) is not str:
        return None, True
    return normalized, False


def _is_opaque_credential_shape(normalized: str) -> bool:
    """Detect provider tokens and opaque bearer values without namespace labels."""

    if _CREDENTIAL_COMPOUND_PREFIX.search(normalized):
        return True
    if any(pattern.search(normalized) for pattern in _EMBEDDED_CREDENTIAL_SHAPES):
        return True
    candidates = [normalized]
    # Preserve the opaque payload after a stable namespace delimiter so a
    # descriptor such as ``runtime:SG.<id>.<secret>`` cannot hide a credential.
    candidates.extend(
        normalized.split(delimiter, 1)[1]
        for delimiter in (":", "/", ".")
        if delimiter in normalized
    )
    candidates.extend(part for part in re.split(r"[:/.]", normalized) if part)
    for candidate in candidates:
        lowered = candidate.lower()
        if lowered.startswith(("bearer ", "token ", "secret_")):
            return True
        if any(pattern.match(candidate) for pattern in _CREDENTIAL_SHAPE_PATTERNS):
            return True
    return False


_STRUCTURED_EVENT_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_SAFE_CREDENTIAL_NAMED_STRUCTURED_EVENTS = frozenset(
    {
        "context.token_count.failed",
        "mcp.auth.invalid_api_key",
        "mcp.auth.token_valid",
    }
)


def is_safe_structured_event_identifier(value: str) -> bool:
    """Return whether an event name is structured and contains no opaque token."""
    normalized, fail_closed = _normalize_credential_shape(value)
    if fail_closed or not normalized or not _STRUCTURED_EVENT_IDENTIFIER_RE.fullmatch(normalized):
        return False
    if normalized in _SAFE_CREDENTIAL_NAMED_STRUCTURED_EVENTS:
        return True
    if is_credential_shaped(normalized):
        return False
    candidates = (normalized, *(part for part in normalized.split(".") if part))
    return not any(
        pattern.match(candidate)
        for candidate in candidates
        for pattern in _CREDENTIAL_SHAPE_PATTERNS
    )


def is_opaque_credential_shaped(value: str) -> bool:
    """Return whether a string has an opaque credential shape.

    Unlike :func:`is_credential_shaped`, this excludes semantic namespace
    labels such as ``auth.token_valid`` so structured event identifiers can
    remain exact without allowing JWTs or provider tokens through.
    """

    normalized, fail_closed = _normalize_credential_shape(value)
    if fail_closed:
        return True
    return bool(normalized) and _is_opaque_credential_shape(normalized)


def is_credential_shaped(value: str) -> bool:
    """Return whether a string matches a high-confidence credential shape.

    This is deliberately shape-based rather than a validity check. It protects
    canonical metadata boundaries from copying opaque credentials while still
    allowing ordinary route and authority identifiers through.

    ``str`` subclasses (notably :class:`enum.StrEnum` members, used widely in
    this codebase) are inspected on their string content rather than waved
    through: because a ``False`` result here means "safe to copy/log", a strict
    ``type(...) is str`` test would let a subclass carrying a live credential
    bypass this guard entirely. Subclass method overrides are never trusted --
    the value is renormalized through the built-in ``str`` implementations, and
    a value that cannot be normalized is treated as credential-shaped.
    """

    normalized, fail_closed = _normalize_credential_shape(value)
    if fail_closed:
        return True
    if not normalized:
        return False
    if _is_opaque_credential_shape(normalized):
        return True
    namespace_parts = [part for part in re.split(r"[:/.]", normalized) if part]
    return any(_is_credential_namespace_label(part) for part in namespace_parts)


def is_stable_authority_identity(value: str) -> bool:
    """Return whether ``value`` is an allowlisted non-secret descriptor.

    Route authority identities intentionally use a small, stable namespace
    vocabulary (``runtime:claude``, ``session-a``, ``authority-default``).  A
    free-form opaque token is not accepted merely because it is not yet known
    to the credential denylist; it must first match this descriptor grammar and
    then pass the shared credential-shape guard.

    ``str`` subclasses are evaluated on their string content so that a
    :class:`enum.StrEnum` member cannot skip the credential-shape guard below.
    Retaining plain built-in ``str`` values at live effect boundaries remains
    the caller's responsibility.
    """

    # Unlike ``is_credential_shaped``, a ``False`` result here *rejects* the
    # value, so declining a caller-controlled ``str`` subclass is fail-closed
    # rather than a bypass. Subclasses are refused outright because an accepted
    # descriptor may be retained by a caller, and a subclass can override
    # ``__eq__``/``__hash__`` to behave differently after this check. Callers
    # that hold a ``StrEnum`` normalize to a built-in ``str`` before asking.
    if type(value) is not str:
        return False
    try:
        normalized = str.strip(str.__str__(value))
    except Exception:
        return False
    return (
        _has_stable_authority_grammar(normalized)
        and not is_credential_shaped(normalized)
        and not _has_strict_compact_authority_label(normalized)
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


def is_sensitive_field(field_name: Any) -> bool:
    """Check if a field name indicates sensitive data.

    Args:
        field_name: The field name to check.

    Returns:
        True if the field likely contains sensitive data.
    """
    if not isinstance(field_name, str):
        return False

    try:
        normalized = str.__str__(field_name)
    except Exception:
        # A string-like key that cannot be safely normalized must not be
        # allowed to bypass field-name redaction.
        return True

    field_lower = str.lower(normalized)
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

    # Normalize to a genuine built-in ``str`` before calling ``.lower()`` so
    # that a hostile ``str`` subclass (e.g. one overriding ``lower``) cannot
    # suppress detection or cause side effects.
    try:
        normalized = str.__str__(value)
    except Exception:
        # If we cannot normalize the subclass, fail closed — treat as sensitive.
        return True
    value_lower = str.lower(normalized)
    return is_credential_shaped(value) or any(
        value_lower.startswith(prefix.lower()) for prefix in SENSITIVE_PREFIXES
    )


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

    # Normalize string subclasses before any indexing, slicing, or return so
    # caller-controlled overrides cannot disclose the original credential.
    if isinstance(value, str):
        try:
            normalized = str.__str__(value)
        except Exception:
            return "<REDACTED>"

        if is_sensitive_value(value):
            return mask_api_key(normalized)

        # Truncate long strings
        if len(normalized) > 100:
            return f"{normalized[:50]}...({len(normalized)} chars)"

        return normalized

    # For other types, show type info
    if isinstance(value, (dict, list)):
        return f"<{type(value).__name__} with {len(value)} items>"

    return str(value)


_MAX_LOGGING_SANITIZATION_DEPTH = 64


def _sanitize_logging_sequence(
    value: list[Any] | tuple[Any, ...],
    active_containers: set[int],
    depth: int,
) -> list[Any] | tuple[Any, ...] | str:
    """Copy a built-in sequence without trusting subclass iteration hooks."""

    identity = id(value)
    if identity in active_containers or depth >= _MAX_LOGGING_SANITIZATION_DEPTH:
        return "<REDACTED>"
    active_containers.add(identity)
    try:
        try:
            items = list(list.__iter__(value) if isinstance(value, list) else tuple.__iter__(value))
        except Exception:
            return [] if isinstance(value, list) else ()

        sanitized = [_sanitize_logging_value(item, active_containers, depth + 1) for item in items]
        if isinstance(value, list):
            return sanitized

        # Tuple subclasses can override renderer-visible protocols such as
        # iteration or repr and expose data that differs from their sanitized base
        # storage. Degrade every subclass, including named tuples, to an inert
        # built-in tuple before crossing the logging boundary.
        return tuple(sanitized)
    finally:
        active_containers.remove(identity)


def _sanitize_logging_value(
    value: Any,
    active_containers: set[int],
    depth: int,
) -> Any:
    """Return a renderer-safe copy of an arbitrary nested logging value.

    Only JSON scalar types and copied built-in containers cross the logging
    boundary. Caller-defined objects are redacted instead of being handed to a
    renderer that may invoke their ``__repr__`` implementation.
    """

    if isinstance(value, dict):
        return _sanitize_logging_mapping(value, active_containers, depth)
    if isinstance(value, (list, tuple)):
        return _sanitize_logging_sequence(value, active_containers, depth)
    if isinstance(value, str):
        try:
            normalized = str.__str__(value)
        except Exception:
            return "<REDACTED>"
        if is_sensitive_value(normalized):
            return mask_api_key(normalized)
        return normalized
    if value is None or type(value) in (bool, int, float):
        return value
    if isinstance(value, int):
        try:
            return int.__index__(value)
        except Exception:
            return "<REDACTED>"
    if isinstance(value, float):
        try:
            return float.__float__(value)
        except Exception:
            return "<REDACTED>"
    return "<REDACTED>"


def _sanitize_logging_key(key: Any) -> tuple[Any, bool]:
    """Return a JSON-safe mapping key and whether its value must be redacted."""
    if isinstance(key, str):
        try:
            normalized = str.__str__(key)
        except Exception:
            return "<REDACTED>", True
        if is_sensitive_field(normalized):
            return normalized, True
        if is_credential_shaped(normalized):
            return "<REDACTED>", True
        return normalized, False

    if key is None or type(key) in (bool, int, float):
        return key, False
    if isinstance(key, int):
        try:
            return int.__index__(key), False
        except Exception:
            return "<unsupported-key>", True
    if isinstance(key, float):
        try:
            return float.__float__(key), False
        except Exception:
            return "<unsupported-key>", True

    # JSON renderers reject arbitrary object keys. Do not call caller-controlled
    # __str__ or __repr__ while converting them into a safe placeholder.
    return "<unsupported-key>", False


def _sanitize_logging_mapping(
    data: dict[Any, Any],
    active_containers: set[int],
    depth: int,
) -> dict[Any, Any] | str:
    """Copy a mapping while bounding recursive and cyclic caller data."""

    identity = id(data)
    if identity in active_containers or depth >= _MAX_LOGGING_SANITIZATION_DEPTH:
        return "<REDACTED>"
    active_containers.add(identity)
    result: dict[Any, Any] = {}
    try:
        try:
            items = dict.items(data)
            for key, value in items:
                sanitized_key, redact_value = _sanitize_logging_key(key)
                if redact_value:
                    result[sanitized_key] = "<REDACTED>"
                else:
                    result[sanitized_key] = _sanitize_logging_value(
                        value, active_containers, depth + 1
                    )
        except Exception:
            # A malformed mapping subclass must not suppress the log call. Values
            # that cannot be extracted through the built-in dict implementation
            # are not safe to serialize.
            return {}
        return result
    finally:
        active_containers.remove(identity)


def sanitize_for_logging(data: dict[Any, Any]) -> dict[Any, Any]:
    """Create a renderer-safe copy of potentially sensitive logging data.

    Built-in mappings, lists, and tuples are copied recursively without
    trusting protocol overrides on subclasses. Sensitive field names retain
    their normalized key for compatibility while their values are redacted;
    credential-shaped keys and their paired values are both redacted.
    Unsupported scalar values are replaced before a renderer can call a
    caller-controlled ``__repr__`` implementation. Cyclic or excessively deep
    containers are replaced with ``<REDACTED>`` before rendering.

    Args:
        data: Dictionary that might contain sensitive data.

    Returns:
        New dictionary with sensitive keys and values masked.

    Example:
        >>> sanitize_for_logging({"api_key": "sk-secret123", "name": "test"})
        {'api_key': '<REDACTED>', 'name': 'test'}
    """
    sanitized = _sanitize_logging_mapping(data, set(), 0)
    return sanitized if isinstance(sanitized, dict) else {}


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
    def validate_path_containment(
        path: str | Path,
        allowed_root: str | Path,
    ) -> tuple[bool, str]:
        """Validate that a resolved path is contained within an allowed root.

        Prevents path traversal attacks by ensuring the resolved (symlink-free,
        canonicalized) path stays within the expected directory tree.

        Args:
            path: The path to validate.
            allowed_root: The root directory that must contain *path*.

        Returns:
            Tuple of (is_valid, error_message). error_message is empty if valid.
        """
        try:
            resolved = Path(path).resolve()
            root = Path(allowed_root).resolve()
        except (OSError, ValueError) as exc:
            return False, f"Path resolution failed: {exc}"

        if not resolved.is_relative_to(root):
            return False, (f"Path escapes allowed root: {resolved} is not under {root}")
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
