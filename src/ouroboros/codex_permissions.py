"""Shared Codex CLI permission policy helpers.

This module is the *translation table* between the engine-owned
:class:`~ouroboros.orchestrator.policy.SandboxClass` vocabulary and the Codex
CLI's native ``--sandbox`` / ``--full-auto`` flags.  It deliberately does not
make policy decisions; those live in ``orchestrator/policy.py``.  Both the
Codex agent runtime and the Codex-based LLM adapter go through this module,
so Codex-side behavior stays consistent and is derived from the same sandbox
enum every other provider maps.
"""

from __future__ import annotations

from typing import Literal

import structlog

from ouroboros.sandbox import SandboxClass

log = structlog.get_logger(__name__)

CodexPermissionMode = Literal["default", "acceptEdits", "bypassPermissions"]

_VALID_PERMISSION_MODES = frozenset({"default", "acceptEdits", "bypassPermissions"})

# Legacy permission-mode vocabulary → engine SandboxClass.  The ``default``
# mode is read-only, ``acceptEdits`` maps to workspace-write (the Codex
# ``--full-auto`` flag), and ``bypassPermissions`` removes both the sandbox
# and the approval gate.  External callers that still speak the string
# vocabulary funnel through this mapping on their way to the sandbox enum.
_PERMISSION_MODE_TO_SANDBOX: dict[CodexPermissionMode, SandboxClass] = {
    "default": SandboxClass.READ_ONLY,
    "acceptEdits": SandboxClass.WORKSPACE_WRITE,
    "bypassPermissions": SandboxClass.UNRESTRICTED,
}

# Engine SandboxClass → Codex CLI invocation flags.  This is the only place
# Codex-specific flag names should appear for sandbox selection; new sandbox
# classes must add an entry here or the invariant test fails.
_SANDBOX_TO_CODEX_ARGS: dict[SandboxClass, list[str]] = {
    SandboxClass.READ_ONLY: ["--sandbox", "read-only"],
    SandboxClass.WORKSPACE_WRITE: ["--full-auto"],
    SandboxClass.UNRESTRICTED: ["--dangerously-bypass-approvals-and-sandbox"],
}


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


def build_codex_exec_args_for_sandbox(
    sandbox: SandboxClass,
    *,
    source: str | None = None,
    permission_mode: str | None = None,
    default_mode: CodexPermissionMode | None = None,
    resolved_mode: CodexPermissionMode | None = None,
) -> list[str]:
    """Translate a sandbox class into Codex CLI exec flags.

    This is the canonical entry point for new call sites.  Engine code
    should derive a :class:`SandboxClass` from a
    :class:`~ouroboros.orchestrator.policy.PolicyContext` and pass it here
    directly rather than round-tripping through a permission-mode string.
    """
    args = _SANDBOX_TO_CODEX_ARGS.get(sandbox)
    if args is None:
        # Invariant: every SandboxClass must have a Codex mapping.  If the
        # enum grows and this module was not updated, fail loudly instead
        # of silently defaulting to a possibly-unsafe sandbox.
        msg = f"No Codex CLI mapping registered for sandbox class {sandbox!r}"
        raise KeyError(msg)
    if sandbox is SandboxClass.UNRESTRICTED:
        log.warning(
            "permissions.bypass_activated",
            sandbox=sandbox.value,
            source=source,
            permission_mode=permission_mode,
            default_mode=default_mode,
            resolved_mode=resolved_mode,
        )
    return list(args)


def build_codex_exec_permission_args(
    permission_mode: str | None,
    *,
    default_mode: CodexPermissionMode = "default",
    source: str | None = None,
) -> list[str]:
    """Translate a legacy permission-mode string into Codex CLI exec flags.

    Thin wrapper preserved for call sites that still hold a string; it
    funnels through the SandboxClass enum so both call paths produce
    byte-identical flag lists.

    Mapping:
    - ``default`` -> ``SandboxClass.READ_ONLY`` -> read-only sandbox
    - ``acceptEdits`` -> ``SandboxClass.WORKSPACE_WRITE`` -> ``--full-auto``
    - ``bypassPermissions`` -> ``SandboxClass.UNRESTRICTED`` -> no approvals,
      no sandbox
    """
    resolved = resolve_codex_permission_mode(permission_mode, default_mode=default_mode)
    return build_codex_exec_args_for_sandbox(
        _PERMISSION_MODE_TO_SANDBOX[resolved],
        source=source,
        permission_mode=permission_mode,
        default_mode=default_mode,
        resolved_mode=resolved,
    )


__all__ = [
    "CodexPermissionMode",
    "build_codex_exec_args_for_sandbox",
    "build_codex_exec_permission_args",
    "resolve_codex_permission_mode",
]
