"""Interview transcript formatting and response-size accounting."""

from typing import Any

from ouroboros.bigbang.ambiguity import AmbiguityScore
from ouroboros.bigbang.answer_provenance import extraction_rounds
from ouroboros.bigbang.interview import (
    INITIAL_CONTEXT_SUMMARY_QUESTION,
    MAX_PROMPT_SAFE_INITIAL_CONTEXT_CHARS,
    InterviewState,
)
from ouroboros.mcp.tools.interview_advisory import _milestone_for_score
from ouroboros.mcp.tools.question_advisory import build_question_advisory_request


def _compute_transcript_chars(state: InterviewState) -> int:
    """Sum question + user_response length over every round in ``state``.

    Used by the response-shape diagnostic event (Q00/ouroboros#831) so we
    can correlate hang reports with cumulative interview context size.
    """
    total = 0
    for round_data in state.rounds:
        total += len(round_data.question or "")
        total += len(round_data.user_response or "")
    return total


def _format_interview_transcript(
    state: InterviewState, *, withhold_observations: bool = False
) -> str:
    """Format persisted interview rounds as a readable transcript for subagent context.

    Two call sites with different roles read this one formatter. Seed generation
    is a requirement-producing consumer and passes ``withhold_observations``; the
    interview subagent's conversation history does not, because the interview
    keeps seeing observations in full — informing the next question is what they
    were collected for (#1755). Question lines are unchanged either way.
    """
    if not state.rounds:
        return ""
    if withhold_observations:
        rendered = [
            (item.round_number, item.question, item.answer) for item in extraction_rounds(state)
        ]
    else:
        rendered = [(r.round_number, r.question, r.user_response) for r in state.rounds]
    lines: list[str] = []
    if state.initial_context:
        lines.append(f"**Initial Context:** {state.initial_context}")
        lines.append("")
    for round_number, question, answer in rendered:
        lines.append(f"**Q{round_number}:** {question}")
        if answer:
            lines.append(f"**A{round_number}:** {answer}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _build_question_advisory_request(
    *,
    session_id: str,
    question: str,
    phase: str,
    score: AmbiguityScore | None,
    code_investigation_request: dict[str, Any] | None = None,
    last_question: str | None = None,
) -> dict[str, Any]:
    """Build the interview's advisory request. Kept for existing importers.

    The builder moved to ``question_advisory`` and is shared with every tool
    that declares a catalog; this signature stays so a caller that had the
    interview's ambiguity score in hand does not have to unpack it here.
    """
    return build_question_advisory_request(
        tool_name="ouroboros_interview",
        session_id=session_id,
        question=question,
        phase=phase,
        ambiguity_score=score.overall_score if score is not None else None,
        milestone=_milestone_for_score(score),
        code_investigation_request=code_investigation_request,
        last_question=last_question,
    )


def _count_answered_rounds(state: InterviewState) -> int:
    """Return the number of completed interview rounds."""
    return sum(1 for round_data in state.rounds if round_data.user_response is not None)


def _is_initial_context_length_guard_question(question: str) -> bool:
    """Return True when ``question`` is the length-guard meta-directive.

    The interview engine surfaces a fixed string as the "next question" when
    ``initial_context`` exceeds ``MAX_PROMPT_SAFE_INITIAL_CONTEXT_CHARS``.
    That string is not a real interview question — it asks the *caller* to
    re-send a shorter summary — so MCP responses carrying it must carry a
    distinguishing meta signal.  See Q00/ouroboros#831.
    """
    return question == INITIAL_CONTEXT_SUMMARY_QUESTION


def _length_guard_meta_fields() -> dict[str, Any]:
    """Return the meta keys that mark a length-guard response.

    The handler merges these into the existing ``meta`` dict (``session_id``,
    ``ambiguity_score``, etc.) when the returned question is the length-guard
    meta-directive.  Clients can branch on ``meta.reason`` to handle the case
    programmatically instead of mis-routing the question to a human via
    AskUserQuestion.
    """
    return {
        "recoverable": True,
        "reason": "initial_context_too_large",
        "expected_action": "resend_with_summary",
        "max_chars": MAX_PROMPT_SAFE_INITIAL_CONTEXT_CHARS,
    }


_QUESTION_GENERATION_ENVELOPE_VIOLATION = "ToolUseBlockViolation"


def _provider_error_type(error: Any) -> str | None:
    details = getattr(error, "details", None)
    if isinstance(details, dict):
        error_type = details.get("error_type")
        if isinstance(error_type, str) and error_type:
            return error_type
    return None


def _is_question_generation_envelope_violation(error: Any) -> bool:
    """Return true for strict provider tool-envelope violations."""
    provider_error_type = _provider_error_type(error)
    if provider_error_type == _QUESTION_GENERATION_ENVELOPE_VIOLATION:
        return True
    return _QUESTION_GENERATION_ENVELOPE_VIOLATION in str(error)
