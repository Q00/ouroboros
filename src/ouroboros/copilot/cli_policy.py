"""Shared GitHub Copilot CLI launch policy helpers for runtime and provider callers.

This module mirrors :mod:`ouroboros.codex.cli_policy` so the Copilot adapter
can reuse the same recursion-guard/env-isolation pattern that Codex uses. The
shared ``_OUROBOROS_DEPTH`` counter is honoured here too, so a Copilot child
spawned from a Codex parent (or vice versa) still sees the depth incremented.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path

DEFAULT_COPILOT_CLI_NAME = "copilot"
DEFAULT_MAX_OUROBOROS_DEPTH = 5

# Env keys whose presence would cause the child Copilot CLI to discover the
# parent Ouroboros MCP and re-enter the orchestrator. Stripped on every spawn.
DEFAULT_COPILOT_CHILD_ENV_KEYS = ("OUROBOROS_AGENT_RUNTIME", "OUROBOROS_LLM_BACKEND")

# Copilot-specific session env keys that must not leak into a child process.
# COPILOT_SESSION_ID and COPILOT_RESUME would cause the child to attach to the
# parent's interactive session (or replay it), which is never what we want for
# a one-shot completion call. COPILOT_ALLOW_ALL is stripped because we want
# the child to honour our explicit ``--allow-tool`` envelope rather than
# inheriting an open-permission posture from the parent shell.
DEFAULT_COPILOT_CHILD_SESSION_ENV_KEYS = (
    "COPILOT_SESSION_ID",
    "COPILOT_RESUME",
    "COPILOT_ALLOW_ALL",
)

_WRAPPER_MAGIC_HEADERS = (
    b"\xcf\xfa\xed\xfe",  # Mach-O 64-bit
    b"\xce\xfa\xed\xfe",  # Mach-O 32-bit
    b"\x7fELF",  # ELF
)


@dataclass(frozen=True, slots=True)
class CopilotCliResolution:
    """Resolved Copilot CLI selection metadata."""

    cli_path: str
    candidate_path: str
    wrapper_path: str | None = None
    fallback_path: str | None = None


def resolve_copilot_cli_path(
    *,
    explicit_cli_path: str | Path | None,
    configured_cli_path: str | None,
    default_cli_name: str = DEFAULT_COPILOT_CLI_NAME,
    logger: object,
    log_namespace: str,
) -> CopilotCliResolution:
    """Resolve the safest Copilot CLI path for nested automation.

    When the configured candidate is a compiled wrapper (e.g. a shim), prefer
    the next real ``copilot`` binary on ``PATH`` instead. Mirrors the Codex
    resolver behaviour so wrapper-based installs do not trip the adapter.
    """
    if explicit_cli_path is not None:
        candidate = str(Path(explicit_cli_path).expanduser())
    else:
        candidate = configured_cli_path or _which(default_cli_name) or default_cli_name

    path = Path(candidate).expanduser()
    if not path.exists():
        return CopilotCliResolution(cli_path=candidate, candidate_path=candidate)

    resolved = str(path)
    if not is_wrapper_binary(resolved):
        return CopilotCliResolution(cli_path=resolved, candidate_path=resolved)

    logger.warning(  # type: ignore[attr-defined]
        f"{log_namespace}.cli_wrapper_detected",
        wrapper_path=resolved,
        hint="Searching PATH for the real Node.js copilot CLI.",
    )
    fallback = find_real_cli(default_cli_name=default_cli_name, skip=resolved)
    if fallback is not None:
        logger.info(  # type: ignore[attr-defined]
            f"{log_namespace}.cli_resolved_via_fallback",
            fallback_path=fallback,
        )
        return CopilotCliResolution(
            cli_path=fallback,
            candidate_path=resolved,
            wrapper_path=resolved,
            fallback_path=fallback,
        )

    logger.warning(  # type: ignore[attr-defined]
        f"{log_namespace}.cli_no_fallback",
        wrapper_path=resolved,
    )
    return CopilotCliResolution(
        cli_path=resolved,
        candidate_path=resolved,
        wrapper_path=resolved,
    )


def is_wrapper_binary(path: str) -> bool:
    """Return True when *path* looks like a compiled wrapper."""
    try:
        with open(path, "rb") as fh:
            magic = fh.read(4)
    except OSError:
        return False
    return magic in _WRAPPER_MAGIC_HEADERS


def find_real_cli(*, default_cli_name: str = DEFAULT_COPILOT_CLI_NAME, skip: str) -> str | None:
    """Walk ``PATH`` for the first executable ``copilot`` that is not a wrapper."""
    skip_path = Path(skip).resolve()
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory, default_cli_name)
        if not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
            continue
        resolved = Path(candidate).resolve()
        if resolved == skip_path:
            continue
        if is_wrapper_binary(candidate):
            continue
        return candidate
    return None


def build_copilot_child_env(
    *,
    base_env: Mapping[str, str] | None = None,
    max_depth: int = DEFAULT_MAX_OUROBOROS_DEPTH,
    child_session_env_keys: Sequence[str] = DEFAULT_COPILOT_CHILD_SESSION_ENV_KEYS,
    depth_error_factory: Callable[[int, int], Exception],
) -> dict[str, str]:
    """Build an isolated environment for nested Copilot subprocesses.

    Strips Ouroboros MCP discovery vars and Copilot session vars so the child
    starts as a clean one-shot process. Preserves ``GH_TOKEN`` /
    ``GITHUB_TOKEN`` because the child needs them to authenticate.
    """
    env = dict(os.environ if base_env is None else base_env)
    for key in DEFAULT_COPILOT_CHILD_ENV_KEYS:
        env.pop(key, None)
    for key in child_session_env_keys:
        env.pop(key, None)
    # Strip parent-runtime markers so child Copilot does not detect another
    # agent runtime and refuse to start or hang.
    env.pop("CLAUDECODE", None)
    env.pop("CODEX_THREAD_ID", None)

    try:
        depth = int(env.get("_OUROBOROS_DEPTH", "0")) + 1
    except (ValueError, TypeError):
        depth = 1

    if depth > max_depth:
        raise depth_error_factory(depth, max_depth)

    env["_OUROBOROS_DEPTH"] = str(depth)
    return env


def _which(name: str) -> str | None:
    """Locate an executable on ``PATH`` via :func:`shutil.which`.

    Using the stdlib implementation ensures correct behaviour on all
    platforms, including Windows ``PATHEXT`` resolution.
    """
    import shutil

    return shutil.which(name)


__all__ = [
    "CopilotCliResolution",
    "DEFAULT_COPILOT_CHILD_ENV_KEYS",
    "DEFAULT_COPILOT_CHILD_SESSION_ENV_KEYS",
    "DEFAULT_COPILOT_CLI_NAME",
    "DEFAULT_MAX_OUROBOROS_DEPTH",
    "build_copilot_child_env",
    "find_real_cli",
    "is_wrapper_binary",
    "resolve_copilot_cli_path",
]
