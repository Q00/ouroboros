"""Canonical transient-error classification shared across LLM adapters.

Historically every adapter carried its own ``_RETRYABLE_ERROR_PATTERNS`` /
``TRANSIENT_ERROR_PATTERNS`` tuple, and those copies **drifted**: the Claude
completion adapter retried on ``"overloaded"`` but not ``"429"``/``"5xx"``; the
Claude *execution* adapter retried on ``"429"``/``"5xx"`` but — critically — not
on ``"overloaded"`` (Anthropic's 529 ``overloaded_error``, the single most common
transient failure under load); the Codex adapter retried on ``"connection
reset"`` but not bare ``"connection"``. The same upstream blip therefore retried
in one adapter and hard-failed in another.

This module is the **single source of truth** for the transient core. Each
adapter composes its local tuple as ``(*TRANSIENT_ERROR_PATTERNS, *backend_specific)``
so the shared terms can never drift again while genuinely backend-specific
signals (e.g. Claude's CLI bootstrap ``"startup"``/``"empty response"``) stay
local where they are safe to match.

Matching is **substring, case-insensitive** — callers lower-case the message
first. Every pattern below is therefore lower-case and chosen to be a
conservative substring of a real transient error message.
"""

from __future__ import annotations

# The transient core shared by every adapter. UNION of the previously-drifted
# copies, using the broadest safe form of each term (e.g. ``"rate"`` subsumes
# ``"rate limit"``/``"rate_limited"``; ``"connection"`` subsumes ``"connection
# reset"``). Adding a term here makes *every* consuming adapter retry it — keep
# it to signals that are unambiguously transient.
TRANSIENT_ERROR_PATTERNS: tuple[str, ...] = (
    "concurrency",  # parallel-request contention inside an active session
    "rate",  # rate limit / rate-limited / rate_limit
    "429",  # HTTP 429 Too Many Requests
    "500",  # HTTP 500 Internal Server Error
    "502",  # HTTP 502 Bad Gateway
    "503",  # HTTP 503 Service Unavailable
    "504",  # HTTP 504 Gateway Timeout
    "timeout",
    "timed out",
    "overloaded",  # Anthropic 529 overloaded_error — the common one
    "temporarily",  # "temporarily unavailable"
    "try again",
    "connection",  # connection reset / aborted / error
)


def is_transient_error(
    message: str,
    *,
    extra_patterns: tuple[str, ...] = (),
) -> bool:
    """Return whether *message* looks like a transient, retry-worthy failure.

    Args:
        message: The raw error message (any case).
        extra_patterns: Backend-specific lower-case substrings to match in
            addition to the shared core (e.g. Claude's CLI-bootstrap signals).

    Returns:
        True when any shared or extra pattern is a substring of the
        lower-cased message.
    """
    lowered = message.lower()
    if any(pattern in lowered for pattern in TRANSIENT_ERROR_PATTERNS):
        return True
    return any(pattern in lowered for pattern in extra_patterns)


__all__ = ["TRANSIENT_ERROR_PATTERNS", "is_transient_error"]
