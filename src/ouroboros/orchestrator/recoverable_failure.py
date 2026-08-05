"""Provider-neutral classification for failures that must pause execution.

The execution owner decides how a pause is persisted and resumed.  This module
owns only the conservative message classification shared by direct and parallel
entrypoints, so a quota window cannot be converted into retry/escalation merely
because it surfaced through a different orchestrator path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
import math
import re

from ouroboros.config import MAX_USAGE_LIMIT_PAUSE_SECONDS
from ouroboros.orchestrator.adapter import AgentMessage

_LONG_RETRY_AFTER_SECONDS = 60 * 60
_MAX_METADATA_MAPS = 32
_MAX_PLAIN_METADATA_FIELDS = 128
_MAX_RETRY_TIMESTAMP_CHARS = 128
_MISSING_METADATA_VALUE = object()
_MAX_DURABLE_PAUSE_REASON_CHARS = 16_384
_MAX_DURABLE_PAUSE_HINT_CHARS = 1_024
_METADATA_CHILD_FIELDS = (
    "meta",
    "mcp_meta",
    "metadata",
    "headers",
    "error",
    "details",
    "response",
    "recovery",
)
_HTTP_STATUS_FIELDS = (
    "http_status",
    "httpStatus",
    "http_status_code",
    "httpStatusCode",
    "status_code",
    "statusCode",
    "api_error_status",
    "apiErrorStatus",
    "status",
    "code",
    "error_code",
    "errorCode",
)
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
_METADATA_FIELDS = tuple(
    dict.fromkeys(
        (
            *_METADATA_CHILD_FIELDS,
            *_HTTP_STATUS_FIELDS,
            "pause_seconds",
            "retry_after_seconds",
            "retryAfterSeconds",
            "reset_after_seconds",
            "resetAfterSeconds",
            "retry_after_ms",
            "retryAfterMs",
            "reset_after_ms",
            "resetAfterMs",
            "retry_after",
            "retryAfter",
            "retry-after",
            "Retry-After",
            "reset_after",
            "resetAfter",
            "resume_after",
            "resumeAfter",
            "reset_at",
            "resetAt",
            "error_type",
            "type",
            "reason",
            "message",
            "provider",
            "recoverable",
            "is_retriable",
            "retriable",
            "usage_limit",
            "quota_exhausted",
            "kind",
        )
    )
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
_CONCURRENCY_LIMIT_PATTERN = re.compile(
    r"\b(?:too\s+many\s+concurrent\s+requests"
    r"|(?:request\s+)?concurrenc(?:y|ies)\s+(?:limit|cap|maximum|exceeded|reached)"
    r"|(?:limit|cap|maximum)\s+(?:for\s+)?concurrent\s+requests?"
    r"|concurrent\s+requests?\s+(?:exceeded|rejected))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class UsageLimitPauseConsequence:
    """Complete bounded consequence of one provider quota-ending effect."""

    reason: str
    resume_hint: str
    pause_seconds: int
    resume_after: datetime
    pause_kind: str = "usage_limit"

    def to_payload(self) -> dict[str, object]:
        """Serialize the consequence into its closed durable schema."""

        _validate_usage_limit_pause_consequence(self)
        return {
            "schema_version": 1,
            "pause_kind": self.pause_kind,
            "reason": self.reason,
            "resume_hint": self.resume_hint,
            "pause_seconds": self.pause_seconds,
            "resume_after": self.resume_after.isoformat(),
        }

    @classmethod
    def from_payload(cls, value: object) -> UsageLimitPauseConsequence:
        """Restore an exact bounded consequence or reject the artifact."""

        expected_keys = frozenset(
            {
                "schema_version",
                "pause_kind",
                "reason",
                "resume_hint",
                "pause_seconds",
                "resume_after",
            }
        )
        if (
            not isinstance(value, Mapping)
            or set(value.keys()) != expected_keys
            or type(value.get("schema_version")) is not int
            or value.get("schema_version") != 1
        ):
            raise ValueError("usage-limit pause consequence has an invalid schema")
        raw_resume_after = value.get("resume_after")
        if not isinstance(raw_resume_after, str) or not raw_resume_after:
            raise ValueError("usage-limit pause consequence has an invalid resume_after")
        try:
            resume_after = datetime.fromisoformat(raw_resume_after)
        except ValueError as exc:
            raise ValueError("usage-limit pause consequence has an invalid resume_after") from exc
        consequence = cls(
            pause_kind=value.get("pause_kind"),  # type: ignore[arg-type]
            reason=value.get("reason"),  # type: ignore[arg-type]
            resume_hint=value.get("resume_hint"),  # type: ignore[arg-type]
            pause_seconds=value.get("pause_seconds"),  # type: ignore[arg-type]
            resume_after=resume_after,
        )
        _validate_usage_limit_pause_consequence(consequence)
        if consequence.to_payload() != dict(value):
            raise ValueError("usage-limit pause consequence is not canonical")
        return consequence


def _validate_usage_limit_pause_consequence(value: object) -> UsageLimitPauseConsequence:
    if (
        not isinstance(value, UsageLimitPauseConsequence)
        or value.pause_kind != "usage_limit"
        or type(value.reason) is not str
        or not value.reason
        or len(value.reason) > _MAX_DURABLE_PAUSE_REASON_CHARS
        or type(value.resume_hint) is not str
        or not value.resume_hint
        or len(value.resume_hint) > _MAX_DURABLE_PAUSE_HINT_CHARS
        or type(value.pause_seconds) is not int
        or not 1 <= value.pause_seconds <= MAX_USAGE_LIMIT_PAUSE_SECONDS
        or not isinstance(value.resume_after, datetime)
        or value.resume_after.tzinfo is None
        or value.resume_after.utcoffset() is None
    ):
        raise ValueError("usage-limit pause consequence exceeds its durable bounds")
    return value


def _format_pause_duration(seconds: int) -> str:
    if seconds % (24 * 60 * 60) == 0:
        days = seconds // (24 * 60 * 60)
        return f"{days} day{'s' if days != 1 else ''}"
    if seconds % (60 * 60) == 0:
        hours = seconds // (60 * 60)
        return f"{hours} hour{'s' if hours != 1 else ''}"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


def project_failure_metadata(
    message: AgentMessage,
) -> tuple[tuple[dict[str, object], ...], bool]:
    """Return bounded closed-vocabulary rows plus an ambiguity sentinel.

    Provider metadata is an untrusted protocol boundary.  Never iterate it and
    never retain a live nested ``Mapping`` for later consumers: either a fixed
    vocabulary can be projected into plain dictionaries or the final failure
    is conservatively treated as a pause.
    """

    candidates: list[dict[str, object]] = []
    seen: set[int] = set()
    pending: list[object] = [message.data]
    while pending:
        value = pending.pop()
        if not isinstance(value, Mapping) or id(value) in seen:
            continue
        if len(candidates) >= _MAX_METADATA_MAPS:
            return tuple(candidates), True
        seen.add(id(value))
        projected: dict[str, object] = {}
        for key in _METADATA_FIELDS:
            try:
                raw = value.get(key, _MISSING_METADATA_VALUE)
            except Exception:
                return tuple(candidates), True
            if raw is not _MISSING_METADATA_VALUE:
                projected[key] = raw
        if type(value) is dict:
            # HTTP field names are case-insensitive.  Plain dictionaries are
            # safe to inspect directly, but keep the scan bounded and never
            # iterate an arbitrary provider-owned Mapping implementation.
            if len(value) > _MAX_PLAIN_METADATA_FIELDS:
                return tuple(candidates), True
            retry_after_seen = False
            for raw_key, raw in value.items():
                if type(raw_key) is not str or raw_key.casefold() != "retry-after":
                    continue
                if retry_after_seen:
                    # Multiple differently-cased fields are ambiguous rather
                    # than an authority to pick the shorter cooldown.
                    return tuple(candidates), True
                projected["retry-after"] = raw
                retry_after_seen = True
        candidates.append(projected)
        pending.extend(projected.get(key) for key in _METADATA_CHILD_FIELDS)
    return tuple(candidates), False


def _metadata_value(metadata: Mapping[str, object], key: str) -> object:
    """Read one admitted key without propagating a hostile Mapping failure."""

    try:
        return metadata.get(key, _MISSING_METADATA_VALUE)
    except Exception:
        return _MISSING_METADATA_VALUE


def _duration_text_to_seconds(text: str) -> int | None:
    total = 0.0
    for match in _DURATION_PATTERN.finditer(text):
        value = float(match.group("value"))
        if not math.isfinite(value):
            return None
        unit = match.group("unit").lower()
        if unit.startswith("d"):
            total += value * 24 * 60 * 60
        elif unit.startswith("h"):
            total += value * 60 * 60
        elif unit.startswith("m"):
            total += value * 60
        else:
            total += value
        if not math.isfinite(total):
            return None
    return max(1, math.ceil(total)) if total > 0 else None


def _parse_datetime(value: object) -> datetime | None:
    """Parse an ISO timestamp or standards-compliant HTTP-date defensively."""

    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or len(stripped) > _MAX_RETRY_TIMESTAMP_CHARS:
        return None
    try:
        parsed = datetime.fromisoformat(stripped)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(stripped)
        except (TypeError, ValueError, OverflowError):
            return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _duration_value_to_seconds(value: object) -> int | None:
    """Parse one numeric or textual duration without non-finite arithmetic."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return max(1, math.ceil(value)) if math.isfinite(value) and value > 0 else None
    if not isinstance(value, str) or not value.strip():
        return None
    stripped = value.strip()
    try:
        numeric = float(stripped)
    except ValueError:
        return _duration_text_to_seconds(stripped)
    return max(1, math.ceil(numeric)) if math.isfinite(numeric) and numeric > 0 else None


