"""One-line notice for a fresh start that took the shipped execution policy."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ouroboros.config.loader import default_execution_efficiency_mode

_NOTICE = (
    " (default; to switch future runs to efficient execution: "
    "`ouroboros config set execution.default_policy efficient`, after "
    "`ouroboros config init` if no config exists yet)"
)


def policy_notice(
    arguments: Mapping[str, Any], *, resolved_efficiency_mode: str | None = None
) -> str:
    """Return the suffix for a start receipt's ``Efficiency Mode`` line.

    A request routed to Ouroboros starts without a policy question; this tells
    the user which policy ran and how to change it. Empty when the caller chose
    a policy, when resuming (``resume`` for auto, ``session_id`` for execute;
    the persisted contract applies), or when the configured default is not
    quality-first.
    """
    if any(arguments.get(key) for key in ("efficiency_mode", "resume", "session_id")):
        return ""
    mode = (
        resolved_efficiency_mode
        if resolved_efficiency_mode is not None
        else default_execution_efficiency_mode()
    )
    return _NOTICE if mode == "quality_first" else ""
