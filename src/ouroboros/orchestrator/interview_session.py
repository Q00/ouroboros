"""Runtime-handle contract for intercepted interview control turns."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import hashlib
import json
from typing import Any, Literal

from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import RuntimeHandle
from ouroboros.router.types import Resolved

INTERVIEW_SESSION_METADATA_KEY = "ouroboros_interview_session_id"
INTERVIEW_CALIBRATION_METADATA_KEY = "ouroboros_interview_calibration"

log = get_logger(__name__)


def interview_transition_digest() -> str:
    """Bind transition code and metadata keys to one portable execution identity."""
    # Reuse the dispatcher serializer for nested code, defaults, and properties.
    from ouroboros.orchestrator.command_dispatcher import CodexCommandDispatcher

    payload = {
        "transition": CodexCommandDispatcher._class_implementation_digest(
            InterviewSessionTransition
        ),
        "session_key": INTERVIEW_SESSION_METADATA_KEY,
        "calibration_key": INTERVIEW_CALIBRATION_METADATA_KEY,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class InterviewSessionTransition:
    """Own both halves of the in-memory interview protocol for every runtime.

    Calibration may precede the first interview and survives ordinary results
    that only return a session ID. It is replaced by the next calibration,
    never submitted as an answer, and omitted by RuntimeHandle persistence.
    """

    intercept: Resolved
    current_handle: RuntimeHandle | None

    @property
    def session_id(self) -> str | None:
        """Return the active interview ID, independently of native runtime IDs."""
        if self.current_handle is None:
            return None
        value = self.current_handle.metadata.get(INTERVIEW_SESSION_METADATA_KEY)
        return value.strip() if isinstance(value, str) and value.strip() else None

    @property
    def action(self) -> Literal["other", "start", "answer", "calibrate", "resume"]:
        """Classify control turns before assigning any pending answer."""
        if self.intercept.mcp_tool != "ouroboros_interview":
            return "other"
        evidence = self.intercept.mcp_args.get("calibration_input")
        if self.intercept.skill_name == "idk" or (isinstance(evidence, str) and evidence.strip()):
            return "calibrate"
        if self.session_id is None:
            return "start"
        return "answer" if self.intercept.first_argument is not None else "resume"

    def tool_arguments(self) -> dict[str, Any]:
        """Overlay session state while keeping calibration out of answer slots."""
        arguments: dict[str, Any] = dict(self.intercept.mcp_args)
        if self.action == "other":
            return arguments
        if self.session_id is not None:
            arguments.pop("initial_context", None)
            arguments["session_id"] = self.session_id
        if self.action == "calibrate":
            arguments.pop("answer", None)
        elif self.current_handle is not None:
            if self.action == "answer":
                arguments["answer"] = self.intercept.first_argument
            calibration = self.current_handle.metadata.get(INTERVIEW_CALIBRATION_METADATA_KEY)
            if isinstance(calibration, Mapping):
                arguments["interview_calibration"] = dict(calibration)
        return arguments

    def resume_handle(
        self,
        result_metadata: Mapping[str, Any],
        *,
        backend: str,
        cwd: str | None,
        approval_mode: str | None = None,
        log_namespace: str = "interview_session",
    ) -> RuntimeHandle | None:
        """Retain returned calibration even when no interview has started yet."""
        if self.action == "other":
            return self.current_handle
        session_id = result_metadata.get("session_id")
        calibration = result_metadata.get("interview_calibration")
        valid_session_id = isinstance(session_id, str) and bool(session_id.strip())
        valid_calibration = isinstance(calibration, Mapping)
        if not valid_session_id and not valid_calibration:
            if session_id is not None:
                log.warning(
                    f"{log_namespace}.resume_handle.invalid_session_id",
                    session_id_type=type(session_id).__name__,
                    session_id_value=repr(session_id),
                )
            return self.current_handle

        metadata = dict(self.current_handle.metadata) if self.current_handle is not None else {}
        if valid_session_id:
            metadata[INTERVIEW_SESSION_METADATA_KEY] = session_id.strip()
        if valid_calibration:
            metadata[INTERVIEW_CALIBRATION_METADATA_KEY] = dict(calibration)
        updated_at = datetime.now(UTC).isoformat()
        if self.current_handle is not None:
            return replace(self.current_handle, metadata=metadata, updated_at=updated_at)
        return RuntimeHandle(
            backend=backend,
            cwd=cwd,
            approval_mode=approval_mode,
            updated_at=updated_at,
            metadata=metadata,
        )
