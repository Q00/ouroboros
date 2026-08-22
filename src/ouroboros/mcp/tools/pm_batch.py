"""Batched PM turns (RFC #2222): pending state, member routing, responses.

A batched turn puts one to three questions on the table at once. What this
module owns is everything that is *about the batch* rather than about one
question: where pending members live, how an incoming answer is matched to its
member, how a batched turn renders to the host, and the answer lock a turn with
several answerable questions is the first shape to need.

Batch pending state lives in PM meta, never as core question-only rounds. The
engine's ``record_answer`` fills the *trailing* unanswered round and overwrites
its question text, so two pending rounds in core state would let an
out-of-order answer destroy a question silently. One entry per pending
question, each carrying the classification its skip sentinel is guarded by;
every member is recorded question-and-answer together at answer time.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog

from ouroboros.core.types import Result
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult

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

    Recording an answer reads the interview state and PM meta, then writes
    both. Two answers that interleave therefore both report success while the
    second write carries a state that never saw the first: the user's words are
    gone and their question comes back as still pending.

    A batched turn is the first shape where this is reachable in ordinary use —
    a host holding three answerable questions can send two answers as parallel
    tool calls — but the read-modify-write is shared by the in-process batch
    and single-question paths alike, so the lock is taken once around the call
    rather than around the batch branch. Locks are keyed by interview and live
    on the handler, which the server builds once at startup; the same idiom as
    the execution handler's ``_idempotency_locks``.

    The call without an answer is deliberately not exempted, though it looks
    read-only and holding the lock through the next turn's generation — a real
    LLM call — makes a timed-out host's retry queue behind it. It only reads
    while something is pending: with no pending batch and no unanswered round
    it plans the next turn and persists it, which is the same read-modify-write
    under another name. Queuing that retry is the point; letting it plan
    concurrently is the defect this closes.

    What this does not cover, so it is not mistaken for more: the plugin
    dispatch branch records answers on its own path, and a data directory is a
    user-global location that several server processes can open at once. An
    ``asyncio.Lock`` orders coroutines in one process; it does not order
    processes.
    """
    async with locks.setdefault(session_id, asyncio.Lock()):
        yield


