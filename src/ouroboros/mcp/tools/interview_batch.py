"""Opt-in first-turn interview batches. Proposals are never user decisions."""

import hashlib
import json
import os
import re
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import ValidationError as PydanticValidationError

from ouroboros.bigbang.interview import InterviewRound, InterviewState
from ouroboros.core.initial_context import resolve_initial_context_input
from ouroboros.core.types import Result
from ouroboros.interview_adapters.first_question import selected_answer, selected_answers
from ouroboros.interview_adapters.independent_batch import ProposedBatch, has_pending_batch
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.providers.base import Message, MessageRole

if TYPE_CHECKING:
    from ouroboros.mcp.tools.authoring_handlers import InterviewHandler

BATCH_INSTRUCTION = (
    "Return JSON only: an object with questions, a list of 3 to 5 mutually "
    "independent requirements questions. Each item has presentation, dimension, "
    "recommended_index and depends_on. presentation contains question, choices "
    "(2 to 4), direct_input_label, multi_select (boolean). dimension is one of "
    "goal, constraint, success, context, scope. recommended_index is a 1-based "
    "choice. depends_on must be an empty list. Each question must make sense "
    "regardless of answers to the others. Use the user's language. Choices and "
    "recommendations are provisional suggestions, not user decisions."
)


def pending_batch(state: InterviewState) -> bool:
    return has_pending_batch(state)


def batch_payload(state: InterviewState) -> tuple[str, list[dict[str, Any]]]:
    rounds = [r for r in state.rounds if r.batch_id is not None]
    if rounds:
        batch_ids = {r.batch_id for r in rounds}
        if any(
            not isinstance(bid, str) or re.fullmatch(r"[0-9a-f]{32}", bid) is None
            for bid in batch_ids
        ):
            raise ValueError("Invalid persisted batch")
        question_ids = [r.question_id for r in rounds]
        if (
            len(batch_ids) != 1
            or len(set(question_ids)) != len(question_ids)
            or any(
                not isinstance(r.question_id, str)
                or re.fullmatch(r"[0-9a-f]{32}", r.question_id) is None
                or r.first_question_presentation is None
                or r.question != r.first_question_presentation.question
                or r.ambiguity_dimension
                not in {"goal", "constraint", "success", "context", "scope"}
                or type(r.recommended_index) is not int
                or r.recommended_index < 1
                or r.recommended_index > len(r.first_question_presentation.choices)
                for r in rounds
            )
        ):
            raise ValueError("Invalid persisted batch")
    items = [
        {
            "question_id": r.question_id,
            "source": "model_proposal",
            "presentation": r.first_question_presentation.model_dump(mode="json")
            if r.first_question_presentation
            else None,
            "dimension": r.ambiguity_dimension,
            "recommended_index": r.recommended_index,
        }
        for r in rounds
    ]
    encoded = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest(), items


def batch_view(state: InterviewState) -> MCPToolResult:
    if re.fullmatch(r"interview_[0-9a-f]{16}", state.interview_id) is None:
        raise ValueError("Invalid batch session ID")
    digest, items = batch_payload(state)
    answered = [r.question_id for r in state.rounds if r.batch_id and r.user_response is not None]
    lines = [
        f"Session ID: {state.interview_id}",
        f"Batch digest: {digest}",
        "Review whether these proposed questions are independent before answering. "
        "Suggestions and recommendations are not decisions.",
    ]
    for number, item in enumerate(items, 1):
        presentation = item["presentation"]
        assert presentation is not None
        label = "answered" if item["question_id"] in answered else "pending"
        lines.append(
            f"\n{number}. {presentation['question']} [question_id={item['question_id']}; {label}]"
        )
        lines.extend(
            f"  {chr(97 + i)}. {choice}" for i, choice in enumerate(presentation["choices"])
        )
        recommended = chr(96 + item["recommended_index"])
        lines.append(f"  Suggested option: {recommended} (model proposal, not a decision)")
        lines.append(f"  {presentation['direct_input_label']}")
    return MCPToolResult(
        content=(MCPContentItem(type=ContentType.TEXT, text="\n".join(lines)),),
        meta={
            "session_id": state.interview_id,
            "batch_digest": digest,
            "questions": items,
            "answered_question_ids": answered,
            "independence_confirmed": state.batch_confirmed_digest == digest,
            "independence_source": "user_confirmation_required",
            "batch_complete": bool(items) and not pending_batch(state),
        },
    )


def batch_error(message: str) -> Result[MCPToolResult, MCPServerError]:
    return Result.err(MCPToolError(message, tool_name="ouroboros_interview"))


def recoverable_batch_error(session_id: str, message: str) -> Result[MCPToolResult, MCPServerError]:
    return Result.ok(
        MCPToolResult(
            content=(
                MCPContentItem(type=ContentType.TEXT, text=f"{message} Session ID: {session_id}"),
            ),
            is_error=True,
            meta={"session_id": session_id, "recoverable": True},
        )
    )


