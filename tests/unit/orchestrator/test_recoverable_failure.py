"""Adversarial bounds for provider-neutral recoverable-failure classification."""

from __future__ import annotations

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
