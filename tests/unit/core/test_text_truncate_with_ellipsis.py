"""Tests for the shared ellipsis trim.

Per #1787, `orchestrator/codex_cli_runtime.py` and
`orchestrator/skill_intercept.py` carried byte-identical copies of this
three-line helper for bounded diagnostic previews. These cases pin the
behaviour both call sites relied on, including the edge where the limit is
narrower than the ellipsis itself.
"""

from __future__ import annotations

import pytest

from ouroboros.core.text import truncate_with_ellipsis


def reference(value: str | None, *, limit: int) -> str | None:
    """The pre-consolidation implementation, verbatim, as the oracle."""
    if value is None or len(value) <= limit:
        return value
    return f"{value[: limit - 3]}..."


CASES = [
    pytest.param(None, 10, id="none"),
    pytest.param("", 10, id="empty"),
    pytest.param("abc", 10, id="shorter-than-limit"),
    pytest.param("abcdefghij", 10, id="exactly-the-limit"),
    pytest.param("abcdefghijk", 10, id="one-over"),
    pytest.param("abcdefghijk", 4, id="limit-just-above-ellipsis"),
    pytest.param("abcdefghijk", 3, id="limit-equals-ellipsis"),
    pytest.param("abcdefghijk", 2, id="limit-below-ellipsis"),
    pytest.param("abcdefghijk", 0, id="zero-limit"),
    pytest.param("한글 값입니다 길게", 6, id="non-ascii"),
]


@pytest.mark.parametrize(("value", "limit"), CASES)
def test_matches_the_pre_consolidation_implementation(value: str | None, limit: int) -> None:
    assert truncate_with_ellipsis(value, limit=limit) == reference(value, limit=limit)


def test_none_passes_through() -> None:
    """Call sites trim optional fields without a guard."""
    assert truncate_with_ellipsis(None, limit=5) is None


def test_value_within_the_limit_is_returned_unchanged() -> None:
    assert truncate_with_ellipsis("abcde", limit=5) == "abcde"


def test_result_never_exceeds_the_limit() -> None:
    """The ellipsis is inside the budget, not added on top of it."""
    for limit in range(3, 20):
        result = truncate_with_ellipsis("x" * 100, limit=limit)
        assert result is not None
        assert len(result) <= limit


def test_truncation_is_marked() -> None:
    result = truncate_with_ellipsis("abcdefghijk", limit=10)
    assert result == "abcdefg..."
    assert result.endswith("...")
