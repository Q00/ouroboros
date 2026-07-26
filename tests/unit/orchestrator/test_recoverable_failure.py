"""Adversarial bounds for provider-neutral recoverable-failure classification."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.recoverable_failure import is_usage_limit_pause_message


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