async def start_batch(
    handler: "InterviewHandler", arguments: dict[str, Any]
) -> Result[MCPToolResult, MCPServerError]:
    initial = arguments.get("initial_context")
    if not isinstance(initial, str) or not initial.strip():
        return batch_error("Initial context is required for a batch")
    allowed = {
        "structured_first_batch",
        "initial_context",
        "cwd",
        "_normalized_interview_turn_context",
    }
    if any(key not in allowed and value is not None for key, value in arguments.items()):
        return batch_error("Batch start cannot be combined with other interview inputs")
    cwd = arguments.get("cwd") or os.getcwd()
    if not isinstance(cwd, str):
        return batch_error("Invalid working directory")
    resolved = resolve_initial_context_input(initial, cwd=cwd)
    if resolved.is_err:
        return batch_error("Invalid initial context")
    engine, adapter = handler._create_interview_engine()
    result = await engine.start_interview(
        resolved.value, cwd=cwd, interview_id=f"interview_{uuid4().hex[:16]}"
    )
    if result.is_err:
        return batch_error("Could not create interview session")
    state = result.value
    prepared = engine.prepare_next_question(state)
    if (
        prepared.is_err
        or prepared.value.immediate_question is not None
        or prepared.value.config is None
    ):
        return recoverable_batch_error(
            state.interview_id, "Cannot prepare a batch. Resume the session for a single question."
        )
    messages = list(prepared.value.messages)
    messages.append(Message(role=MessageRole.USER, content=BATCH_INSTRUCTION))
    try:
        completion = await adapter.complete(messages, prepared.value.config)
    except Exception:
        return recoverable_batch_error(
            state.interview_id, "Batch generation failed. Resume the session for a single question."
        )
    if completion.is_err:
        return recoverable_batch_error(
            state.interview_id, "Batch generation failed. Resume the session for a single question."
        )
    try:
        batch = ProposedBatch.model_validate_json(completion.value.content)
    except (PydanticValidationError, ValueError):
        return recoverable_batch_error(
            state.interview_id, "Invalid batch proposal. Resume the session for a single question."
        )
    batch_id = uuid4().hex
    for item in batch.questions:
        state.rounds.append(
            InterviewRound(
                round_number=state.current_round_number,
                question=item.presentation.question,
                first_question_presentation=item.presentation,
                batch_id=batch_id,
                question_id=uuid4().hex,
                ambiguity_dimension=item.dimension,
                recommended_index=item.recommended_index,
            )
        )
    state.mark_updated()
    saved = await engine.save_state(state)
    if saved.is_err:
        return recoverable_batch_error(
            state.interview_id, "Batch could not be saved. Resume the session to inspect its state."
        )
    from ouroboros.events.interview import interview_started

    handler._emit_event_bg(interview_started(state.interview_id, resolved.value))
    return Result.ok(batch_view(state))


