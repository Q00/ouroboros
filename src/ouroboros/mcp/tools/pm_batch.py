"""Batched PM turns (RFC #2222): a turn is asked whole and answered whole.

A batched turn puts one to three questions on the table at once. What this
module owns is everything that is *about the turn* rather than about one
question: how a turn renders to the host, how its answers come back, and how
they are recorded.

**A turn is atomic** (RFC #2222 revision 4). Nothing is persisted when a turn
is asked — no question-only round, no pending-member list — and a turn's
answers arrive together, so a round is written only when it is whole. That is
what removes the seam three review rounds kept finding: a pending list beside
the transcript is one fact in two places, and an interleaved write, an
interrupted one, or a question whose text recurs each turned that gap into a
lost or duplicated decision. There is no gap to guard now. A call that carries
no answers is a host that lost its turn; the next turn is planned from the
transcript rather than restored.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog

from ouroboros.core.types import Result

log = structlog.get_logger()

#: Store kind for externalized lane briefs. Not ``question_advisory``, so the
#: recent-findings reuse query never offers a brief as a finding.
ADVISORY_PROMPT_BUNDLE_KIND = "advisory_prompt_bundle"

#: What a lane child replies when it cannot fetch its externalized brief. The
#: host submits that lane as ``undispatched`` — a lane without its brief has
#: no contract to answer under, and guessing one is worse than absence.
UNDISPATCHED_SENTINEL = "UNDISPATCHED"


@asynccontextmanager
async def interview_answer_lock(
    locks: dict[str, asyncio.Lock],
    session_id: str,
) -> AsyncIterator[None]:
    """Hold one interview's answer lock for a whole record call.

    A turn is one write now, but two calls for one interview can still be in
    flight — a host that retried, two sessions on one id. Each reads the
    interview state and writes it back, so interleaved they both report success
    while the later write carries a state that never saw the earlier one, and
    those decisions are gone.

    The call carrying no answers is not exempted. It looks read-only and it
    holds the lock through the next turn's generation, a real LLM call, so a
    timed-out host's retry queues behind it — but it plans a turn and persists
    what that costs, which is the same read-modify-write under another name.
    Queuing the retry is the point; letting it plan concurrently is the defect
    this closes.

    Locks are keyed by interview and live on the handler, which the server
    builds once at startup; the same idiom as the execution handler's
    ``_idempotency_locks``.

    What this does not cover, so it is not mistaken for more: the plugin
    dispatch branch records answers on its own path, and a data directory is a
    user-global location that several server processes can open at once. An
    ``asyncio.Lock`` orders coroutines in one process; it does not order
    processes.
    """
    async with locks.setdefault(session_id, asyncio.Lock()):
        yield


def turn_answers(
    answers: Any,
    answer: str | None,
    last_question: str | None,
) -> tuple[list[tuple[str, str]], str | None]:
    """Normalize one call's answers into ``(question, answer)`` pairs.

    A turn's answers arrive together (RFC #2222 decision 2), so this is the
    only shape the recorder ever sees: each pair carries its own question and
    nothing is matched against anything the server remembered. ``answers`` is
    the batch spelling — a list of ``{question, answer}`` — and
    ``answer``/``last_question`` is the single-question one.

    An answer without its question is refused rather than filed against
    whatever round happens to be last. Nothing is persisted when a turn is
    asked, so there is no remembered question to fall back on, and guessing
    would write a decision under a question nobody was looking at.

    Returns ``(pairs, error)``; an empty pair list with no error means this
    call carries no answers, which is a reconnect and plans a fresh turn.
    """
    if isinstance(answers, list) and answers:
        pairs: list[tuple[str, str]] = []
        for entry in answers:
            if not isinstance(entry, dict):
                return [], "Each item of 'answers' must be an object with 'question' and 'answer'."
            question = str(entry.get("question") or "").strip()
            text = entry.get("answer")
            if not question or not isinstance(text, str) or not text.strip():
                return [], (
                    "Each item of 'answers' needs a non-empty 'question' and 'answer'. "
                    "Every answer names the question it belongs to."
                )
            pairs.append((question, text))
        return pairs, None
    if answer:
        if not last_question:
            return [], (
                "Pass the question this answer belongs to as 'last_question', or send the "
                "turn's answers together as 'answers': [{question, answer}]."
            )
        return [(last_question, answer)], None
    return [], None


async def record_turn_answers(engine: Any, state: Any, pairs: list[tuple[str, str]]) -> Any:
    """Record a turn's answers, each as a whole question-and-answer round.

    A skip sentinel is honoured wherever it arrives. The server no longer
    keeps a record of which questions it offered as skippable — that record
    was the pending state this design removed — so there is nothing to check
    the sentinel against. Honouring it costs a deferral recorded for a
    question that may not have been offered as deferrable; refusing it would
    write a control token into the transcript as though the PM had typed it.
    The first loses no words of the user's, the second invents some.
    """
    for question, answer in pairs:
        stripped = answer.strip()
        skipped = stripped in ("[decide_later]", "[deferred]")
        if stripped == "[decide_later]":
            result = await engine.skip_as_decide_later(state, question)
        elif stripped == "[deferred]":
            result = await engine.skip_as_deferred(state, question)
        else:
            result = await engine.record_response(state, answer, question)
        if result.is_err:
            return result
        state = result.value
        if skipped:
            state.clear_stored_ambiguity()
    return Result.ok(state)


def batch_entries_for_turns(turns: list[Any]) -> list[dict[str, Any]]:
    """Project planned turns into the entries a turn's response shows."""
    return [
        {
            "question": turn.question,
            "classification": turn.classification.output_type.value,
            "skip_eligible": turn.classification.output_type.value in ("decide_later", "deferred"),
        }
        for turn in turns
    ]


