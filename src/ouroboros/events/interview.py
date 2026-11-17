"""Interview event definitions for interview lifecycle tracking.

Events follow the BaseEvent pattern (frozen pydantic, to_db_dict()) and use
the dot.notation.past_tense naming convention.
"""

from typing import Any

from ouroboros.events.base import BaseEvent


def _interview_timings(
    *,
    total_duration_ms: float | None,
    ambiguity_scoring_duration_ms: float | None,
    question_generation_duration_ms: float | None,
    advisory_build_duration_ms: float | None,
) -> dict[str, float | None]:
    """Return the shared privacy-safe timing payload for interview turns."""
    return {
        "total": total_duration_ms,
        "ambiguity_scoring": ambiguity_scoring_duration_ms,
        "question_generation": question_generation_duration_ms,
        "advisory_build": advisory_build_duration_ms,
    }


def interview_started(
    interview_id: str,
    initial_context: str,
    *,
    surface: str | None = None,
    host: str | None = None,
) -> BaseEvent:
    """Create event when a new interview session starts.

    ``surface`` labels the caller (``console``, ``mcp``, ``cli_init``, ``auto``)
    and ``host`` the MCP host family for calls made inside an MCP request; both
    are closed labels from ``ouroboros.bigbang.interview_closure``. A field is
    written only when given, so older readers see an unchanged payload.
    """
    return BaseEvent(
        type="interview.started",
        aggregate_type="interview",
        aggregate_id=interview_id,
        data={
            "initial_context": initial_context[:500],
            **({"surface": surface} if surface is not None else {}),
            **({"host": host} if host is not None else {}),
        },
    )


def interview_response_recorded(
    interview_id: str,
    round_number: int,
    question_preview: str,
    response_preview: str,
    *,
    question_id: str | None = None,
    ambiguity_dimension: str | None = None,
    answer_source: str | None = None,
    closure: dict[str, Any] | None = None,
) -> BaseEvent:
    """Create event when a user response is recorded.

    ``answer_source`` labels who answered (``from_user``, ``from_code``,
    ``auto_confirmed``, ``from_research``, ``from_auto``, ``unknown``).
    ``closure`` is the gate snapshot when the same turn was scored (CLI ``init``
    scores before it records the event); see ``closure_snapshot``.
    """
    return BaseEvent(
        type="interview.response.recorded",
        aggregate_type="interview",
        aggregate_id=interview_id,
        data={
            "round_number": round_number,
            "question_preview": question_preview[:200],
            "response_preview": response_preview[:200],
            **({"question_id": question_id} if question_id is not None else {}),
            **(
                {"ambiguity_dimension": ambiguity_dimension}
                if ambiguity_dimension is not None
                else {}
            ),
            **({"answer_source": answer_source} if answer_source is not None else {}),
            **({"closure": closure} if closure is not None else {}),
        },
    )


def interview_completed(
    interview_id: str,
    total_rounds: int,
    *,
    total_duration_ms: float | None = None,
    ambiguity_scoring_duration_ms: float | None = None,
    question_generation_duration_ms: float | None = None,
    advisory_build_duration_ms: float | None = None,
    closure: dict[str, Any] | None = None,
) -> BaseEvent:
    """Create event when an interview session completes.

    ``closure`` is the gate snapshot that closed the interview.
    """
    return BaseEvent(
        type="interview.completed",
        aggregate_type="interview",
        aggregate_id=interview_id,
        data={
            "total_rounds": total_rounds,
            "timings_ms": _interview_timings(
                total_duration_ms=total_duration_ms,
                ambiguity_scoring_duration_ms=ambiguity_scoring_duration_ms,
                question_generation_duration_ms=question_generation_duration_ms,
                advisory_build_duration_ms=advisory_build_duration_ms,
            ),
            **({"closure": closure} if closure is not None else {}),
        },
    )


def interview_seed_forced(
    interview_id: str,
    *,
    answered_rounds: int,
    interview_completed: bool,
    closure: dict[str, Any] | None = None,
) -> BaseEvent:
    """Create event when a Seed was generated with the closure gate bypassed.

    Emitted after ``force`` seed generation succeeds: the console's "Write the
    spec now", ``ouroboros_generate_seed(force=true)``, ``ooo auto``'s
    ledger-evidence close, or CLI "Generate Seed anyway". Numbers and labels
    only; ``closure`` is the gate snapshot of the score the Seed was stamped with.
    """
    return BaseEvent(
        type="interview.seed_forced",
        aggregate_type="interview",
        aggregate_id=interview_id,
        data={
            "answered_rounds": answered_rounds,
            "interview_completed": interview_completed,
            **({"closure": closure} if closure is not None else {}),
        },
    )


