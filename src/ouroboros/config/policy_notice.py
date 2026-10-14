"""One-line notice for a fresh start that took the shipped execution policy."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ouroboros.config.loader import default_execution_efficiency_mode

_NOTICE = (
    " (default; to switch future runs to efficient execution: `ooo config` or "
    "`ouroboros config set execution.default_policy efficient`)"
)


def policy_notice(arguments: Mapping[str, Any]) -> str:
    """Return the suffix for a start receipt's ``Efficiency Mode`` line.

    A request routed to Ouroboros starts without a policy question; this tells
    the user which policy ran and how to change it. Empty when the caller chose
    a policy, when resuming (``resume`` for auto, ``session_id`` for execute;
    the persisted contract applies), or when the configured default is not
    quality-first.
    """
    if any(arguments.get(key) for key in ("efficiency_mode", "resume", "session_id")):
        return ""
    return _NOTICE if default_execution_efficiency_mode() == "quality_first" else ""
