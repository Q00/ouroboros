"""Adversarial bounds for provider-neutral recoverable-failure classification."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime

import pytest

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.recoverable_failure import is_usage_limit_pause_message

_STATUS_FIELDS = (
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


@pytest.mark.parametrize("status_field", _STATUS_FIELDS)
@pytest.mark.parametrize("status_value", (429, "429", " 429 "))
def test_long_window_429_status_population_is_one_pause_class(
    status_field: str,
    status_value: object,
) -> None:
    """Every supported provider spelling and JSON encoding pauses identically."""

    message = AgentMessage(
        type="result",
        content="Provider request failed.",
        data={
            "subtype": "error",
            status_field: status_value,
            "retry_after_seconds": 7_200,
        },
    )

    assert is_usage_limit_pause_message(message) is True


@pytest.mark.parametrize("status_field", _STATUS_FIELDS)
@pytest.mark.parametrize("status_value", (True, 429.0, "429.0", "429x", None))
def test_non_exact_429_status_population_never_authorizes_a_quota_pause(
    status_field: str,
    status_value: object,
) -> None:
    message = AgentMessage(
        type="result",
        content="Provider request failed.",
        data={
            "subtype": "error",
            status_field: status_value,
            "retry_after_seconds": 7_200,
        },
    )

    assert is_usage_limit_pause_message(message) is False


@pytest.mark.parametrize("status_field", _STATUS_FIELDS)
@pytest.mark.parametrize("status_value", (429, "429"))
def test_short_window_429_population_remains_an_ordinary_retryable_failure(
    status_field: str,
    status_value: object,
) -> None:
    message = AgentMessage(
        type="result",
        content="Provider request failed.",
        data={
            "subtype": "error",
            status_field: status_value,
            "retry_after_seconds": 30,
        },
    )

    assert is_usage_limit_pause_message(message) is False


@pytest.mark.parametrize(
    "container_field",
    ("meta", "mcp_meta", "metadata", "error", "details", "response"),
)
@pytest.mark.parametrize("status_is_nested", (False, True))
def test_status_and_retry_window_share_one_bounded_metadata_population(
    container_field: str,
    status_is_nested: bool,
) -> None:
    outer = {"statusCode": "429"} if not status_is_nested else {"retryAfterSeconds": "7200"}
    nested = {"retryAfterSeconds": "7200"} if not status_is_nested else {"statusCode": "429"}
    message = AgentMessage(
        type="result",
        content="Provider request failed.",
        data={"subtype": "error", **outer, container_field: nested},
    )

    assert is_usage_limit_pause_message(message) is True


@pytest.mark.parametrize(
    "retry_after",
    [
        float("inf"),
        float("-inf"),
        float("nan"),
        "inf",
        "-inf",
        "nan",
        f"{'9' * 400} hours",
    ],
)
def test_non_finite_retry_metadata_never_throws(retry_after: object) -> None:
    message = AgentMessage(
        type="result",
        content="Provider returned HTTP 429.",
        data={
            "subtype": "error",
            "http_status": 429,
            "retry_after_seconds": retry_after,
        },
    )

    assert is_usage_limit_pause_message(message) is False


def test_huge_integer_retry_metadata_remains_non_throwing() -> None:
    message = AgentMessage(
        type="result",
        content="Provider retry boundary.",
        data={
            "subtype": "error",
            "http_status": 429,
            "retry_after_seconds": 10**1000,
        },
    )

    assert is_usage_limit_pause_message(message) is True


@pytest.mark.parametrize(
    "metadata",
    [
        {"retry_after_ms": 7_200_000},
        {"retryAfterMs": "7200000"},
        {"resume_after": "2026-01-01T02:00:00+00:00"},
        {"resetAt": datetime(2026, 1, 1, 2, tzinfo=UTC)},
    ],
)
def test_all_supported_retry_encodings_share_quota_classification(
    metadata: dict[str, object],
) -> None:
    """Runner duration support and provider-neutral classification cannot drift."""

    now = datetime(2026, 1, 1, tzinfo=UTC)
    message = AgentMessage(
        type="result",
        content="Provider quota window.",
        data={
            "subtype": "error",
            "reason": "quota window",
            **metadata,
        },
    )

    assert is_usage_limit_pause_message(message, now=now) is True


def test_metadata_population_overflow_fails_closed_as_pause() -> None:
    """An unvisited metadata tail cannot authorize a costlier route."""

    data: dict[str, object] = {"subtype": "error", "reason": "provider failure"}
    cursor = data
    for _ in range(33):
        child: dict[str, object] = {}
        cursor["details"] = child
        cursor = child
    cursor["quota_exhausted"] = True
    message = AgentMessage(
        type="result",
        content="Provider request failed.",
        data=data,
    )

    assert is_usage_limit_pause_message(message) is True


class _ExplodingNestedMapping(Mapping[str, object]):
    """Legal Mapping whose fixed-key protocol fails on access."""

    def __getitem__(self, key: str) -> object:
        raise RuntimeError(f"provider mapping rejected {key}")

    def __iter__(self) -> Iterator[str]:
        yield "status_code"

    def __len__(self) -> int:
        return 1


@pytest.mark.parametrize("container_field", ("error", "details", "metadata", "recovery"))
def test_hostile_nested_mapping_protocol_fails_closed_without_throwing(
    container_field: str,
) -> None:
    """A completed provider failure cannot escape pause handling via Mapping.get."""

    message = AgentMessage(
        type="result",
        content="Provider request failed.",
        data={"subtype": "error", container_field: _ExplodingNestedMapping()},
    )

    assert is_usage_limit_pause_message(message) is True
