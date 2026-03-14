"""Pytest configuration for Ouroboros."""

import os

# Disable Rich/ANSI color codes in CLI test output.
# Without this, Rich inserts ANSI escape sequences at word boundaries
# (e.g. hyphens in --llm-backend), breaking plain-text assertions.
# See: https://no-color.org/
os.environ.setdefault("NO_COLOR", "1")
