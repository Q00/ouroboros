"""Plugin interview state I/O without requiring a provider."""

import asyncio
from pathlib import Path

import structlog

from ouroboros.bigbang.interview import InterviewState
from ouroboros.core.owner_only import secure_directory
from ouroboros.core.types import Result

log = structlog.get_logger(__name__)


async def _plugin_save_state(state_dir: Path, state: InterviewState) -> Result[Path, str]:
    """Persist interview state without needing InterviewEngine (no LLM dep).

    Used exclusively in the plugin dispatch path where litellm may not be
    installed. Returns Result so callers can propagate persistence failures.
    """
    try:
        file_path = state_dir / f"interview_{state.interview_id}.json"
        state.mark_updated()
        from ouroboros.bigbang.interview import save_interview_state_checked

        secure_directory(file_path.parent)
        durability_confirmed = await save_interview_state_checked(file_path, state)

        if not durability_confirmed:
            # The stdio writer logs this state; discarding it here reported a
            # durability the filesystem never confirmed.
            log.warning(
                "plugin.state_save_durability_uncertain",
                interview_id=state.interview_id,
                file_path=str(file_path),
            )
        return Result.ok(file_path)
    except ValueError as e:
        reason = (
            str(e)
            if str(e)
            in (
                "interview state changed; reload before retrying",
                "interview state removed; reload before retrying",
            )
            else "invalid state"
        )
        return Result.err(f"Failed to save interview state: {reason}")
    except OSError as e:
        return Result.err(f"Failed to save interview state: {e}")


async def _plugin_load_state(state_dir: Path, interview_id: str) -> Result[InterviewState, str]:
    """Load interview state without needing InterviewEngine (no LLM dep).

    Used exclusively in the plugin dispatch path.
    """
    file_path = state_dir / f"interview_{interview_id}.json"
    if not file_path.exists():
        return Result.err(f"Interview state not found: {interview_id}")
    try:
        from ouroboros.core.file_lock import file_lock

        def _sync_read() -> str:
            with file_lock(file_path, exclusive=False):
                return file_path.read_text(encoding="utf-8")

        content = await asyncio.to_thread(_sync_read)
        state = InterviewState.model_validate_json(content)
        state._loaded_revision = state.persistence_revision
        state.discard_stale_requirement_distillation()
        return Result.ok(state)
    except ValueError:
        return Result.err("Failed to load interview state: invalid state")
    except OSError as e:
        return Result.err(f"Failed to load interview state: {e}")
