"""Text utilities for Ouroboros."""

_TRUNCATION_SEPARATOR = "\n\n... (truncated) ...\n\n"


def truncate_head_tail(
    text: str,
    head: int = 500,
    tail: int = 2000,
    separator: str = _TRUNCATION_SEPARATOR,
) -> str:
    """Keep the first *head* and last *tail* characters of *text*.

    Execution output typically starts with setup noise (pip install, etc.)
    while the actionable information (stack traces, test results) appears
    at the end.  This function preserves both boundaries.

    If the text is short enough to fit within ``head + tail``, it is
    returned unchanged.
    """
    threshold = head + tail
    if len(text) <= threshold:
        return text

    return text[:head] + separator + text[-tail:]


_ELLIPSIS = "..."


def truncate_with_ellipsis(value: str | None, *, limit: int) -> str | None:
    """Trim *value* to *limit* characters, marking the cut with ``...``.

    Used for bounded diagnostic previews -- MCP argument dumps and warning
    logs -- where the point is to keep a line readable, not to preserve the
    payload. ``None`` passes through so callers can trim optional fields
    without a guard, and a value already within *limit* is returned unchanged.

    The ellipsis is included in the budget: the result never exceeds *limit*.
    """
    if value is None or len(value) <= limit:
        return value
    return f"{value[: limit - len(_ELLIPSIS)]}{_ELLIPSIS}"