def skip_hint_suffix(classification: str | None, session_id: str) -> str:
    """Return the single-question skip hint for a response text, or ``""``.

    Shared by every turn shape that shows one question at a time — start,
    reconnect, and the single-question response — which carried three verbatim
    copies of these sentences before batching made a fourth unaffordable.
    """
    if classification == "decide_later":
        return (
            "\n\n💡 This question can be deferred. "
            'The user may answer now, or choose "decide later" to skip it. '
            "If they choose to decide later, pass "
            f'answer="[decide_later]" with session_id="{session_id}".'
        )
    if classification == "deferred":
        return (
            "\n\n💡 This is a technical question that can be deferred to the dev phase. "
            "The user may answer now, or choose to defer it. "
            "If they choose to defer, pass "
            f'answer="[deferred]" with session_id="{session_id}".'
        )
    return ""


def _batch_skip_hint(entry: dict[str, Any]) -> str:
    """Return the skip hint for one question of a turn, or an empty string."""
    classification = entry.get("classification")
    if classification == "decide_later":
        return '  💡 May be skipped: give this question the answer "[decide_later]".'
    if classification == "deferred":
        return '  💡 May be deferred to the dev phase: answer it "[deferred]".'
    return ""


def _numbered_questions(entries: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for index, entry in enumerate(entries, 1):
        lines.append(f"{index}. {entry.get('question')}")
        hint = _batch_skip_hint(entry)
        if hint:
            lines.append(hint)
    return lines


def batch_turn_meta_and_text(
    session_id: str,
    batch_entries: list[dict[str, Any]],
    advisories: list[dict[str, Any]],
    *,
    pending_reframe: dict[str, str] | None,
    diff: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Build the response meta and pre-dispatch text for a freshly issued batch.

    One advisory envelope per question, nested rather than merged: the fan-out
    stamps a fixed key namespace, so envelopes on one flat dict would overwrite
    each other's fanout ids and payloads. The caller appends each envelope's
    dispatch block to the returned text.
    """
    response_meta = {
        "session_id": session_id,
        "input_type": "freeText",
        "response_param": "answer",
        "question": batch_entries[0]["question"],
        "question_batch": [dict(e) for e in batch_entries],
        "question_advisories": advisories,
        "is_complete": False,
        "interview_complete": False,
        "classification": batch_entries[0]["classification"],
        "skip_eligible": batch_entries[0]["skip_eligible"],
        "pending_reframe": pending_reframe,
        "deferred_this_round": diff["new_deferred"],
        "decide_later_this_round": diff["new_decide_later"],
        **diff,
    }
    lines = [
        f"Session {session_id}",
        "",
        f"This turn asks {len(batch_entries)} independent questions. Put every "
        "answer to the user, then send them back together in one call: "
        f"'answers': [{{question, answer}}, ...] with session_id=\"{session_id}\". "
        "The turn is recorded as a whole; a call that arrives without them "
        "plans a new turn instead.",
        "",
        *_numbered_questions(batch_entries),
    ]
    return response_meta, "\n".join(lines)


def _payload_stub(payload: dict[str, Any], bundle_id: str) -> str:
    """Render the compact prompt a child receives when its brief is stored."""
    context = payload.get("context") or {}
    lane_id = context.get("lane_id")
    return f"""## Task
You are an Ouroboros PM interview advisory subagent — lane {lane_id}.

## PM Question
{context.get("question")}

## Session
- session_id: {context.get("session_id")}
- question_identity: {context.get("question_identity")}

## Your Brief
The full brief (rules, repository roster, answer contract) is stored, not
inlined. Fetch it with the MCP tool `ouroboros_fetch_artifact` (load it via
your runtime's tool discovery if deferred):
- contract_id: `{bundle_id}`
- lane_id: `{lane_id}`
Follow the fetched brief exactly — it defines your answer contract, and your
final message is only what its Output section specifies.
If the fetch fails, reply with exactly `{UNDISPATCHED_SENTINEL}` and nothing else."""


async def externalize_advisory_payloads(meta: dict[str, Any], findings_store: Any) -> None:
    """Store the lane briefs and leave references on the wire (RFC #2222).

    The same rule findings follow — bodies do not travel; ids do — applied to
    the plumbing itself. A turn's response used to carry each lane's full
    brief twice (meta and dispatch text) plus a copy of the tool's capability
    metadata per envelope, so one question cost ~120k characters and a batch
    outgrew what a host accepts inline, forcing hosts to improvise file
    surgery. After this, the response carries the questions and fetchable
    references; the briefs sit in the same store the children already fetch
    findings from.

    Fail-open on every edge: no store, no fanout id, or a publish that fails
    leaves the full prompts inline — today's behavior, oversized but whole.
    """
    fanout_id = meta.get("question_advisory_fanout_id")
    payloads = meta.get("question_advisory_subagents")
    if findings_store is None or not fanout_id or not isinstance(payloads, list) or not payloads:
        return
    bundle_id = f"advisory-prompts:{fanout_id}"
    body = {
        "kind": ADVISORY_PROMPT_BUNDLE_KIND,
        "result": {
            "aggregated_outputs": [
                {
                    "lane_id": (payload.get("context") or {}).get("lane_id"),
                    "output": payload.get("prompt"),
                }
                for payload in payloads
                if isinstance(payload, dict)
            ]
        },
    }
    try:
        from ouroboros.orchestrator.disposable_memory import DisposableMemory

        memory = DisposableMemory(artifact_store=findings_store)

        async def _work(_handle: Any) -> Any:
            return body

        await memory.run(
            intent="publish advisory prompt bundle",
            runtime_id="mcp:pm-advisory-prompts",
            work_fn=_work,
            contract_id=bundle_id,
        )
    except Exception as exc:
        log.warning(
            "pm_batch.prompt_bundle_publish_failed",
            fanout_id=fanout_id,
            error=str(exc),
        )
        return
    for payload in payloads:
        if isinstance(payload, dict) and payload.get("prompt"):
            payload["prompt"] = _payload_stub(payload, bundle_id)
    request = meta.get("question_advisory_request")
    if isinstance(request, dict):
        capability = request.get("mcp_tool_capability")
        if isinstance(capability, dict) and capability.get("tool_name"):
            # The only field any wire consumer reads; the rest is fetchable
            # from the capability registry by name.
            request["mcp_tool_capability"] = {"tool_name": capability["tool_name"]}
        # The payloads were already built from it, and they are the wire's
        # authority on what each lane is asked; a second copy of every lane's
        # answer contract is weight without a reader.
        request.pop("lanes", None)
    log.info(
        "pm_batch.advisory_payloads_externalized",
        fanout_id=fanout_id,
        bundle_id=bundle_id,
        payload_count=len(payloads),
    )


__all__ = [
    "ADVISORY_PROMPT_BUNDLE_KIND",
    "UNDISPATCHED_SENTINEL",
    "batch_entries_for_turns",
    "record_turn_answers",
    "turn_answers",
    "batch_turn_meta_and_text",
    "externalize_advisory_payloads",
]
