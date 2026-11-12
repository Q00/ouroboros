"""Privacy-safe diagnostics for interview state persistence."""

from pathlib import Path

import structlog

from ouroboros.core.errors import ValidationError
from ouroboros.core.types import Result

log = structlog.get_logger(__name__)


def safe_save_reason(error: ValueError) -> str:
    allowed = {
        "interview state changed; reload before retrying",
        "interview state removed; reload before retrying",
    }
    text = str(error)
    return text if text in allowed else "invalid state"


def save_error(error: OSError | ValueError, interview_id: str) -> Result:
    if isinstance(error, ValueError):
        log.error("interview.state_save_failed", interview_id=interview_id)
        reason = safe_save_reason(error)
    else:
        log.exception("interview.state_save_failed", interview_id=interview_id, error=str(error))
        reason = str(error)
    return Result.err(
        ValidationError(
            f"Failed to save interview state: {reason}", details={"interview_id": interview_id}
        )
    )


def load_error(error: OSError | ValueError, interview_id: str, file_path: Path) -> Result:
    if isinstance(error, ValueError):
        log.error("interview.state_load_failed", interview_id=interview_id)
        reason = "invalid state"
    else:
        log.exception("interview.state_load_failed", interview_id=interview_id, error=str(error))
        reason = str(error)
    return Result.err(
        ValidationError(
            f"Failed to load interview state: {reason}",
            field="interview_id",
            value=interview_id,
            details={"file_path": str(file_path)},
        )
    )


def list_error(error: OSError | ValueError, file_path: Path) -> None:
    if isinstance(error, ValueError):
        log.warning("interview.list_failed_for_file", file_path=str(file_path))
    else:
        log.warning("interview.list_failed_for_file", file_path=str(file_path), error=str(error))
