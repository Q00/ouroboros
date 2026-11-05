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


def validate_opening_args(arguments: dict[str, Any], plugin: bool) -> str | None:
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
