"""Reasoning metadata for interview responses."""

from typing import Any

from ouroboros.bigbang.ambiguity import (
    AUTO_COMPLETE_STREAK_REQUIRED,
    AmbiguityScore,
    get_completion_floor_failures,
    qualifies_for_seed_completion,
)
from ouroboros.bigbang.interview import InterviewState
from ouroboros.mcp.tools.interview_advisory import _milestone_for_score
from ouroboros.mcp.tools.interview_transcript import _count_answered_rounds


def _interview_reasoning_meta(
    *,
    state: InterviewState | None,
    session_id: str | None,
    phase: str,
    next_action: str,
    score: AmbiguityScore | None = None,
    question: str | None = None,
    recoverable: bool = False,
) -> dict[str, Any]:
    """Build compact interview reasoning metadata for client-side UI surfaces."""
    answered_rounds = _count_answered_rounds(state) if state is not None else None
    total_rounds = len(state.rounds) if state is not None else None
    pending_question = (
        bool(state.rounds and state.rounds[-1].user_response is None)
        if state is not None
        else False
    )
    floor_failures: list[str] = []
    completion_qualified: bool | None = None
    if score is not None:
        floor_failures = get_completion_floor_failures(
            score,
            is_brownfield=state.is_brownfield if state is not None else False,
        )
        completion_qualified = (
            qualifies_for_seed_completion(
                score,
                is_brownfield=state.is_brownfield if state is not None else False,
            )
            and not floor_failures
        )

    lines = [f"phase: {phase}"]
    if session_id:
        lines.append(f"session: {session_id}")
    if answered_rounds is not None and total_rounds is not None:
        lines.append(f"rounds: {answered_rounds} answered / {total_rounds} total")
    if state is not None:
        lines.append(f"brownfield: {str(state.is_brownfield).lower()}")
        if pending_question:
            lines.append("pending: waiting for user answer")
        if state.completion_candidate_streak:
            lines.append(
                f"stability: {state.completion_candidate_streak}/{AUTO_COMPLETE_STREAK_REQUIRED}"
            )
    if score is not None:
        lines.append(f"ambiguity: {score.overall_score:.2f}")
        milestone = _milestone_for_score(score)
        if milestone:
            lines.append(f"milestone: {milestone}")
        if floor_failures:
            lines.append(f"completion blocked: {'; '.join(floor_failures)}")
    if question:
        lines.append(f"question_chars: {len(question)}")
    if recoverable:
        lines.append("recoverable: true")
    lines.append(f"next: {next_action}")

    return {
        "internal_reasoning": lines,
        "interview_reasoning": {
            "phase": phase,
            "next_action": next_action,
            "session_id": session_id,
            "answered_rounds": answered_rounds,
            "total_rounds": total_rounds,
            "pending_question": pending_question,
            "is_brownfield": state.is_brownfield if state is not None else None,
            "ambiguity_score": score.overall_score if score is not None else None,
            "milestone": _milestone_for_score(score),
            "seed_ready": (score.is_ready_for_seed if score is not None else None),
            "completion_qualified": completion_qualified,
            "completion_floor_failures": floor_failures,
            "completion_candidate_streak": (
                state.completion_candidate_streak if state is not None else None
            ),
            "streak_required": AUTO_COMPLETE_STREAK_REQUIRED,
            "recoverable": recoverable,
            "question_chars": len(question) if question else None,
        },
    }
