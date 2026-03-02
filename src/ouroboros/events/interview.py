"""Interview event definitions for interview lifecycle tracking.

Events follow the BaseEvent pattern (frozen pydantic, to_db_dict()) and use
the dot.notation.past_tense naming convention.
"""

from ouroboros.events.base import BaseEvent


def interview_started(
    interview_id: str,
    initial_context: str,
) -> BaseEvent:
    """Create event when a new interview session starts."""
    return BaseEvent(
        type="interview.started",
        aggregate_type="interview",
        aggregate_id=interview_id,
        data={
            "initial_context": initial_context[:500],
        },
    )


def interview_response_recorded(
    interview_id: str,
    round_number: int,
    question_preview: str,
    response_preview: str,
) -> BaseEvent:
    """Create event when a user response is recorded."""
    return BaseEvent(
        type="interview.response.recorded",
        aggregate_type="interview",
        aggregate_id=interview_id,
        data={
            "round_number": round_number,
            "question_preview": question_preview[:200],
            "response_preview": response_preview[:200],
        },
    )


def interview_completed(
    interview_id: str,
    total_rounds: int,
) -> BaseEvent:
    """Create event when an interview session completes."""
    return BaseEvent(
        type="interview.completed",
        aggregate_type="interview",
        aggregate_id=interview_id,
        data={
            "total_rounds": total_rounds,
        },
    )


def interview_failed(
    interview_id: str,
    error_message: str,
    phase: str,
) -> BaseEvent:
    """Create event when an interview encounters a fatal error."""
    return BaseEvent(
        type="interview.failed",
        aggregate_type="interview",
        aggregate_id=interview_id,
        data={
            "error": error_message[:500],
            "phase": phase,
        },
    )
