"""Shared Gemini CLI permission policy helpers.

This module centralizes how Ouroboros maps internal permission modes onto the
currently supported Gemini CLI flags. Both the agent runtime and the Gemini-based
LLM adapter use the same policy so permission behavior stays predictable.
"""

from __future__ import annotations

from typing import Literal

import structlog

log = structlog.get_logger(__name__)

GeminiPermissionMode = Literal["default", "acceptEdits", "bypassPermissions"]

_VALID_PERMISSION_MODES = frozenset({"default", "acceptEdits", "bypassPermissions"})


def resolve_gemini_permission_mode(
    permission_mode: str | None,
    *,
    default_mode: GeminiPermissionMode = "default",
) -> GeminiPermissionMode:
    """Validate and normalize a Gemini permission mode."""
    candidate = (permission_mode or default_mode).strip()
    if candidate not in _VALID_PERMISSION_MODES:
        msg = f"Unsupported Gemini permission mode: {candidate}"
        raise ValueError(msg)
    return candidate  # type: ignore[return-value]


def build_gemini_exec_permission_args(
    permission_mode: str | None,
    *,
    default_mode: GeminiPermissionMode = "default",
) -> list[str]:
    """Translate a permission mode into Gemini CLI flags.

    Mapping:
    - ``default`` -> ``--sandbox`` + ``--approval-mode default`` (read-only sandbox)
    - ``acceptEdits`` -> ``--approval-mode auto_edit`` (auto-approve edits)
    - ``bypassPermissions`` -> ``--yolo`` (auto-approve everything)
    """
    resolved = resolve_gemini_permission_mode(permission_mode, default_mode=default_mode)
    if resolved == "default":
        return ["--sandbox", "--approval-mode", "default"]
    if resolved == "acceptEdits":
        return ["--approval-mode", "auto_edit"]
    log.warning(
        "permissions.bypass_activated",
        mode="bypassPermissions",
    )
    return ["--yolo"]


__all__ = [
    "GeminiPermissionMode",
    "build_gemini_exec_permission_args",
    "resolve_gemini_permission_mode",
]
