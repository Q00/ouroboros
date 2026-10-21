"""The constructor's model call keeps no session on disk.

The constructor's reply holds every oracle case, held-out inputs and
expected values included. A runtime that saves its session (Codex under
``CODEX_HOME/sessions`` and its sqlite state, Claude Code under
``~/.claude/projects``) would leave them where the worker, running as the
same user with the same runtime home, can read them. So before each
constructor call the runtime is switched to its no-persistence mode:

- Codex CLI (backend ``codex``): ``codex exec --ephemeral`` ("Run without
  persisting session files to disk"; checked against codex-cli 0.157: nothing
  under ``CODEX_HOME``, including its sqlite files, holds the call);
- Claude Code (backend ``claude``, Agent SDK): ``--no-session-persistence``
  (checked against Claude Code 2.1.283 in the SDK's stream-json mode: no
  transcript is written);
- Claude Code CLI worker (backend ``claude_mcp``, ``claude -p``, the runtime
  of the ``[mcp]`` profile's ``--runtime claude-cli``): the same flag, which
  its transport passes whenever it does not persist sessions (checked against
  Claude Code 2.1.284 in ``-p --output-format json`` mode: no transcript is
  written). The constructor turns its opt-in session persistence off;
- OMP (backend ``omp``): ``--no-session`` disables session persistence.

The same switch keeps the reply out of the logs: the Claude adapter logs
message and result text by length and SHA-256 only (``_log_message_text``).
The Codex CLI and OMP runtimes log no reply text.

A runtime without such a mode is refused before any model call
(``constructor_session_not_ephemeral:<backend>``): the attempt is a typed
construction failure, so the run falls back to the legacy verifier. A private
per-call runtime home is not attempted: which auth and config files each
other CLI needs is backend specific, and a guessed home either breaks the
call or copies credentials around.
"""

from __future__ import annotations

from typing import Any

CODEX_EPHEMERAL = ("--ephemeral",)
CLAUDE_NO_SESSION_PERSISTENCE = (("no-session-persistence", None),)
OMP_NO_SESSION = ("--no-session",)
NOT_EPHEMERAL_PREFIX = "constructor_session_not_ephemeral"


def disable_session_persistence(runtime: Any) -> str | None:
    """Switch ``runtime`` to keep no session on disk and no reply text in its logs.

    Returns ``None``, or the refusal reason for a runtime without such a mode.
    """
    backend = getattr(type(runtime), "_runtime_backend", None)
    if backend == "codex" and hasattr(runtime, "_exec_session_flags"):
        runtime._exec_session_flags = CODEX_EPHEMERAL
        return None
    if backend == "omp" and hasattr(runtime, "_session_cli_flags"):
        runtime._session_cli_flags = OMP_NO_SESSION
        return None
    if backend == "claude" and hasattr(runtime, "_session_cli_args"):
        runtime._session_cli_args = CLAUDE_NO_SESSION_PERSISTENCE
        runtime._log_message_text = False
        return None
    transport = getattr(runtime, "_transport", None)
    if getattr(transport, "backend_name", None) == "claude_mcp" and hasattr(
        transport, "_persist_sessions"
    ):
        # The CLI worker logs no reply text; without persistence its every
        # ``claude -p`` call carries ``--no-session-persistence``.
        transport._persist_sessions = False
        return None
    # A plugin runtime (for example LeaderDrivenWorkerRuntime) has no class
    # backend; name the backend it was configured with, not its class.
    configured = getattr(runtime, "runtime_backend", None)
    if not backend and isinstance(configured, str) and configured:
        backend = configured
    return f"{NOT_EPHEMERAL_PREFIX}:{backend or type(runtime).__name__}"


__all__ = [
    "CLAUDE_NO_SESSION_PERSISTENCE",
    "CODEX_EPHEMERAL",
    "NOT_EPHEMERAL_PREFIX",
    "OMP_NO_SESSION",
    "disable_session_persistence",
]
