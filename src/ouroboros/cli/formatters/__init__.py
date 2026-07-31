"""Rich formatters for CLI output.

This module provides a shared Console instance and exports all formatters
for consistent terminal output across the Ouroboros CLI.

Semantic Colors:
- green: success
- yellow: warning
- red: error
- blue: info
"""

import io
import sys

from rich.console import Console
from rich.theme import Theme

# Semantic color theme for consistent output
OUROBOROS_THEME = Theme(
    {
        "success": "green",
        "warning": "yellow",
        "error": "red",
        "info": "blue",
        "muted": "dim",
        "highlight": "bold cyan",
    }
)


def _make_console() -> Console:
    """Create a Console that can safely emit Unicode on all platforms.

    On Windows with legacy code-page consoles (e.g. cp949, cp932, cp936),
    Rich's default Console raises UnicodeEncodeError when printing characters
    outside the active code page.  We wrap stdout in a UTF-8 TextIOWrapper so
    Rich always encodes to UTF-8, which modern Windows Terminal and ConPTY
    render correctly.
    """
    file = None
    if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
        encoding = getattr(sys.stdout, "encoding", "") or ""
        if encoding.lower().replace("-", "") not in ("utf8", "utf_8"):
            file = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
            )
    return Console(theme=OUROBOROS_THEME, force_terminal=True, file=file)


# Shared Console instance for all CLI modules
console = _make_console()

__all__ = ["console", "OUROBOROS_THEME"]
