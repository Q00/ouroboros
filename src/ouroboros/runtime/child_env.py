"""Backend-neutral child-process environment isolation / recursion guard.

Every CLI backend (codex, copilot, gemini, opencode, hermes, kiro) spawns the
vendor CLI as a subprocess and must:

* strip the environment keys that would otherwise let the child re-enter the
  Ouroboros orchestrator (a fork bomb), and
* increment/enforce the ``_OUROBOROS_DEPTH`` recursion counter so nested
  automation cannot exceed a fixed ceiling.

This module owns that single shared implementation.  Each backend supplies its
own ordered ``strip_keys`` tuple (the strip sets legitimately diverge between
backends — e.g. ``CLAUDECODE`` is stripped for codex/copilot/kiro but not for
gemini/hermes/opencode), its own ``depth_error_factory`` (``RuntimeError`` vs a
typed ``ProviderError``), and an optional ``post_build`` hook for per-backend
side effects (copilot's instruction-dir injection, kiro's ``OUROBOROS_SUBAGENT``
marker).

The ``_OUROBOROS_DEPTH`` semantics and the default depth ceiling are
byte-preserved from the original per-backend copies.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import os

# Recursion-guard env var: incremented on every nested spawn; the build raises
# once it would exceed ``max_depth``.  Preserved verbatim from the original
# hand-copied guards across all backends.
OUROBOROS_DEPTH_ENV_KEY = "_OUROBOROS_DEPTH"
DEFAULT_MAX_OUROBOROS_DEPTH = 5

# Keys stripped by every backend: their presence would let the child discover
# the parent Ouroboros runtime and re-enter the orchestrator.
DEFAULT_OUROBOROS_STRIP_KEYS = ("OUROBOROS_AGENT_RUNTIME", "OUROBOROS_LLM_BACKEND")


def build_child_env(
    *,
    base_env: Mapping[str, str] | None = None,
    strip_keys: Sequence[str] = DEFAULT_OUROBOROS_STRIP_KEYS,
    max_depth: int = DEFAULT_MAX_OUROBOROS_DEPTH,
    depth_error_factory: Callable[[int, int], Exception],
    post_build: Callable[[dict[str, str]], None] | None = None,
) -> dict[str, str]:
    """Build an isolated environment for a nested backend CLI subprocess.

    Args:
        base_env: Environment to derive from.  Defaults to ``os.environ``.
        strip_keys: Ordered keys to remove before the child starts.  Each
            backend supplies its exact set (this is where the strip sets
            legitimately diverge — preserve them as-is).
        max_depth: Recursion ceiling.  Raising past it triggers the factory.
        depth_error_factory: Builds the exception raised when the incremented
            depth would exceed ``max_depth``.  Receives ``(depth, max_depth)``.
        post_build: Optional hook applied to the finished env for per-backend
            side effects (instruction-dir injection, subagent markers).

    Returns:
        A new environment dict with markers stripped and the depth incremented.

    Raises:
        Exception: Whatever ``depth_error_factory`` returns when the depth
            ceiling would be exceeded.
    """
    env = dict(os.environ if base_env is None else base_env)
    for key in strip_keys:
        env.pop(key, None)

    try:
        depth = int(env.get(OUROBOROS_DEPTH_ENV_KEY, "0")) + 1
    except (ValueError, TypeError):
        depth = 1

    if depth > max_depth:
        raise depth_error_factory(depth, max_depth)

    env[OUROBOROS_DEPTH_ENV_KEY] = str(depth)

    if post_build is not None:
        post_build(env)

    return env


__all__ = [
    "DEFAULT_MAX_OUROBOROS_DEPTH",
    "DEFAULT_OUROBOROS_STRIP_KEYS",
    "OUROBOROS_DEPTH_ENV_KEY",
    "build_child_env",
]