def interview_failed(
    interview_id: str,
    error_message: str,
    phase: str,
    *,
    total_duration_ms: float | None = None,
    ambiguity_scoring_duration_ms: float | None = None,
    question_generation_duration_ms: float | None = None,
    advisory_build_duration_ms: float | None = None,
) -> BaseEvent:
    """Create event when an interview encounters a fatal error."""
    return BaseEvent(
        type="interview.failed",
        aggregate_type="interview",
        aggregate_id=interview_id,
        data={
            "error": error_message[:500],
            "phase": phase,
            "timings_ms": _interview_timings(
                total_duration_ms=total_duration_ms,
                ambiguity_scoring_duration_ms=ambiguity_scoring_duration_ms,
                question_generation_duration_ms=question_generation_duration_ms,
                advisory_build_duration_ms=advisory_build_duration_ms,
            ),
        },
    )


def interview_question_parent_handoff(
    interview_id: str,
    *,
    phase: str,
    reason_code: str,
    provider_error_type: str | None = None,
    total_duration_ms: float | None = None,
    ambiguity_scoring_duration_ms: float | None = None,
    question_generation_duration_ms: float | None = None,
    advisory_build_duration_ms: float | None = None,
) -> BaseEvent:
    """Create event when question generation is handed to the parent session."""
    data = {
        "phase": phase,
        "reason_code": reason_code,
        "timings_ms": _interview_timings(
            total_duration_ms=total_duration_ms,
            ambiguity_scoring_duration_ms=ambiguity_scoring_duration_ms,
            question_generation_duration_ms=question_generation_duration_ms,
            advisory_build_duration_ms=advisory_build_duration_ms,
        ),
    }
    if provider_error_type:
        data["provider_error_type"] = provider_error_type
    return BaseEvent(
        type="interview.question_generation.parent_handoff",
        aggregate_type="interview",
        aggregate_id=interview_id,
        data=data,
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
    total_duration_ms: float | None = None,
    ambiguity_scoring_duration_ms: float | None = None,
    question_generation_duration_ms: float | None = None,
    advisory_build_duration_ms: float | None = None,
    closure: dict[str, Any] | None = None,
) -> BaseEvent:
    """Diagnostic event recording the shape of an MCP question-bearing response.

    No behaviour change: emitted alongside the existing lifecycle events so
    a later investigation can correlate hang reports (e.g. claude-agent-sdk
    producing an empty-``thinking`` / ``stop_reason=tool_use`` turn after
    receiving an interview question) with response payload characteristics.
    See Q00/ouroboros#831 comment thread for the hang trace context.

    ``closure`` is the gate snapshot that chose to ask this question instead of
    closing (start and answer responses; see ``closure_snapshot``).
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
            "timings_ms": _interview_timings(
                total_duration_ms=total_duration_ms,
                ambiguity_scoring_duration_ms=ambiguity_scoring_duration_ms,
                question_generation_duration_ms=question_generation_duration_ms,
                advisory_build_duration_ms=advisory_build_duration_ms,
            ),
            **({"closure": closure} if closure is not None else {}),
        },
    )


def interview_lateral_review_recommended(
    interview_id: str,
    *,
    from_milestone: str,
    to_milestone: str,
    ambiguity_score: float,
    round_number: int,
) -> BaseEvent:
    """Create event when a milestone transition recommends lateral review.

    The event is advisory only: it records that the main/session layer may run
    a lateral review between interview turns, but it does not imply that the
    interview handler invoked lateral thinking or blocked question generation.
    """
    return BaseEvent(
        type="interview.lateral_review.recommended",
        aggregate_type="interview",
        aggregate_id=interview_id,
        data={
            "from_milestone": from_milestone,
            "to_milestone": to_milestone,
            "ambiguity_score": ambiguity_score,
            "round_number": round_number,
            "reason": "first_forward_milestone_transition",
        },
    )
