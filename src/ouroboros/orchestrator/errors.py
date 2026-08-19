"""Typed errors raised by orchestrator execution boundaries."""

from __future__ import annotations

from ouroboros.core.errors import OuroborosError


class OrchestratorError(OuroborosError):
    """Error during orchestrator execution."""


class ExecutionCancelledError(OuroborosError):
    """Raised when an execution is cancelled via the cancellation set."""

    def __init__(self, session_id: str, reason: str = "Cancelled by user") -> None:
        self.session_id = session_id
        self.reason = reason
        super().__init__(f"Execution cancelled for session {session_id}: {reason}")


__all__ = ["ExecutionCancelledError", "OrchestratorError"]
