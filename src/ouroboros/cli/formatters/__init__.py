"""Rich formatters for CLI output.

This module provides a shared Console instance and exports all formatters
for consistent terminal output across the Ouroboros CLI.

Semantic Colors:
- green: success
- yellow: warning
- red: error
- blue: info
"""

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

# Shared Console instance for all CLI modules
console = Console(theme=OUROBOROS_THEME, force_terminal=True)

__all__ = ["console", "OUROBOROS_THEME"]
