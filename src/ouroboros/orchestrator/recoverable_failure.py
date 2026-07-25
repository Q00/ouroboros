"""Provider-neutral classification for failures that must pause execution.

The execution owner decides how a pause is persisted and resumed.  This module
owns only the conservative message classification shared by direct and parallel
entrypoints, so a quota window cannot be converted into retry/escalation merely
because it surfaced through a different orchestrator path.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import math
import re

from ouroboros.orchestrator.adapter import AgentMessage

_LONG_RETRY_AFTER_SECONDS = 60 * 60
_MAX_METADATA_MAPS = 32
_RECOVERY_KINDS = frozenset(
    {
        "usage_limit",
        "usage_quota",
        "quota_limit",
        "quota_window",
        "quota_exceeded",
        "quota_exhausted",
        "usage_limit_pause",
    }
)
_DURATION_PATTERN = re.compile(
    r"\b(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b",
    re.IGNORECASE,
)
_LIMIT_PATTERN = re.compile(
    r"\b(?:usage|quota|credit|request)\s+"
    r"(?:limit|quota|cap|window|allowance)\b.{0,120}"
    r"\b(?:hit|reached|exceeded|exhausted|depleted|reset|resets|available|renews)\b"
    r"|\b(?:hit|reached|exceeded|exhausted|depleted|reset|resets|available|renews)\b"
    r".{0,120}\b(?:usage|quota|credit|request)\s+"
    r"(?:limit|quota|cap|window|allowance)\b"
    r"|\b(?:quota|allowance)\s+(?:exceeded|exhausted|depleted)\b"
    r"|\brate\s+limit\s+window\b.{0,80}"
    r"\b(?:hit|reached|exceeded|exhausted|depleted|reset|resets)\b",
    re.IGNORECASE,
)


def _metadata_candidates(message: AgentMessage) -> tuple[Mapping[str, object], ...]:
    candidates: list[Mapping[str, object]] = []
    seen: set[int] = set()
    pending: list[object] = [message.data]
    while pending and len(candidates) < _MAX_METADATA_MAPS:
        value = pending.pop()
        if not isinstance(value, Mapping) or id(value) in seen:
            continue
        seen.add(id(value))
        candidates.append(value)
        for key in ("meta", "mcp_meta", "metadata", "error", "details", "response"):
            pending.append(value.get(key))
    return tuple(candidates)


def _duration_text_to_seconds(text: str) -> int | None:
    total = 0.0
    for match in _DURATION_PATTERN.finditer(text):
        value = float(match.group("value"))
        unit = match.group("unit").lower()
        if unit.startswith("d"):
            total += value * 24 * 60 * 60
        elif unit.startswith("h"):
            total += value * 60 * 60
        elif unit.startswith("m"):
            total += value * 60
        else:
            total += value
    return max(1, math.ceil(total)) if total > 0 else None


def _metadata_text(metadata: Mapping[str, object]) -> str:
    parts: list[str] = []
    for key in (
        "error_type",
        "error_code",
        "code",
        "type",
        "reason",
        "message",
        "status",
        "provider",
    ):
        value = metadata.get(key)
        if isinstance(value, str):
            # Normalize RuntimeError-style CamelCase and machine separators so
            # typed values and prose pass through one vocabulary.
            parts.append(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value))
    return " ".join(parts).replace("_", " ").replace("-", " ").lower()


def _duration_from_metadata(metadata: Mapping[str, object], *, now: datetime) -> int | None:
    for key in (
        "pause_seconds",
        "retry_after_seconds",
        "retryAfterSeconds",
        "reset_after_seconds",
        "resetAfterSeconds",
        "retry_after",
        "retryAfter",
        "reset_after",
        "resetAfter",
    ):
        value = metadata.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int | float) and value > 0:
            return max(1, math.ceil(value))
        if isinstance(value, str) and value.strip():
            try:
                numeric = float(value)
            except ValueError:
                try:
                    parsed = datetime.fromisoformat(value.strip())
                except ValueError:
                    parsed = None
                if parsed is not None:
                    parsed = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
                    seconds = math.ceil((parsed - now).total_seconds())
                    if seconds > 0:
                        return seconds
                duration = _duration_text_to_seconds(value)
                if duration is not None:
                    return duration
            else:
                if numeric > 0:
                    return max(1, math.ceil(numeric))
    return None


def _has_runtime_shape(metadata: Mapping[str, object]) -> bool:
    return any(
        key in metadata
        for key in (
            "error_type",
            "error_code",
            "code",
            "status",
            "status_code",
            "http_status",
            "provider",
            "recoverable",
            "is_retriable",
            "retriable",
            "retry_after",
            "retry_after_seconds",
            "retryAfter",
            "retryAfterSeconds",
            "resume_after",
            "reset_at",
            "reset_after",
        )
    )


def is_usage_limit_pause_message(
    message: AgentMessage,
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether a final provider failure represents a quota-window pause."""

    if not isinstance(message, AgentMessage) or not (message.is_final and message.is_error):
        return False
    resolved_now = now or datetime.now(UTC)
    metadata_rows = _metadata_candidates(message)
    runtime_shaped = any(_has_runtime_shape(metadata) for metadata in metadata_rows)

    for metadata in metadata_rows:
        recovery = metadata.get("recovery")
        if isinstance(recovery, Mapping):
            kind = str(recovery.get("kind", "")).strip().lower()
            if kind in _RECOVERY_KINDS:
                return True
        if metadata.get("usage_limit") is True or metadata.get("quota_exhausted") is True:
            return True
        status = metadata.get("http_status", metadata.get("status_code"))
        text = _metadata_text(metadata)
        duration = _duration_from_metadata(metadata, now=resolved_now)
        if status == 429 and duration is not None and duration >= _LONG_RETRY_AFTER_SECONDS:
            return True
        if _LIMIT_PATTERN.search(text) is not None:
            return True
        if (
            duration is not None
            and duration >= _LONG_RETRY_AFTER_SECONDS
            and re.search(r"\b(?:usage|quota|allowance|limit|window)\b", text)
        ):
            return True

    normalized_content = " ".join(message.content.lower().split())
    if not runtime_shaped:
        return False
    return _LIMIT_PATTERN.search(normalized_content) is not None


__all__ = ["is_usage_limit_pause_message"]
