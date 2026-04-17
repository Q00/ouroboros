"""Shared Codex CLI launch policy helpers for runtime and provider callers."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path

DEFAULT_CODEX_CLI_NAME = "codex"
DEFAULT_MAX_OUROBOROS_DEPTH = 5
DEFAULT_CODEX_CHILD_ENV_KEYS = ("OUROBOROS_AGENT_RUNTIME", "OUROBOROS_LLM_BACKEND")
DEFAULT_CODEX_CHILD_SESSION_ENV_KEYS = ("CODEX_THREAD_ID",)
_WRAPPER_MAGIC_HEADERS = (
    b"\xcf\xfa\xed\xfe",  # Mach-O 64-bit
    b"\xce\xfa\xed\xfe",  # Mach-O 32-bit
    b"\x7fELF",  # ELF
)


@dataclass(frozen=True, slots=True)
class CodexCliResolution:
    """Resolved Codex CLI selection metadata."""

    cli_path: str
    candidate_path: str
    wrapper_path: str | None = None
    fallback_path: str | None = None


def resolve_codex_cli_path(
    *,
    explicit_cli_path: str | Path | None,
    configured_cli_path: str | None,
    default_cli_name: str = DEFAULT_CODEX_CLI_NAME,
    logger: object,
    log_namespace: str,
) -> CodexCliResolution:
    """Resolve the safest Codex CLI path for nested automation.

    When the configured candidate is a compiled wrapper (for example a Zeude
    shim), prefer the next real ``codex`` binary on ``PATH`` instead.
    """
    if explicit_cli_path is not None:
        candidate = str(Path(explicit_cli_path).expanduser())
    else:
        candidate = configured_cli_path or _which(default_cli_name) or default_cli_name

    path = Path(candidate).expanduser()
    if not path.exists():
        return CodexCliResolution(cli_path=candidate, candidate_path=candidate)

    resolved = str(path)
    if not is_wrapper_binary(resolved):
        return CodexCliResolution(cli_path=resolved, candidate_path=resolved)

    logger.warning(
        f"{log_namespace}.cli_wrapper_detected",
        wrapper_path=resolved,
        hint="Searching PATH for the real Node.js codex CLI.",
    )
    fallback = find_real_cli(default_cli_name=default_cli_name, skip=resolved)
    if fallback is not None:
        logger.info(
            f"{log_namespace}.cli_resolved_via_fallback",
            fallback_path=fallback,
        )
        return CodexCliResolution(
            cli_path=fallback,
            candidate_path=resolved,
            wrapper_path=resolved,
            fallback_path=fallback,
        )

    logger.warning(
        f"{log_namespace}.cli_no_fallback",
        wrapper_path=resolved,
    )
    return CodexCliResolution(
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


def find_real_cli(*, default_cli_name: str = DEFAULT_CODEX_CLI_NAME, skip: str) -> str | None:
    """Walk ``PATH`` for the first executable ``codex`` that is not a wrapper."""
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


def build_codex_child_env(
    *,
    base_env: Mapping[str, str] | None = None,
    max_depth: int = DEFAULT_MAX_OUROBOROS_DEPTH,
    child_session_env_keys: Sequence[str] = DEFAULT_CODEX_CHILD_SESSION_ENV_KEYS,
    depth_error_factory: Callable[[int, int], Exception],
) -> dict[str, str]:
    """Build an isolated environment for nested Codex subprocesses."""
    env = dict(os.environ if base_env is None else base_env)
    for key in DEFAULT_CODEX_CHILD_ENV_KEYS:
        env.pop(key, None)
    for key in child_session_env_keys:
        env.pop(key, None)
    # Strip CLAUDECODE so child codex does not detect the parent Codex/Claude
    # session and hang or refuse to start.
    env.pop("CLAUDECODE", None)

    try:
        depth = int(env.get("_OUROBOROS_DEPTH", "0")) + 1
    except (ValueError, TypeError):
        depth = 1

    if depth > max_depth:
        raise depth_error_factory(depth, max_depth)

    env["_OUROBOROS_DEPTH"] = str(depth)
    return env


def _which(name: str) -> str | None:
    """Locate an executable on ``PATH``, delegating to :func:`shutil.which`.

    Using the stdlib implementation ensures correct behavior on all
    platforms, including Windows ``PATHEXT`` resolution.
    """
    import shutil

    return shutil.which(name)


__all__ = [
    "CodexCliResolution",
    "DEFAULT_CODEX_CHILD_ENV_KEYS",
    "DEFAULT_CODEX_CHILD_SESSION_ENV_KEYS",
    "DEFAULT_CODEX_CLI_NAME",
    "DEFAULT_MAX_OUROBOROS_DEPTH",
    "build_codex_child_env",
    "find_real_cli",
    "is_wrapper_binary",
    "resolve_codex_cli_path",
]
