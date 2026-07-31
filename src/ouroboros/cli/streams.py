"""Process-boundary normalization for Windows CLI text streams."""

from __future__ import annotations

import codecs
import sys
from typing import Any


def _is_utf8_stream(stream: Any) -> bool:
    encoding = getattr(stream, "encoding", None)
    if not isinstance(encoding, str) or not encoding:
        return False
    try:
        return codecs.lookup(encoding).name == "utf-8"
    except LookupError:
        return False


def _normalize_stream(stream: Any) -> None:
    """Reconfigure one active legacy stream without replacing or owning it."""
    try:
        if _is_utf8_stream(stream):
            return
        reconfigure = getattr(stream, "reconfigure", None)
    except (AttributeError, OSError, TypeError, ValueError):
        # Host-owned streams may expose stale or intentionally opaque
        # capability properties. Treat those capabilities as unavailable
        # instead of preventing Click from dispatching the command.
        return
    if not callable(reconfigure):
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, TypeError, ValueError):
        # Captured, embedded, or already-detached streams may expose a
        # non-functional reconfigure method. Preserve their ownership and let
        # the host decide how output is encoded.
        return


def normalize_windows_standard_streams() -> None:
    """Make the current Windows CLI streams Unicode-safe in place.

    This runs at Click's process boundary before option parsing and therefore
    covers eager callbacks and every Rich ``Console`` created by subcommands.
    Reconfiguring the active objects preserves dynamic ``sys.stdout`` lookup,
    output capture, redirection, and host ownership.
    """
    if sys.platform != "win32":
        return
    _normalize_stream(sys.stdout)
    _normalize_stream(sys.stderr)


__all__ = ["normalize_windows_standard_streams"]