async def answer_batch(
    handler: "InterviewHandler", arguments: dict[str, Any]
) -> Result[MCPToolResult, MCPServerError]:
    allowed = {
        "session_id",
        "batch_answers",
        "batch_answers_text",
        "batch_confirm_digest",
        "batch_independence_confirmed",
        "_normalized_interview_turn_context",
    }
    if any(key not in allowed and value is not None for key, value in arguments.items()):
        return batch_error("Batch answers cannot be combined with other interview inputs")
    session_id = arguments.get("session_id")
    raw_answers = arguments.get("batch_answers")
    raw_text = arguments.get("batch_answers_text")
    if (
        not isinstance(session_id, str)
        or re.fullmatch(r"interview_[0-9a-f]{16}", session_id) is None
        or ((raw_answers is None) == (raw_text is None))
    ):
        return batch_error("Provide a session and exactly one batch answer format")
    if raw_answers is not None and (
        not isinstance(raw_answers, list) or not raw_answers or len(raw_answers) > 5
    ):
        return batch_error("One to five batch answers are required")
    if raw_text is not None and (not isinstance(raw_text, str) or len(raw_text) > 10000):
        return batch_error("Invalid batch answer text")
    engine, _ = handler._create_interview_engine()
    loaded = await engine.load_state(session_id)
    if loaded.is_err:
        return batch_error("Could not load batch session")
    state = loaded.value
    if state.interview_id != session_id:
        return batch_error("Invalid batch session")
    try:
        digest, _ = batch_payload(state)
    except ValueError:
        return batch_error("Invalid persisted batch")
    if (
        arguments.get("batch_confirm_digest") != digest
        or arguments.get("batch_independence_confirmed") is not True
    ):
        return batch_error("Confirm independence of the current batch before answering")
    batch_rounds = {r.question_id: r for r in state.rounds if r.batch_id and r.question_id}
    if not batch_rounds:
        return batch_error("No batch is present")
    if raw_text is not None:
        try:
            raw_answers = parse_batch_text(raw_text, list(batch_rounds))
        except ValueError:
            return batch_error("Invalid batch answer text")
    assert isinstance(raw_answers, list)
    normalized = []
    seen: set[str] = set()
    for raw in raw_answers:
        if not isinstance(raw, dict) or set(raw) - {"question_id", "answer", "selected_choices"}:
            return batch_error("Invalid batch answer")
        qid = raw.get("question_id")
        if not isinstance(qid, str) or qid not in batch_rounds or qid in seen:
            return batch_error("Unknown or duplicate batch question ID")
        seen.add(qid)
        round_data = batch_rounds[qid]
        has_answer, has_choices = "answer" in raw, "selected_choices" in raw
        if has_answer == has_choices:
            return batch_error("Provide exactly one answer or selection")
        selected = raw.get("selected_choices")
        if has_answer:
            answer = raw["answer"]
            if not isinstance(answer, str):
                return batch_error("Answer must be text")
        else:
            if not isinstance(selected, list) or any(type(i) is not int for i in selected):
                return batch_error("Invalid selection")
            try:
                if (
                    round_data.first_question_presentation
                    and round_data.first_question_presentation.multi_select
                ):
                    answer = selected_answers(round_data.first_question_presentation, selected)
                elif len(selected) == 1:
                    answer = selected_answer(round_data.first_question_presentation, selected[0])
                else:
                    return batch_error("This question requires a single selection")
            except ValueError:
                return batch_error("Invalid selection")
        from ouroboros.auto.intent_guard import IntentGuardStatus
        from ouroboros.mcp.tools.authoring_handlers import _guard_interview_answer

        guard = _guard_interview_answer(state=state, question=round_data.question, answer=answer)
        if guard.status is IntentGuardStatus.FAIL:
            return batch_error("Batch answer blocked by intent guard")
        if round_data.user_response is not None:
            expected_indices = tuple(selected) if has_choices else None
            if (
                round_data.user_response != answer
                or round_data.batch_selected_indices != expected_indices
            ):
                return batch_error("A different answer is already recorded")
            continue
        normalized.append((round_data, answer, selected if has_choices else None))
    # Work on an isolated snapshot so one invalid answer does not mutate the loaded state.
    working = state.model_copy(deep=True)
    for old_round, answer, selected in normalized:
        updated = await engine.record_response(
            working, answer, old_round.question, question_id=old_round.question_id
        )
        if updated.is_err:
            return batch_error("Invalid answer")
        if selected is not None:
            recorded = next(r for r in working.rounds if r.question_id == old_round.question_id)
            recorded.batch_selected_indices = tuple(selected)
            if (
                recorded.first_question_presentation
                and recorded.first_question_presentation.multi_select
            ):
                recorded.first_question_selected_indices = tuple(selected)
    if normalized or state.batch_confirmed_digest != digest:
        working.batch_confirmed_digest = digest
        saved = await engine.save_state(working)
        if saved.is_err:
            return batch_error("Batch changed or could not be saved; reload before retrying")
        state = working
        from ouroboros.events.interview import interview_response_recorded

        for old_round, answer, _selected in normalized:
            handler._emit_event_bg(
                interview_response_recorded(
                    state.interview_id,
                    old_round.round_number,
                    old_round.question,
                    answer,
                    question_id=old_round.question_id,
                    ambiguity_dimension=old_round.ambiguity_dimension,
                )
            )
    return Result.ok(batch_view(state))


async def reject_batch(
    handler: "InterviewHandler", arguments: dict[str, Any]
) -> Result[MCPToolResult, MCPServerError]:
    allowed = {
        "session_id",
        "batch_reject",
        "batch_confirm_digest",
        "_normalized_interview_turn_context",
    }
    if any(key not in allowed and value is not None for key, value in arguments.items()):
        return batch_error("Batch rejection cannot be combined with other interview inputs")
    session_id = arguments.get("session_id")
    if (
        not isinstance(session_id, str)
        or re.fullmatch(r"interview_[0-9a-f]{16}", session_id) is None
    ):
        return batch_error("A valid session is required")
    engine, _ = handler._create_interview_engine()
    loaded = await engine.load_state(session_id)
    if loaded.is_err:
        return batch_error("Could not load batch session")
    state = loaded.value
    if state.interview_id != session_id:
        return batch_error("Invalid batch session")
    try:
        digest, _ = batch_payload(state)
    except ValueError:
        return batch_error("Invalid persisted batch")
    if arguments.get("batch_confirm_digest") != digest:
        return batch_error("Batch changed; reload before rejecting")
    rounds = [r for r in state.rounds if r.batch_id]
    if not any(r.user_response is None for r in rounds):
        return batch_error("No pending batch questions to discard")
    state.rounds = [r for r in state.rounds if not r.batch_id or r.user_response is not None]
    state.batch_confirmed_digest = None
    state.mark_updated()
    saved = await engine.save_state(state)
    if saved.is_err:
        return batch_error("Batch changed or could not be saved; reload before retrying")
    return Result.ok(
        MCPToolResult(
            content=(
                MCPContentItem(
                    type=ContentType.TEXT,
                    text="Unanswered batch questions discarded. Recorded answers remain. Resume for one question at a time.",
                ),
            ),
            meta={"session_id": session_id, "batch_discarded": True},
        )
    )


