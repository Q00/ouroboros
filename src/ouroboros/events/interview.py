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


def interview_response_emitted(
    interview_id: str,
    *,
    response_kind: str,
    round_number: int,
    payload_chars: int,
    transcript_chars: int,
    ambiguity_prefix_present: bool,
    is_length_guard: bool,
) -> BaseEvent:
    """Diagnostic event recording the shape of an MCP question-bearing response.

    No behaviour change: emitted alongside the existing lifecycle events so
    a later investigation can correlate hang reports (e.g. claude-agent-sdk
    producing an empty-``thinking`` / ``stop_reason=tool_use`` turn after
    receiving an interview question) with response payload characteristics.
    See Q00/ouroboros#831 comment thread for the hang trace context.

    Args:
        interview_id: Session id.
        response_kind: One of ``"start"`` / ``"resume_pending"`` /
            ``"answer"`` -- which build path produced the response.
        round_number: Round count at emission time (``len(state.rounds)``).
        payload_chars: Total characters in the response text body.
        transcript_chars: Sum of ``len(question) + len(user_response)`` for
            every round in ``state.rounds``, including pending questions with
            no response yet.
        ambiguity_prefix_present: ``True`` when the response text begins
            with ``(ambiguity: ...)``.
        is_length_guard: ``True`` when the response carries the
            ``INITIAL_CONTEXT_SUMMARY_QUESTION`` meta-directive.
    """
    return BaseEvent(
        type="interview.response.emitted",
        aggregate_type="interview",
        aggregate_id=interview_id,
        data={
            "response_kind": response_kind,
            "round_number": round_number,
            "payload_chars": payload_chars,
            "transcript_chars": transcript_chars,
            "ambiguity_prefix_present": ambiguity_prefix_present,
            "is_length_guard": is_length_guard,
        },
    )