def retry_duration_seconds_from_metadata(
    metadata: Mapping[str, object],
    *,
    now: datetime,
) -> int | None:
    """Resolve every supported relative or absolute provider retry encoding."""

    for key in (
        "pause_seconds",
        "retry_after_seconds",
        "retryAfterSeconds",
        "reset_after_seconds",
        "resetAfterSeconds",
    ):
        parsed = _duration_value_to_seconds(_metadata_value(metadata, key))
        if parsed is not None:
            return parsed

    for key in ("retry_after_ms", "retryAfterMs", "reset_after_ms", "resetAfterMs"):
        parsed = _duration_value_to_seconds(_metadata_value(metadata, key))
        if parsed is not None:
            return max(1, (parsed + 999) // 1000)

    for key in (
        "retry_after",
        "retryAfter",
        "retry-after",
        "Retry-After",
        "reset_after",
        "resetAfter",
    ):
        value = _metadata_value(metadata, key)
        parsed_datetime = _parse_datetime(value)
        if parsed_datetime is not None:
            seconds = math.ceil((parsed_datetime - now).total_seconds())
            if seconds > 0:
                return seconds
        parsed_duration = _duration_value_to_seconds(value)
        if parsed_duration is not None:
            return parsed_duration

    for key in ("resume_after", "resumeAfter", "reset_at", "resetAt"):
        parsed_datetime = _parse_datetime(_metadata_value(metadata, key))
        if parsed_datetime is not None:
            seconds = math.ceil((parsed_datetime - now).total_seconds())
            if seconds > 0:
                return seconds
    return None


def retry_duration_seconds_from_message(message: AgentMessage, *, now: datetime) -> int | None:
    """Resolve structured retry metadata, then bounded human-readable text."""

    metadata_rows, _overflowed = project_failure_metadata(message)
    for metadata in metadata_rows:
        duration = retry_duration_seconds_from_metadata(metadata, now=now)
        if duration is not None:
            return duration
    return _duration_text_to_seconds(message.content)


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
        value = _metadata_value(metadata, key)
        if isinstance(value, str):
            # Normalize RuntimeError-style CamelCase and machine separators so
            # typed values and prose pass through one vocabulary.
            parts.append(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value))
    return " ".join(parts).replace("_", " ").replace("-", " ").lower()


