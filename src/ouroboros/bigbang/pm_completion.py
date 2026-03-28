"""Shared PM interview completion helpers."""

from __future__ import annotations

from typing import Any

from ouroboros.bigbang.interview import InterviewState
from ouroboros.bigbang.pm_interview import PMInterviewEngine
from ouroboros.core.errors import ValidationError
from ouroboros.core.types import Result


async def maybe_complete_pm_interview(
    state: InterviewState,
    engine: PMInterviewEngine,
) -> Result[tuple[InterviewState, dict[str, Any] | None], ValidationError]:
    """Run PM ambiguity completion logic and mark the interview complete if ready."""
    completion = await engine.check_completion(state)
    if completion is None:
        return Result.ok((state, None))

    complete_result = await engine.complete_interview(state)
    if complete_result.is_err:
        return Result.err(complete_result.error)

    return Result.ok((complete_result.value, completion))


def build_pm_completion_summary(
    *,
    session_id: str,
    completion: dict[str, Any] | None,
    stored_ambiguity_score: float | None = None,
    deferred_count: int,
    decide_later_count: int,
    decide_later_summary: str,
) -> str:
    """Build human-readable PM completion text for CLI and MCP flows."""
    lines = [f"Interview complete. Session ID: {session_id}"]

    if completion is not None:
        lines.append(f"Rounds completed: {completion['rounds_completed']}")
        lines.append(f"Completion reason: {completion['completion_reason']}")

    ambiguity_score = (
        completion.get("ambiguity_score") if completion is not None else stored_ambiguity_score
    )
    if ambiguity_score is not None:
        lines.append(f"Ambiguity score: {ambiguity_score:.2f}")

    lines.append("")
    lines.append(f"Deferred items: {deferred_count}")
    lines.append(f"Decide-later items: {decide_later_count}")

    if decide_later_summary:
        lines.append("")
        lines.append(decide_later_summary)

    lines.append("")
    lines.append(f'Generate PM with: action="generate", session_id="{session_id}"')
    return "\n".join(lines)