def parse_batch_text(value: str, question_ids: list[str]) -> list[dict[str, Any]]:
    """Parse labelled choices and free text; JSON quotes disambiguate literal labels."""
    entries: list[dict[str, Any]] = []
    pos = 0
    while pos < len(value):
        match = re.match(r"\s*([1-5])\s*:\s*", value[pos:])
        if match is None:
            raise ValueError("Invalid question label")
        number = int(match.group(1))
        if number > len(question_ids):
            raise ValueError("Question out of range")
        pos += match.end()
        if pos < len(value) and value[pos] == '"':
            answer, consumed = json.JSONDecoder().raw_decode(value[pos:])
            if not isinstance(answer, str):
                raise ValueError("Expected text")
            item = {"question_id": question_ids[number - 1], "answer": answer}
            pos += consumed
        else:
            boundary = re.search(r"\s*/\s*(?=[0-9]+\s*:)", value[pos:])
            end = pos + boundary.start() if boundary else len(value)
            segment = value[pos:end].strip()
            if not segment or re.fullmatch(
                r"(?:-|[a-dA-D1-4](?:\s*,\s*[a-dA-D1-4])*)\s*/", segment
            ):
                raise ValueError("Invalid answer")
            tokens = [token.strip() for token in segment.split(",")]
            looks_like_selection = (
                segment == "-"
                or (len(tokens) > 1 and re.fullmatch(r"[a-zA-Z]|[0-9]+", tokens[0]))
                or all(re.fullmatch(r"[a-zA-Z]|[0-9]+", token) for token in tokens)
            )
            if looks_like_selection:
                if segment == "-":
                    indices = []
                elif all(re.fullmatch(r"[a-dA-D]", token) for token in tokens):
                    indices = [ord(token.lower()) - ord("a") + 1 for token in tokens]
                elif all(re.fullmatch(r"[1-4]", token) for token in tokens):
                    indices = [int(token) for token in tokens]
                else:
                    raise ValueError("Invalid selection")
                if len(indices) != len(set(indices)):
                    raise ValueError("Duplicate selection")
                item = {"question_id": question_ids[number - 1], "selected_choices": indices}
            else:
                item = {"question_id": question_ids[number - 1], "answer": segment}
            pos = end
        entries.append(item)
        if len(entries) > 5:
            raise ValueError("Too many answers")
        tail = value[pos:]
        if not tail.strip():
            break
        separator = re.match(r"\s*/\s*", tail)
        if separator is None:
            raise ValueError("Invalid separator")
        pos += separator.end()
        if pos == len(value):
            raise ValueError("Trailing separator")
    if not entries:
        raise ValueError("No answers")
    return entries


async def dispatch_batch_if_requested(
    handler: "InterviewHandler",
    arguments: dict[str, Any],
) -> Result[MCPToolResult, MCPServerError] | None:
    from ouroboros.mcp.tools.subagent import should_dispatch_via_plugin

    if (
        "structured_first_batch" in arguments
        and type(arguments["structured_first_batch"]) is not bool
    ):
        return batch_error("structured_first_batch must be boolean")
    if not (
        arguments.get("structured_first_batch") is True
        or "batch_answers" in arguments
        or "batch_answers_text" in arguments
        or "batch_reject" in arguments
    ):
        return None
    if should_dispatch_via_plugin(handler.agent_runtime_backend, handler.opencode_mode):
        return batch_error("Batches require in-process mode")
    if arguments.get("structured_first_batch") is True:
        return await start_batch(handler, arguments)
    if "batch_reject" in arguments:
        if (
            arguments.get("batch_reject") is not True
            or "batch_answers" in arguments
            or "batch_answers_text" in arguments
        ):
            return batch_error("Invalid batch rejection")
        return await reject_batch(handler, arguments)
    if any(
        arguments.get(key) is not None
        for key in ("answer", "selected_first_choice", "selected_first_choices")
    ):
        return batch_error("Batch answers cannot be combined with other answers")
    return await answer_batch(handler, arguments)