def _duration_from_metadata(metadata: Mapping[str, object], *, now: datetime) -> int | None:
    return retry_duration_seconds_from_metadata(metadata, now=now)


def _has_runtime_shape(metadata: Mapping[str, object]) -> bool:
    return any(
        _metadata_value(metadata, key) is not _MISSING_METADATA_VALUE
        for key in (
            "error_type",
            *_HTTP_STATUS_FIELDS,
            "provider",
            "recoverable",
            "is_retriable",
            "retriable",
            "retry_after",
            "retry_after_seconds",
            "retry_after_ms",
            "retryAfter",
            "retryAfterSeconds",
            "retryAfterMs",
            "resume_after",
            "resumeAfter",
            "reset_at",
            "resetAt",
            "reset_after",
            "reset_after_ms",
            "resetAfter",
            "resetAfterMs",
        )
    )


def _metadata_has_http_429(metadata: Mapping[str, object]) -> bool:
    """Recognize HTTP 429 across the closed provider status vocabulary.

    Provider adapters and protocol bridges use both snake_case and camelCase,
    while JSON transports may retain the code as a decimal string.  Only the
    exact integer/string code is admitted: bools, floats, and status prose do
    not become quota authority merely because they compare loosely to 429.
    """

    for key in _HTTP_STATUS_FIELDS:
        value = _metadata_value(metadata, key)
        if type(value) is int and value == 429:
            return True
        if type(value) is str and value.strip() == "429":
            return True
    return False


