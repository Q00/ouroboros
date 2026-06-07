"""Typed classification of runtime subprocess failures.

CLI-backed runtimes (hermes, gemini-cli, ...) surface upstream failures as a
non-zero process exit plus free-form stdout/stderr text. Collapsing that to
``{"subtype": "error", "exit_code": N}`` discards the provider's typed signal —
the HTTP status and quota/usage-limit phrasing — that the orchestrator's
recoverable-failure classifier keys on. The consequence (see
``docs/rca-june-2026.md``, issue 1.1) is that a provider usage-limit window
hard-fails the whole run instead of pausing it gracefully.

``classify_subprocess_failure`` re-derives those typed fields from the failure
text so they can be attached to the runtime ``AgentMessage`` metadata. The
returned keys are a subset of those recognized by
``OrchestratorRunner._metadata_has_runtime_error_shape`` — in particular
``error_type`` is always present, which is what marks a failure as
runtime-owned and lets the existing (conservative) usage-limit text classifier
run at all.
"""

from __future__ import annotations

import re
from typing import Any

#: Generic runtime failure when no more specific class is detected.
RUNTIME_EXECUTION_ERROR = "RuntimeExecutionError"
#: Rate-limit / usage-limit / quota failure (HTTP 429 or matching phrasing).
RATE_LIMIT_ERROR = "RateLimitError"

# Match an HTTP 4xx/5xx status only when anchored to a status-like keyword, so
# durations ("5 hour") and timestamps ("21:41:18") are never read as codes.
_HTTP_STATUS_PATTERN = re.compile(
    r"(?:http|status(?:\s*code)?|code|error)\b\W{0,4}(?P<status>[45]\d\d)\b",
    re.IGNORECASE,
)

# Phrases that indicate a rate-limit / usage-limit / quota condition.
_RATE_LIMIT_PATTERN = re.compile(
    r"\b(?:rate[\s_-]*limit"
    r"|usage[\s_-]*limit"
    r"|quota"
    r"|too\s+many\s+requests"
    r"|limit\s+reached)\b",
    re.IGNORECASE,
)


def _extract_http_status(text: str) -> int | None:
    """Return the first anchored HTTP 4xx/5xx status code in ``text``."""
    match = _HTTP_STATUS_PATTERN.search(text)
    if match is None:
        return None
    return int(match.group("status"))


def classify_subprocess_failure(text: str, *, exit_code: int) -> dict[str, Any]:
    """Build typed error metadata for a non-zero runtime subprocess exit.

    Args:
        text: Combined stderr/stdout (or a synthesized message) describing the
            failure.
        exit_code: The process exit code.

    Returns:
        A metadata dict suitable for ``AgentMessage.data``. Always carries
        ``subtype="error"``, the ``exit_code``, and a typed ``error_type``;
        adds ``http_status`` when a status code can be identified.
    """
    failure_text = text or ""
    metadata: dict[str, Any] = {"subtype": "error", "exit_code": exit_code}

    http_status = _extract_http_status(failure_text)
    if http_status is not None:
        metadata["http_status"] = http_status

    is_rate_limited = http_status == 429 or _RATE_LIMIT_PATTERN.search(failure_text) is not None
    metadata["error_type"] = RATE_LIMIT_ERROR if is_rate_limited else RUNTIME_EXECUTION_ERROR

    return metadata


__all__ = [
    "RATE_LIMIT_ERROR",
    "RUNTIME_EXECUTION_ERROR",
    "classify_subprocess_failure",
]
