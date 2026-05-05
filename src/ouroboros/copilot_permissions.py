"""Shared GitHub Copilot CLI permission policy helpers.

This module is the *translation table* between the engine-owned
:class:`~ouroboros.sandbox.SandboxClass` vocabulary and the Copilot CLI's
native permission flags. It mirrors :mod:`ouroboros.codex_permissions` so
both backends derive their flag set from the same sandbox enum.

Copilot's relevant flags:

* ``--allow-all``: equivalent to ``--allow-all-tools --allow-all-paths
  --allow-all-urls`` — used for the unrestricted bypass mode.
* ``--allow-all-tools``: skip per-tool prompts (required for
  non-interactive ``-p`` mode whenever any tool may run). Combined with
  ``--add-dir`` for workspace-write semantics.
* ``--deny-tool=*``: the read-only mode disables every tool by denying the
  full wildcard, leaving Copilot as a pure-completion engine.
"""

from __future__ import annotations

from typing import Literal

import structlog

from ouroboros.sandbox import SandboxClass

log = structlog.get_logger(__name__)

CopilotPermissionMode = Literal["default", "acceptEdits", "bypassPermissions"]

_VALID_PERMISSION_MODES = frozenset({"default", "acceptEdits", "bypassPermissions"})

# Legacy permission-mode vocabulary -> engine SandboxClass. Same shape as
# Codex so callers that funnel through the string vocabulary land at the
# identical sandbox enum on both backends.
_PERMISSION_MODE_TO_SANDBOX: dict[CopilotPermissionMode, SandboxClass] = {
    "default": SandboxClass.READ_ONLY,
    "acceptEdits": SandboxClass.WORKSPACE_WRITE,
    "bypassPermissions": SandboxClass.UNRESTRICTED,
}

# Engine SandboxClass -> Copilot CLI invocation flags. Only sandbox-defining
# flags belong here; the per-call ``--available-tools`` and ``--add-dir``
# values are emitted separately because they depend on the call's
# ``allowed_tools`` envelope and ``cwd``, not on the static sandbox class.
_SANDBOX_TO_COPILOT_ARGS: dict[SandboxClass, list[str]] = {
    # Read-only: pass an empty allowlist via ``--available-tools=`` so the
    # model literally has zero tools to call. Copilot CLI rejects
    # ``--deny-tool=*`` (exits 1 at startup), so this is the only stable way
    # to express a tool-less reasoning surface.
    SandboxClass.READ_ONLY: ["--available-tools="],
    # Workspace-write: skip tool prompts. The caller adds ``--add-dir <cwd>``
    # to bound filesystem access to the workspace.
    SandboxClass.WORKSPACE_WRITE: ["--allow-all-tools"],
    # Unrestricted: open everything (tools, paths, URLs).
    SandboxClass.UNRESTRICTED: ["--allow-all"],
}


def resolve_copilot_permission_mode(
    permission_mode: str | None,
    *,
    default_mode: CopilotPermissionMode = "default",
) -> CopilotPermissionMode:
    """Validate and normalise a Copilot permission mode."""
    candidate = (permission_mode or default_mode).strip()
    if candidate not in _VALID_PERMISSION_MODES:
        msg = f"Unsupported Copilot permission mode: {candidate}"
        raise ValueError(msg)
    return candidate  # type: ignore[return-value]


def build_copilot_exec_args_for_sandbox(sandbox: SandboxClass) -> list[str]:
    """Translate a sandbox class into Copilot CLI exec flags."""
    args = _SANDBOX_TO_COPILOT_ARGS.get(sandbox)
    if args is None:
        msg = f"No Copilot CLI mapping registered for sandbox class {sandbox!r}"
        raise KeyError(msg)
    if sandbox is SandboxClass.UNRESTRICTED:
        log.warning("permissions.bypass_activated", sandbox=sandbox.value)
    return list(args)


def build_copilot_exec_permission_args(
    permission_mode: str | None,
    *,
    default_mode: CopilotPermissionMode = "default",
) -> list[str]:
    """Translate a legacy permission-mode string into Copilot CLI exec flags.

    Mapping:

    * ``default`` -> ``SandboxClass.READ_ONLY`` -> ``--available-tools=`` (empty allowlist)
    * ``acceptEdits`` -> ``SandboxClass.WORKSPACE_WRITE`` -> ``--allow-all-tools``
    * ``bypassPermissions`` -> ``SandboxClass.UNRESTRICTED`` -> ``--allow-all``
    """
    resolved = resolve_copilot_permission_mode(permission_mode, default_mode=default_mode)
    return build_copilot_exec_args_for_sandbox(_PERMISSION_MODE_TO_SANDBOX[resolved])


__all__ = [
    "CopilotPermissionMode",
    "build_copilot_exec_args_for_sandbox",
    "build_copilot_exec_permission_args",
    "resolve_copilot_permission_mode",
]