def _metadata_has_concurrency_limit_signal(metadata: Mapping[str, object]) -> bool:
    """Return whether bounded provider metadata names a concurrency cap."""

    kind = _metadata_value(metadata, "kind")
    if isinstance(kind, str) and kind.strip().lower() in {
        "concurrency_limit",
        "concurrency_exceeded",
        "concurrency_rejection",
        "too_many_concurrent_requests",
    }:
        return True
    return _CONCURRENCY_LIMIT_PATTERN.search(_metadata_text(metadata)) is not None


def is_usage_limit_pause_message(
    message: AgentMessage,
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether a final provider failure represents a quota-window pause."""

    if not isinstance(message, AgentMessage) or not (message.is_final and message.is_error):
        return False
    resolved_now = now or datetime.now(UTC)
    metadata_rows, metadata_overflowed = project_failure_metadata(message)
    if metadata_overflowed:
        # A final provider error with metadata beyond the inspected envelope is
        # ambiguous. It must pause rather than authorize a costlier successor.
        return True
    runtime_shaped = any(_has_runtime_shape(metadata) for metadata in metadata_rows)
    has_http_429 = any(_metadata_has_http_429(metadata) for metadata in metadata_rows)
    has_concurrency_limit_signal = (
        any(_metadata_has_concurrency_limit_signal(metadata) for metadata in metadata_rows)
        or _CONCURRENCY_LIMIT_PATTERN.search(message.content) is not None
    )
    has_long_retry_window = any(
        (duration := _duration_from_metadata(metadata, now=resolved_now)) is not None
        and duration >= _LONG_RETRY_AFTER_SECONDS
        for metadata in metadata_rows
    )
    if has_http_429 and has_long_retry_window and not has_concurrency_limit_signal:
        # Provider bridges may place the HTTP envelope and retry headers at
        # adjacent bounded metadata layers. They still describe one final
        # failure and must not authorize a costlier route merely because a
        # wrapper split the fields across its error/details objects.
        return True

    for metadata in metadata_rows:
        kind = _metadata_value(metadata, "kind")
        if isinstance(kind, str) and kind.strip().lower() in _RECOVERY_KINDS:
            return True
        if (
            _metadata_value(metadata, "usage_limit") is True
            or _metadata_value(metadata, "quota_exhausted") is True
        ):
            return True
        text = _metadata_text(metadata)
        duration = _duration_from_metadata(metadata, now=resolved_now)
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


def usage_limit_pause_consequence_from_message(
    message: AgentMessage,
    *,
    now: datetime,
    default_pause_seconds: int,
) -> UsageLimitPauseConsequence | None:
    """Freeze one quota message into the complete runner-owned pause decision."""

    if (
        type(default_pause_seconds) is not int
        or not 1 <= default_pause_seconds <= MAX_USAGE_LIMIT_PAUSE_SECONDS
    ):
        raise ValueError("durable usage-limit pause policy is outside its range")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("usage-limit pause observation time must be timezone-aware")
    if not is_usage_limit_pause_message(message, now=now):
        return None

    parsed_seconds = retry_duration_seconds_from_message(message, now=now)
    pause_seconds = default_pause_seconds if parsed_seconds is None else parsed_seconds
    pause_seconds = min(max(1, pause_seconds), MAX_USAGE_LIMIT_PAUSE_SECONDS)
    try:
        resume_after = now + timedelta(seconds=pause_seconds)
    except OverflowError:
        pause_seconds = default_pause_seconds
        resume_after = now + timedelta(seconds=pause_seconds)
    reason = message.content.strip()[:_MAX_DURABLE_PAUSE_REASON_CHARS]
    if not reason:
        reason = "Provider usage/quota window reached."
    duration_display = _format_pause_duration(pause_seconds)
    consequence = UsageLimitPauseConsequence(
        reason=reason,
        pause_seconds=pause_seconds,
        resume_after=resume_after,
        resume_hint=(
            "Provider usage/quota window reached. "
            f"Resume after {resume_after.isoformat()} "
            f"(wait at least {duration_display})."
        ),
    )
    return _validate_usage_limit_pause_consequence(consequence)


__all__ = [
    "UsageLimitPauseConsequence",
    "is_usage_limit_pause_message",
    "project_failure_metadata",
    "retry_duration_seconds_from_message",
    "retry_duration_seconds_from_metadata",
    "usage_limit_pause_consequence_from_message",
]
