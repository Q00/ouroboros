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
