"""Helpers for fitting styled TUI text to terminal display cells."""

from __future__ import annotations

from rich.cells import cell_len, set_cell_size
from rich.text import Text


def truncate_to_cell_width(content: str, width: int) -> str:
    """Truncate plain text to ``width`` terminal cells with an ASCII ellipsis."""
    width = max(0, width)
    if cell_len(content) <= width:
        return content

    ellipsis = "." * min(3, width)
    content_width = width - cell_len(ellipsis)
    return set_cell_size(content, content_width).rstrip() + ellipsis


def fit_text(
    content: str,
    width: int,
    *,
    prefix: Text | None = None,
    suffix: Text | None = None,
    content_style: str | None = None,
) -> Text:
    """Fit content between fixed styled text while bounding the complete line."""
    width = max(0, width)
    prefix = prefix.copy() if prefix is not None else Text()
    suffix = suffix.copy() if suffix is not None else Text()
    content_width = max(0, width - prefix.cell_len - suffix.cell_len)

    rendered = Text()
    rendered.append_text(prefix)
    rendered.append(truncate_to_cell_width(content, content_width), style=content_style)
    rendered.append_text(suffix)

    if rendered.cell_len > width:
        rendered.truncate(width, overflow="crop")
    return rendered


__all__ = ["fit_text", "truncate_to_cell_width"]
