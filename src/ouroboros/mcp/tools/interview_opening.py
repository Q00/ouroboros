"""Generate two unconfirmed opening interpretations without changing intent."""

from typing import TYPE_CHECKING, Any

from ouroboros.core.errors import ProviderError, ValidationError
from ouroboros.core.types import Result
from ouroboros.interview_adapters.opening_interpretations import (
    OPENING_INSTRUCTION,
    OPENING_QUESTION,
    ProposedOpening,
    ProviderOpening,
)
from ouroboros.providers.base import Message, MessageRole

if TYPE_CHECKING:
    from ouroboros.bigbang.interview import InterviewEngine, InterviewState
    from ouroboros.interview_calibration import InterviewCalibration


def validate_opening_args(
    arguments: dict[str, Any], plugin: bool, *, completion_check: Any = None
) -> str | None:
    if arguments.get("opening_user_followup_question") is not None and (
        not arguments.get("session_id")
        or not arguments.get("answer")
        or arguments.get("last_question") is not None
        or any(
            arguments.get(key) is not None
            for key in (
                "initial_context",
                "interview_id",
                "calibration_input",
                "propose_opening_interpretations",
                "structured_first_question",
                "structured_first_batch",
                "selected_first_choice",
                "selected_first_choices",
                "selected_first_choices_text",
                "batch_confirm_digest",
                "batch_reject",
                "batch_answers",
                "batch_page_size",
            )
        )
        or (
            completion_check is not None
            and isinstance(arguments.get("answer"), str)
            and completion_check(arguments["answer"])
        )
    ):
        return "A user follow-up question requires a session and answer without last_question"
    flag = arguments.get("propose_opening_interpretations", False)
    if type(flag) is not bool:
        return "propose_opening_interpretations must be boolean"
    if flag and not arguments.get("initial_context") and arguments.get("session_id"):
        return "Opening interpretations are only available on start"
    if (
        flag
        and arguments.get("initial_context")
        and (
            plugin
            or any(
                arguments.get(key)
                for key in (
                    "structured_first_question",
                    "structured_first_batch",
                    "session_id",
                    "answer",
                    "calibration_input",
                )
            )
            or any(
                arguments.get(key) is not None
                for key in (
                    "selected_first_choice",
                    "selected_first_choices",
                    "selected_first_choices_text",
                )
            )
        )
    ):
        return "Opening interpretations require an in-process start without another presentation"
    return None


def plugin_interview_blocker(state: "InterviewState") -> str | None:
    """Reject incompatible sessions before plugin state or dispatch changes."""
    from ouroboros.mcp.tools.interview_batch import pending_batch

    if pending_batch(state):
        return "Pending batches require in-process handling"
    if state.proposed_opening is not None:
        return "Opening interpretations require in-process handling"
    return None


def safe_synthesis_question(state: "InterviewState", last_question: Any) -> str:
    """Keep model-proposed opening text out of synthesis provenance."""
    if state.proposed_opening is not None:
        if state.rounds and state.rounds[-1].user_response is None:
            return state.rounds[-1].question
        return "[driver safe-default finalization]"
    return last_question or "[driver safe-default finalization]"


def opening_followup_parameters() -> tuple[Any, ...]:
    from ouroboros.mcp.types import MCPToolParameter, ToolInputType

    return (
        MCPToolParameter(
            name="opening_user_followup_question",
            type=ToolInputType.STRING,
            description=(
                "For reopening an opening-interpretation interview with no pending question: "
                "the exact new follow-up question explicitly authored by the user. Never use a "
                "model proposal or a host-generated question. Do not combine with last_question."
            ),
            required=False,
        ),
    )


def validate_reopen_question(
    state: "InterviewState", last_question: Any, explicit_question: Any
) -> str | None:
    if state.proposed_opening is not None:
        proposed_texts = (
            value.casefold()
            for item in state.proposed_opening.interpretations
            for value in (item.interpretation, item.evidence, item.uncertainty)
        )
        if (
            last_question is not None
            or not isinstance(explicit_question, str)
            or not explicit_question.strip()
            or len(explicit_question) > 2000
            or any(
                value == explicit_question.strip().casefold()
                or (len(value) >= 8 and value in explicit_question.casefold())
                for value in proposed_texts
            )
        ):
            return (
                "To reopen this session, supply only the person's exact new question as "
                "opening_user_followup_question; do not use a model proposal."
            )
    elif explicit_question is not None:
        return "opening_user_followup_question requires an opening-interpretation session"
    elif not last_question:
        return (
            "Cannot record answer - the previous round is already answered and no follow-up "
            "question was provided. When reopening a completed interview (Seed-ready challenge), "
            "pass the new probe question as 'last_question' alongside 'answer'."
        )
    return None


async def opening_question(
    engine: "InterviewEngine", state: "InterviewState", calibration: "InterviewCalibration | None"
) -> Result[str, ProviderError | ValidationError]:
    from ouroboros.mcp.tools.interview_calibration import _ask_next_question

    result = await generate_opening(engine, state, calibration)
    if result.is_err:
        return Result.err(result.error)
    if result.value is None:
        return await _ask_next_question(engine, state, calibration)
    state.proposed_opening = result.value
    return Result.ok(OPENING_QUESTION)


async def generate_opening(
    engine: "InterviewEngine", state: "InterviewState", calibration: "InterviewCalibration | None"
) -> Result[ProposedOpening | None, ProviderError | ValidationError]:
    """Use one provider call; None preserves the existing length-guard path."""
    if state.rounds or engine.question_candidate_panel:
        return Result.err(ValidationError("Opening interpretations are unavailable"))
    prepared_result = engine.prepare_next_question(state, language_calibration=calibration)
    if prepared_result.is_err:
        return Result.err(prepared_result.error)
    prepared = prepared_result.value
    if prepared.immediate_question is not None:
        return Result.ok(None)
    assert prepared.config is not None
    messages = list(prepared.messages)
    messages.append(Message(role=MessageRole.USER, content=OPENING_INSTRUCTION))
    result = await engine._require_llm_adapter().complete(messages, prepared.config)
    if result.is_err:
        return Result.err(result.error)
    try:
        proposal = ProviderOpening.model_validate_json(result.value.content)
    except ValueError:
        return Result.err(ValidationError("Invalid opening interpretations"))
    return Result.ok(ProposedOpening.from_provider(proposal))
