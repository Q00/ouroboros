"""Optional, non-authoritative draft of explicitly supplied material."""

import re
from typing import TYPE_CHECKING

from ouroboros.core.file_lock import file_lock
from ouroboros.interview_adapters.first_question import _safe_single_line
from ouroboros.interview_adapters.inline_materials import InlineMaterial, MaterialDraft
from ouroboros.mcp.types import ContentType, MCPContentItem

if TYPE_CHECKING:
    from ouroboros.bigbang.interview import InterviewEngine, InterviewState


def material_followup_error(state: "InterviewState", echo: object, question: object) -> str | None:
    """Require the host to assert the person authored a new reopen question."""
    source = state.inline_material
    if source is None or state.proposed_opening is not None:
        return "Material follow-up is unavailable for this session"
    if (
        echo is not None
        or not isinstance(question, str)
        or not question.strip()
        or len(question) > 2000
    ):
        return "Use the person's exact authored question, not a material draft or host suggestion"
    return None


async def draft_inline_material(
    engine: "InterviewEngine", state: "InterviewState", *, retry: bool = False
) -> tuple["InterviewState", str]:
    """Best-effort draft; failed summaries never block the saved interview answer.

    State is already persisted before the provider call. A pending draft is not
    retried implicitly: a cancelled call must not duplicate provider work.
    """
    lock_target = engine._state_file_path(state.interview_id).with_suffix(".material")
    try:
        with file_lock(lock_target, exclusive=True, blocking=False):
            latest = await engine.load_state(state.interview_id)
            if latest.is_err:
                return state, "error"
            state = latest.value
            source = state.inline_material
            if source is None:
                return state, "error"
            if source.status == "ready":
                return state, "ready"
            if source.status == "unavailable" and not retry:
                return state, "ready"
            await _draft_under_lock(engine, state)
            latest = await engine.load_state(state.interview_id)
            return (latest.value, "ready") if latest.is_ok else (state, "error")
    except BlockingIOError:
        return state, "busy"
    except OSError:
        return state, "error"


async def _draft_under_lock(engine: "InterviewEngine", state: "InterviewState") -> None:
    source = state.inline_material
    assert source is not None
    draft = _local_excerpts(source.text, state.initial_context)
    status = "ready" if draft else "unavailable"
    state.inline_material = InlineMaterial(text=source.text, status=status, draft=draft)
    state.mark_updated()
    # A conflict means the earlier source/answer remains durable. Never clobber
    # a newer state or turn a best-effort draft into an interview gate.
    saved = await engine.save_state(state)
    if saved.is_err:
        state.inline_material = source


def _local_excerpts(text: str, context: str) -> MaterialDraft | None:
    """Rank literal source lines by word overlap without invoking any provider."""
    context_words = {word for word in re.findall(r"\w+", context.casefold()) if len(word) > 1}
    ranked: list[tuple[int, int, str]] = []
    for index, line in enumerate(text.splitlines()):
        line = line.strip()
        if not line or not _safe_single_line(line):
            continue
        words = set(re.findall(r"\w+", line.casefold()))
        # Select a literal window around the first matching source word, so
        # late matches influence ranking and remain visible in the excerpt.
        start = 0
        for match in re.finditer(r"\w+", line):
            if context_words.intersection(re.findall(r"\w+", match.group().casefold())):
                start = max(0, match.start() - 200)
                break
        excerpt = line[start : start + 400].strip()
        ranked.append((-len(context_words & words), index, excerpt))
    ranked.sort()
    excerpts = tuple(dict.fromkeys(excerpt for _, _, excerpt in ranked))[:5]
    return (
        MaterialDraft(origin="local_excerpts", source_quotes=excerpts, inferred_assumptions=())
        if excerpts
        else None
    )


def material_content(state: "InterviewState") -> tuple[MCPContentItem, ...]:
    """Render separately from the question, including for text-only MCP hosts."""
    source = state.inline_material
    if source is None:
        return ()
    lines = [
        "Optional material excerpts (untrusted source; not user intent or verified truth):",
    ]
    if source.draft is None:
        lines.append(
            "No displayable excerpt is available or extraction is pending. "
            "The interview may continue, and the material remains in local state."
        )
    else:
        if source.draft.origin == "local_excerpts":
            lines.append(
                "Literal lines ranked by word overlap with the initial request; "
                "if none match, source order is used. Lines may be truncated:"
            )
        else:
            lines.append("Literal excerpts from an earlier model-assisted draft:")
        lines.extend(f"- {quote}" for quote in source.draft.source_quotes)
        if source.draft.origin == "local_excerpts":
            lines.append("No model assumptions were generated for these excerpts.")
        else:
            lines.append("Earlier model assumptions (unconfirmed, not requirements):")
            lines.extend(f"- {item}" for item in source.draft.inferred_assumptions)
    return (MCPContentItem(type=ContentType.TEXT, text="\n".join(lines)),)


def material_argument_error(arguments: dict, plugin: bool) -> str | None:
    if arguments.get("inline_material_user_followup_question") is None:
        return None
    if plugin:
        return "Material follow-up requires in-process handling"
    if (
        not arguments.get("session_id")
        or not arguments.get("answer")
        or arguments.get("last_question") is not None
        or any(
            arguments.get(key) is not None
            for key in (
                "initial_context",
                "batch_answers",
                "batch_answers_text",
                "batch_reject",
                "inline_material_text",
                "inline_material_retry",
                "opening_user_followup_question",
                "calibration_input",
            )
        )
    ):
        return "A material follow-up requires only a session, user question and answer"
    return None


def validate_material_answer(
    state: "InterviewState", last_question: object, explicit: object, completion: bool
) -> tuple[object, str | None]:
    from ouroboros.interview_adapters import normalized_question_key

    pending = bool(state.rounds and state.rounds[-1].user_response is None)
    if explicit is not None and (state.inline_material is None or not state.rounds or pending):
        return (
            last_question,
            "A material follow-up requires a completed question in a material session",
        )
    if state.inline_material is None:
        return last_question, None
    if state.rounds and not pending and not completion:
        if error := material_followup_error(state, last_question, explicit):
            return last_question, error
    if completion:
        return (state.rounds[-1].question if pending else None), None
    if pending and last_question is not None:
        issued = state.rounds[-1].question
        if not isinstance(last_question, str) or normalized_question_key(
            last_question
        ) != normalized_question_key(issued):
            return (
                last_question,
                "The displayed material is not a question. Resume and answer the stored question.",
            )
        return issued, None
    return last_question, None
