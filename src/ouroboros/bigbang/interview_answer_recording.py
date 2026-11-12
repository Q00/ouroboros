"""Record interview answers while retaining the common provenance effects."""

from typing import TYPE_CHECKING

from ouroboros.bigbang.answer_provenance import classify_answer_provenance
from ouroboros.interview_adapters import InterviewTurnContext, detect_explicit_confusion_terms

if TYPE_CHECKING:
    from ouroboros.bigbang.interview import InterviewRound, InterviewState


def record_interview_answer(
    state: "InterviewState", question: str, answer: str, question_id: str | None = None
) -> "InterviewRound":
    from ouroboros.bigbang.interview import InterviewRound

    provenance = classify_answer_provenance(answer)

    stored_question = ""
    if question_id is not None:
        matches = [
            round_data for round_data in state.rounds if round_data.question_id == question_id
        ]
        if (
            len(matches) != 1
            or matches[0].user_response is not None
            or matches[0].question != question
        ):
            raise ValueError("Invalid batch question target")
        round_data = matches[0]
        stored_question = round_data.question
        round_data.user_response = answer
        round_data.provenance = provenance
    elif any(
        round_data.batch_id and round_data.user_response is None for round_data in state.rounds
    ):
        raise ValueError("Pending batch requires question ID")
    elif state.rounds and state.rounds[-1].user_response is None:
        # A round persisted question-only. Refresh the question text when a
        # caller supplies one, since the stored copy can be a placeholder
        # from a partial persistence. The engine-persisted copy is still
        # kept as a matching candidate below: for reference contrasts it is
        # the authoritative text, and letting a host-rendered echo replace
        # it silently used to break resolution matching.
        round_data = state.rounds[-1]
        stored_question = round_data.question
        if question:
            round_data.question = question
        round_data.user_response = answer
        round_data.provenance = provenance
    else:
        round_data = InterviewRound(
            round_number=state.current_round_number,
            question=question,
            user_response=answer,
            provenance=provenance,
        )
        state.rounds.append(round_data)

    state.record_adapter_answer(round_data.question, answer, stored_question=stored_question)
    detected_terms = detect_explicit_confusion_terms(answer)
    if detected_terms:
        state.merge_turn_context(InterviewTurnContext(confused_terms=detected_terms))
    state.invalidate_requirement_distillation()
    # The ambiguity snapshot is derived from the answered rounds, exactly
    # like the distillation above, so it dies where its inputs change
    # rather than at each call site that remembers to clear it. Leaving it
    # to callers let a durable save land rounds from revision N+1 next to a
    # score from revision N: an interrupted process then resumed with a
    # score that _select_perspectives reads as current.
    state.clear_stored_ambiguity()
    state.mark_updated()
    return round_data
