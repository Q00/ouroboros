"""Shared Codex CLI permission policy helpers.

This module centralizes how Ouroboros maps internal permission modes onto the
currently supported Codex CLI flags. Both the agent runtime and the Codex-based
LLM adapter use the same policy so permission behavior stays predictable.
"""

from __future__ import annotations

from typing import Literal

CodexPermissionMode = Literal["default", "acceptEdits", "bypassPermissions"]

_VALID_PERMISSION_MODES = frozenset({"default", "acceptEdits", "bypassPermissions"})


def resolve_codex_permission_mode(
    permission_mode: str | None,
    *,
    default_mode: CodexPermissionMode = "default",
) -> CodexPermissionMode:
    """Validate and normalize a Codex permission mode."""
    candidate = (permission_mode or default_mode).strip()
    if candidate not in _VALID_PERMISSION_MODES:
        msg = f"Unsupported Codex permission mode: {candidate}"
        raise ValueError(msg)
    return candidate  # type: ignore[return-value]


def build_codex_exec_permission_args(
    permission_mode: str | None,
    *,
    default_mode: CodexPermissionMode = "default",
) -> list[str]:
    """Translate a permission mode into Codex CLI exec flags.

    Mapping:
    - ``default`` -> read-only sandbox
    - ``acceptEdits`` -> ``--full-auto`` (workspace-write + automatic execution)
    - ``bypassPermissions`` -> no approvals, no sandbox
    """
    resolved = resolve_codex_permission_mode(permission_mode, default_mode=default_mode)
    if resolved == "default":
        return ["--sandbox", "read-only"]
    if resolved == "acceptEdits":
        return ["--full-auto"]
    return ["--dangerously-bypass-approvals-and-sandbox"]


__all__ = [
    "CodexPermissionMode",
    "build_codex_exec_permission_args",
    "resolve_codex_permission_mode",
]