def pending_batch_entries(meta: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the still-unanswered members of a batched turn."""
    entries = (meta or {}).get("pending_batch")
    if not isinstance(entries, list):
        return []
    return [
        entry for entry in entries if isinstance(entry, dict) and str(entry.get("question") or "")
    ]


def batch_entries_for_turns(turns: list[Any]) -> list[dict[str, Any]]:
    """Project planned turns into persistable pending-batch entries."""
    return [
        {
            "question": turn.question,
            "classification": turn.classification.output_type.value,
            "skip_eligible": turn.classification.output_type.value in ("decide_later", "deferred"),
        }
        for turn in turns
    ]


def resolve_batch_member(
    pending_batch: list[dict[str, Any]],
    last_question: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Match an incoming answer to its pending member.

    Returns ``(member, None)`` on a match and ``(None, error_message)`` when
    the answer cannot be filed — several members pending and none named, or a
    named question that is not one of them. Refusing is what keeps an answer
    from being recorded under a question the user was not looking at.
    """
    pending_list = "\n".join(f"- {e.get('question')}" for e in pending_batch)
    if last_question:
        target = next(
            (e for e in pending_batch if e.get("question") == last_question),
            None,
        )
        if target is None:
            return None, (
                "last_question does not match any pending question of this "
                f"turn. Pending:\n{pending_list}"
            )
        return target, None
    if len(pending_batch) == 1:
        return pending_batch[0], None
    return None, (
        "Several questions from this turn are pending. Pass the question this "
        f"answer belongs to as 'last_question'. Pending:\n{pending_list}"
    )


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


async def record_member_answer(
    engine: Any,
    state: Any,
    member: dict[str, Any],
    answer: str,
) -> Any:
    """Record one batch member's answer, honouring its own skip sentinel.

    The sentinel is guarded by the *member's* classification — a
    ``[decide_later]`` sent for a passthrough member records as a plain answer
    rather than silently discarding data, exactly as the single-question guard
    does with the last classification.

    A member whose decision is already in the interview state is not recorded
    again. Two files carry one turn — the state holds the decisions, the PM
    meta holds who is still pending — and the state is written first, so a
    failed or interrupted meta write leaves a member marked pending whose
    answer is already durable. The host sees an error and retries, and without
    this the retry would put a second round under the same question. Reading
    the decision back from the state is what makes the retry idempotent: the
    caller goes on to persist the pending list without this member, so the
    retry repairs the metadata instead of duplicating the decision.

    The comparison is deliberately narrow — the same answer, or a sentinel,
    which carries no words of the user's to lose. A question repeated verbatim
    later in the interview and answered differently is a new decision and is
    recorded as one.
    """
    question = str(member.get("question"))
    classification = member.get("classification")
    stripped = answer.strip()
    sentinel = (stripped == "[decide_later]" and classification == "decide_later") or (
        stripped == "[deferred]" and classification == "deferred"
    )
    recorded = next(
        (r for r in state.rounds if r.question == question and r.user_response is not None),
        None,
    )
    if recorded is not None and (sentinel or recorded.user_response == answer):
        log.info("pm.batch_member_already_recorded", question=question[:100])
        return Result.ok(state)
    skipped = False
    if stripped == "[decide_later]" and classification == "decide_later":
        result = await engine.skip_as_decide_later(state, question)
        skipped = True
    elif stripped == "[deferred]" and classification == "deferred":
        result = await engine.skip_as_deferred(state, question)
        skipped = True
    else:
        result = await engine.record_response(state, answer, question)
    if skipped and result.is_ok:
        result.value.clear_stored_ambiguity()
    return result


def _batch_skip_hint(entry: dict[str, Any], session_id: str) -> str:
    """Return the skip hint for one batch member, or an empty string."""
    classification = entry.get("classification")
    question = str(entry.get("question") or "")
    if classification == "decide_later":
        return (
            '  💡 May be skipped: pass answer="[decide_later]" with '
            f'last_question="{question}" and session_id="{session_id}".'
        )
    if classification == "deferred":
        return (
            '  💡 May be deferred to the dev phase: pass answer="[deferred]" '
            f'with last_question="{question}" and session_id="{session_id}".'
        )
    return ""


def _numbered_questions(session_id: str, entries: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for index, entry in enumerate(entries, 1):
        lines.append(f"{index}. {entry.get('question')}")
        hint = _batch_skip_hint(entry, session_id)
        if hint:
            lines.append(hint)
    return lines


def batch_pending_meta(
    session_id: str,
    remaining: list[dict[str, Any]],
    *,
    decide_later_count: int,
) -> dict[str, Any]:
    """Build the response meta for a turn whose batch is partially answered."""
    first = remaining[0]
    return {
        "session_id": session_id,
        "input_type": "freeText",
        "response_param": "answer",
        "question": first.get("question"),
        "question_batch": [dict(entry) for entry in remaining],
        "is_complete": False,
        "interview_complete": False,
        "classification": first.get("classification"),
        "skip_eligible": bool(first.get("skip_eligible")),
        "deferred_this_round": [],
        "decide_later_this_round": [],
        "new_deferred": [],
        "new_decide_later": [],
        "deferred_count": 0,
        "decide_later_count": decide_later_count,
    }


def batch_pending_result(
    session_id: str,
    remaining: list[dict[str, Any]],
    *,
    decide_later_count: int,
    diff: dict[str, Any] | None = None,
) -> MCPToolResult:
    """Render the still-pending members of a batch for the host.

    Advisory lanes were dispatched when the batch was issued, so a re-display
    carries none — dispatching again would multiply fan-outs for questions
    whose lanes already ran.
    """
    meta = batch_pending_meta(session_id, remaining, decide_later_count=decide_later_count)
    if diff:
        meta["deferred_this_round"] = diff["new_deferred"]
        meta["decide_later_this_round"] = diff["new_decide_later"]
        meta.update(diff)
    lines = [
        f"Session {session_id}",
        "",
        f"{len(remaining)} question(s) from this turn still await an answer. "
        "Answer each with its own call, passing the question text as "
        "'last_question'. Evidence lanes for these questions were already "
        "dispatched with the turn — do not dispatch them again.",
        "",
        *_numbered_questions(session_id, remaining),
    ]
    return MCPToolResult(
        content=(MCPContentItem(type=ContentType.TEXT, text="\n".join(lines)),),
        is_error=False,
        meta=meta,
    )


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
        f"This turn asks {len(batch_entries)} independent questions. Answer "
        "each with its own call, passing the question text as "
        "'last_question'. Answer in any order; unanswered ones stay pending.",
        "",
        *_numbered_questions(session_id, batch_entries),
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
    "batch_pending_meta",
    "batch_pending_result",
    "batch_turn_meta_and_text",
    "externalize_advisory_payloads",
    "pending_batch_entries",
    "resolve_batch_member",
]
