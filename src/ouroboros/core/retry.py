"""Retry helpers and shared transient-error classification.

``is_transient_error`` is shared by every provider adapter and the execution
adapter, so its precision is a cross-cutting concern: a false positive burns the
whole retry budget on a deterministic failure and delays the real error.

Matching is therefore *per pattern*, not uniformly substring-based:

* Patterns with no realistic false positive ("timeout", "overloaded", ...) stay
  plain substrings — narrowing them would drop genuine variants such as
  ``ReadTimeout`` or ``TimeoutException``.
* Rate-limit spellings (``rate limit``, ``rate_limit``, ``RateLimitError``)
  are recognised, while a bare ``rate`` and word fragments such as ``generate``
  are not.
* HTTP status codes are matched only in a status-code *position* — after a
  bounded status-ish token, at the start of a line, or in front of their reason
  phrase — so fields such as ``zipcode: 500`` are not treated as HTTP failures.

``extra_patterns`` supplied by a caller keep substring semantics unless the
pattern has a precise rule in :data:`_PRECISE_PATTERN_REGEXES`, so adapter-local
vocabularies ("quota exceeded", "exit code 1", ...) behave exactly as before.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import lru_cache, wraps
import random
import re
from typing import Any, ParamSpec, TypeVar

import structlog

P = ParamSpec("P")
T = TypeVar("T")

log = structlog.get_logger()

BASE_TRANSIENT_PATTERNS: tuple[str, ...] = (
    "concurrency",  # parallel-request contention inside an active session
    "rate",  # rate limit / rate-limited / rate_limit
    "429",  # HTTP 429 Too Many Requests
    "500",  # HTTP 500 Internal Server Error
    "502",  # HTTP 502 Bad Gateway
    "503",  # HTTP 503 Service Unavailable
    "504",  # HTTP 504 Gateway Timeout
    "timeout",
    "timed out",
    "overloaded",  # Anthropic 529 overloaded_error
    "temporarily",  # "temporarily unavailable"
    "try again",
    "connection",  # connection reset / aborted / error
)

# Explicit HTTP/status tokens that put a bare number in a status-code position.
# Generic words such as "code", "got", and "returned" are deliberately absent:
# they also describe deterministic application values, counts, and validation
# errors.  Compound forms such as "APIStatusError" remain supported.
_HTTP_STATUS_CONTEXT = (
    r"(?<![a-z])(?:https?(?:/[\d.]+)?|status(?:[ _-]?code)?|response[ _-]?status"
    r"|api[ _-]?(?:status[ _-]?)?error)(?![a-z])"
)

# Reason phrases let a leading code be recognised mid-sentence, e.g.
# "Reconnecting... 1/5 (502 Bad Gateway)".
_HTTP_STATUS_REASONS: dict[str, str] = {
    "429": r"too\W{0,2}many\W{0,2}requests",
    "500": r"internal\W{0,2}server\W{0,2}error",
    "502": r"bad\W{0,2}gateway",
    "503": r"(?:service\W{0,2})?(?:temporarily\W{0,2})?unavailable",
    "504": r"gateway\W{0,2}time\W{0,2}out",
}


def _http_status_regex(code: str) -> str:
    """Build a regex that matches *code* only where a status code can appear."""
    alternatives = [
        # "HTTP 503 ...", "status code: 502", "APIStatusError: 500"
        rf"{_HTTP_STATUS_CONTEXT}\W{{0,4}}{code}(?![\d.])",
        # Natural upstream reports are specific enough to distinguish a status
        # from counts such as "received 500 tokens" or "returned 500 chars".
        rf"(?<![a-z])upstream\W+returned\W{{0,4}}{code}(?![\d.])",
        rf"(?<![a-z])received\W+(?:a\W+)?{code}\W+from\W+upstream(?![a-z])",
        # "429 from https://api.openai.com/v1/responses" (start of message/line)
        rf"^\W*{code}(?![\d.])",
    ]
    reason = _HTTP_STATUS_REASONS.get(code)
    if reason:
        # requests/urllib3 render "503 Server Error: Service Unavailable".
        alternatives.append(
            rf"(?<![\d.]){code}\W{{0,3}}(?:(?:client|server)\W+error\W{{0,3}})?{reason}"
        )
    return "|".join(alternatives)


_ALPHA_LEFT = r"(?<![a-z])"

# Patterns whose plain-substring form produced false positives. Everything not
# listed here is matched as a substring (see module docstring).
_PRECISE_PATTERN_REGEXES: dict[str, str] = {
    # Require a rate-limit spelling; a standalone "rate" describes many
    # deterministic validation failures (sample rate, tax rate, and so on).
    "rate": rf"{_ALPHA_LEFT}rate[ _-]?limit\w*",
    "429": _http_status_regex("429"),
    "500": _http_status_regex("500"),
    "502": _http_status_regex("502"),
    "503": _http_status_regex("503"),
    "504": _http_status_regex("504"),
}


@lru_cache(maxsize=512)
def _pattern_matcher(pattern: str) -> re.Pattern[str]:
    """Compile *pattern* into its matcher (precise rule, else literal substring)."""
    source = _PRECISE_PATTERN_REGEXES.get(pattern.lower(), re.escape(pattern.lower()))
    return re.compile(source, re.MULTILINE)


def is_transient_error(
    message: str,
    *,
    extra_patterns: tuple[str, ...] = (),
) -> bool:
    """Return whether *message* looks like a transient, retry-worthy failure."""
    lowered = message.lower()
    for pattern in (*BASE_TRANSIENT_PATTERNS, *extra_patterns):
        if _pattern_matcher(pattern).search(lowered):
            return True
    return False


# Backoff waits are jittered by default so concurrent generations that were
# rate-limited together do not retry in lockstep against the same provider.
DEFAULT_JITTER_RATIO = 0.5
# A retry must always yield the event loop for a measurable moment.
MIN_WAIT_SECONDS = 0.001


def _jittered_wait(base: float, wait_jitter: float | None) -> float:
    """Return the actual sleep for a retry — always > 0."""
    base = max(base, 0.0)
    if wait_jitter is None:
        # Equal jitter: half the wait is fixed, half is random.
        fixed = base * (1.0 - DEFAULT_JITTER_RATIO)
        sleep_for = fixed + random.uniform(0.0, base * DEFAULT_JITTER_RATIO)
    elif wait_jitter > 0:
        sleep_for = base + random.uniform(0.0, wait_jitter)
    else:
        sleep_for = base
    return max(sleep_for, MIN_WAIT_SECONDS)


def retry_async(
    *,
    on: tuple[type[BaseException], ...],
    attempts: int,
    wait_initial: float,
    wait_max: float,
    wait_jitter: float | None = None,
) -> Callable[[Callable[P, Any]], Callable[P, Any]]:
    """Retry an async callable with exponential backoff and jittered waits.

    Args:
        on: Exception types that are worth retrying.
        attempts: Total attempts, including the first one. Must be > 0.
        wait_initial: First backoff wait, in seconds.
        wait_max: Ceiling for the exponential backoff wait, in seconds.
        wait_jitter: ``None`` (default) applies equal jitter proportional to the
            current wait. A positive value adds ``uniform(0, wait_jitter)``
            seconds on top of the wait. ``0.0`` opts out of jitter entirely.
    """

    if attempts <= 0:
        msg = "attempts must be > 0"
        raise ValueError(msg)
    if wait_jitter is not None and wait_jitter < 0:
        msg = "wait_jitter must be >= 0 or None"
        raise ValueError(msg)

    def decorator(func: Callable[P, Any]) -> Callable[P, Any]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            delay = max(wait_initial, 0.0)
            for attempt in range(1, attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except on as exc:
                    if attempt >= attempts:
                        raise

                    sleep_for = _jittered_wait(min(delay, wait_max), wait_jitter)
                    log.warning(
                        "retry_async.retry",
                        target=getattr(func, "__qualname__", repr(func)),
                        attempt=attempt,
                        attempts=attempts,
                        delay=round(sleep_for, 3),
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    await asyncio.sleep(sleep_for)
                    delay = min(max(delay * 2, wait_initial), wait_max)

            msg = "retry_async exhausted without returning or raising"
            raise RuntimeError(msg)

        return wrapper

    return decorator


__all__ = [
    "BASE_TRANSIENT_PATTERNS",
    "DEFAULT_JITTER_RATIO",
    "MIN_WAIT_SECONDS",
    "is_transient_error",
    "retry_async",
]
