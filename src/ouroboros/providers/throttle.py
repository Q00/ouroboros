"""Recognize a throttled provider call and pace the next attempt.

A call is throttled when the provider answered HTTP 429. The status is read
from structured fields first (``ProviderError.status_code``, an
``http_status`` in its details). Where a path only keeps the error text (an MCP
handler error, a runtime result flattened to its message), the one Codex CLI
parser (``codex_cli_adapter.codex_last_http_status``) reads the status Codex
reports after exhausting its own retries. No other text is matched.
"""

from __future__ import annotations

from collections.abc import Mapping

from ouroboros.providers.codex_cli_adapter import codex_last_http_status

THROTTLED_HTTP_STATUS = 429
# Waits before re-issuing a throttled call: 30 s, doubling, at most 300 s each.
THROTTLE_BACKOFF_BASE_SECONDS = 30.0
THROTTLE_BACKOFF_MAX_SECONDS = 300.0
# Total wait for one call across its throttled re-issues.
THROTTLE_TOTAL_CEILING_SECONDS = 900.0


def is_throttled(error: object) -> bool:
    """Whether ``error`` (an exception, a provider error or its text) is HTTP 429."""
    if error is None:
        return False
    status = getattr(error, "status_code", None)
    if status == THROTTLED_HTTP_STATUS:
        return True
    details = getattr(error, "details", None)
    if isinstance(details, Mapping) and details.get("http_status") == THROTTLED_HTTP_STATUS:
        return True
    return codex_last_http_status(str(error)) == THROTTLED_HTTP_STATUS


def throttle_backoff_seconds(throttled_attempts: int) -> float:
    """Wait before re-issuing a call throttled ``throttled_attempts`` times (1-based)."""
    return min(
        THROTTLE_BACKOFF_MAX_SECONDS,
        THROTTLE_BACKOFF_BASE_SECONDS * 2 ** max(0, throttled_attempts - 1),
    )


__all__ = [
    "THROTTLE_BACKOFF_BASE_SECONDS",
    "THROTTLE_BACKOFF_MAX_SECONDS",
    "THROTTLE_TOTAL_CEILING_SECONDS",
    "is_throttled",
    "throttle_backoff_seconds",
]
