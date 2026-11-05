"""Explicit, revision-bound decisions on opening interpretations."""

import re
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ouroboros.core.security import InputValidator
from ouroboros.core.types import Result
from ouroboros.interview_adapters.opening_interpretations import (
    NEITHER_PROMPT,
    OpeningDecisionEvent,
    confirmation_prompt,
    confirmed_interpretations,
    opening_digest,
    opening_revision,
)
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult

if TYPE_CHECKING:
    from ouroboros.mcp.tools.authoring_handlers import InterviewHandler


def opening_meta(state: Any) -> dict[str, Any]:
    opening = state.proposed_opening
    if opening is None:
        return {}
    return {
        "opening_interpretations": opening.model_dump(mode="json"),
        "opening_revision": opening_revision(opening, state.opening_decisions),
        "opening_confirmed_item_ids": [
            item.item_id for item in confirmed_interpretations(opening, state.opening_decisions)
        ],
        "opening_confirmation_prompts": {
            item.item_id: confirmation_prompt(item) for item in opening.interpretations
        },
        "opening_neither_prompt": NEITHER_PROMPT,
    }


def has_confirmed_opening(state: Any) -> bool:
    return bool(confirmed_interpretations(state.proposed_opening, state.opening_decisions))


def plugin_seed_blocker(state: Any) -> str | None:
    from ouroboros.mcp.tools.interview_batch import pending_batch

    if pending_batch(state):
        return "Answer or discard the pending batch before generating a Seed"
    if has_confirmed_opening(state):
        return "Use in-process Seed generation for confirmed opening interpretations"
    return None


def render_opening(state: Any, question: str, fallback: str) -> str:
    if state.proposed_opening is None or len(state.rounds) > 1:
        return fallback
    return state.proposed_opening.render(question, state.opening_decisions)


def _error(message: str) -> Result[MCPToolResult, MCPServerError]:
    return Result.err(MCPToolError(message, tool_name="ouroboros_interview"))


async def dispatch_opening_decision(
    handler: "InterviewHandler", arguments: dict[str, Any]
) -> Result[MCPToolResult, MCPServerError] | None:
    """Record one explicit host-reported decision or reject an ambiguous request."""
    keys = {
        "opening_revision",
        "opening_confirm_item_id",
        "opening_user_confirmed",
        "opening_neither",
        "opening_confirmation_text",
        "opening_confirmation_prompt",
    }
    if not any(arguments.get(key) is not None for key in keys):
        return None
    allowed = keys | {"session_id", "_normalized_interview_turn_context"}
    if any(key not in allowed and value is not None for key, value in arguments.items()):
        return _error("Opening decisions cannot be combined with other interview inputs")
    session_id = arguments.get("session_id")
    if not isinstance(session_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id) is None:
        return _error("A valid session is required")
    revision = arguments.get("opening_revision")
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{64}", revision) is None:
        return _error("The current opening revision is required")
    item_id = arguments.get("opening_confirm_item_id")
    user_confirmed = arguments.get("opening_user_confirmed")
    neither = arguments.get("opening_neither")
    user_response = arguments.get("opening_confirmation_text")
    presented_prompt = arguments.get("opening_confirmation_prompt")
    if not isinstance(user_response, str) or len(user_response) > 2000 or not user_response.strip():
        return _error("The person's exact decision text is required")
    valid, _ = InputValidator.validate_user_response(user_response)
    if not valid:
        return _error("Invalid decision text")
    if neither is True and item_id is None and user_confirmed is None:
        kind = "neither"
    elif neither is None and isinstance(item_id, str) and user_confirmed is True:
        kind = "confirm_item"
    else:
        return _error("Provide one explicit item confirmation or neither")

    if user_response != ("NEITHER" if kind == "neither" else "YES"):
        return _error("Reply exactly YES to confirm or NEITHER to reject both")

    from ouroboros.mcp.tools.subagent import should_dispatch_via_plugin

    if should_dispatch_via_plugin(handler.agent_runtime_backend, handler.opencode_mode):
        return _error("Opening decisions require in-process mode")
    engine, _ = handler._create_interview_engine()
    loaded = await engine.load_state(session_id)
    if loaded.is_err:
        return _error("Could not load opening session")
    state = loaded.value
    opening = state.proposed_opening
    if state.interview_id != session_id or opening is None:
        return _error("No opening interpretations are available")
    if state.is_complete or len(state.rounds) != 1 or state.rounds[0].user_response is not None:
        return _error("Opening decisions require the pending first question")
    if kind == "confirm_item" and item_id not in {item.item_id for item in opening.interpretations}:
        return _error("Unknown opening item")
    expected_prompt = (
        NEITHER_PROMPT
        if kind == "neither"
        else confirmation_prompt(
            next(item for item in opening.interpretations if item.item_id == item_id)
        )
    )
    if presented_prompt != expected_prompt:
        return _error("Show the exact confirmation prompt before recording a decision")
    current = opening_revision(opening, state.opening_decisions)
    if revision != current:
        # A retry of exactly the last committed operation is safe. A decision
        # after it changes the revision, so an older retry is rejected.
        if state.opening_decisions:
            last = state.opening_decisions[-1]
            if (
                last.previous_revision == revision
                and last.kind == kind
                and last.item_id == item_id
                and last.user_response == user_response
                and last.presented_prompt == presented_prompt
            ):
                return Result.ok(_decision_result(state, last))
        return _error("Opening changed; resume before retrying")
    if len(state.opening_decisions) >= 100:
        return _error("Opening decision limit reached")
    event = OpeningDecisionEvent(
        event_id=uuid4().hex,
        kind=kind,
        item_id=item_id,
        user_response=user_response,
        presented_prompt=expected_prompt,
        opening_digest=opening_digest(opening),
        previous_revision=current,
    )
    state.opening_decisions += (event,)
    state.invalidate_requirement_distillation()
    state.clear_stored_ambiguity()
    saved = await engine.save_state(state)
    if saved.is_err:
        return _error("Opening decision could not be saved; resume before retrying")
    return Result.ok(_decision_result(state, event))


def _decision_result(state: Any, event: OpeningDecisionEvent) -> MCPToolResult:
    return MCPToolResult(
        content=(
            MCPContentItem(
                type=ContentType.TEXT,
                text=(
                    f"Opening decision saved. Session ID: {state.interview_id}\n"
                    f"Opening revision: {opening_revision(state.proposed_opening, state.opening_decisions)}\n"
                    "Continue by answering the pending clarification question in your own words."
                ),
            ),
        ),
        meta={
            "session_id": state.interview_id,
            "opening_event_id": event.event_id,
            **opening_meta(state),
        },
    )
