"""Structural check of evidence spans an investigated auto answer cites.

The investigating answerer (``auto/investigator.py``) cites each fact it
reads from the repository as a span: a path, a 1-based line range, and a
quote. A span passes only when the file is inside the workspace and the quote
occurs in the cited lines. The check reads files and compares text; it asks
no model and does not interpret what the quote means.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Lines of slack on each side of the cited range (models miscount by a few).
LINE_WINDOW = 3
# A quote shorter than this (after whitespace normalization) proves nothing.
MIN_QUOTE_CHARS = 8
# A span longer than this is a whole-file citation, not evidence for a fact.
MAX_SPAN_LINES = 200
# Files larger than this are not read for the check.
MAX_FILE_BYTES = 5_000_000


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    """One cited span: ``path`` relative to the workspace root, lines 1-based."""

    path: str
    start_line: int
    end_line: int
    quote: str

    @property
    def label(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"

    @classmethod
    def from_reply(cls, item: Any) -> EvidenceSpan | None:
        """Read one ``evidence`` item of a reply; ``None`` when it is malformed."""
        if not isinstance(item, dict):
            return None
        path, quote = item.get("path"), item.get("quote")
        start, end = item.get("start_line"), item.get("end_line")
        if not isinstance(path, str) or not path.strip() or not isinstance(quote, str):
            return None
        if not all(isinstance(n, int) and not isinstance(n, bool) for n in (start, end)):
            return None
        return cls(path.strip(), start, end, quote)


def normalize_whitespace(text: str) -> str:
    """Collapse every run of whitespace to one space and strip the ends."""
    return " ".join(text.split())


def check_span(workspace: Path, span: EvidenceSpan) -> str | None:
    """Return ``None`` when ``span`` passes, else the reason it fails.

    Reasons: ``outside_workspace``, ``missing_file``, ``bad_range``,
    ``range_too_long``, ``quote_too_short``, ``file_too_large``,
    ``unreadable``, ``quote_not_found``.
    """
    root = workspace.resolve()
    raw = Path(span.path)
    try:
        candidate = (raw if raw.is_absolute() else root / raw).resolve()
    except (OSError, RuntimeError):
        return "missing_file"
    if not candidate.is_relative_to(root):
        return "outside_workspace"
    if not candidate.is_file():
        return "missing_file"
    if span.start_line < 1 or span.end_line < span.start_line:
        return "bad_range"
    if span.end_line - span.start_line + 1 > MAX_SPAN_LINES:
        return "range_too_long"
    quote = normalize_whitespace(span.quote)
    if len(quote) < MIN_QUOTE_CHARS:
        return "quote_too_short"
    try:
        if candidate.stat().st_size > MAX_FILE_BYTES:
            return "file_too_large"
        lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "unreadable"
    if span.start_line > len(lines):
        return "bad_range"
    low = max(0, span.start_line - 1 - LINE_WINDOW)
    high = min(len(lines), span.end_line + LINE_WINDOW)
    window = normalize_whitespace("\n".join(lines[low:high]))
    return None if quote in window else "quote_not_found"


__all__ = [
    "LINE_WINDOW",
    "MAX_SPAN_LINES",
    "MIN_QUOTE_CHARS",
    "EvidenceSpan",
    "check_span",
    "normalize_whitespace",
]
