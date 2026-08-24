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
import json
from typing import Any

import structlog

from ouroboros.bigbang.pm_interview import (
    DECIDE_LATER_PLACEHOLDER,
    DEFERRED_PLACEHOLDER,
)
from ouroboros.core.errors import ValidationError
from ouroboros.core.security import InputValidator
from ouroboros.core.types import Result
from ouroboros.orchestrator.capabilities.question_text import normalize_question_text

log = structlog.get_logger()

#: Store kind for externalized lane briefs. Not ``question_advisory``, so the
#: recent-findings reuse query never offers a brief as a finding.
ADVISORY_PROMPT_BUNDLE_KIND = "advisory_prompt_bundle"

#: What a lane child replies when it cannot do its work at all. The host
#: submits that lane as ``undispatched``, which is the honest record: a lane
#: that reports nothing found would be read as having found nothing.
#:
#: A failed brief fetch is no longer one of those cases. The stub carries the
#: schema, the roster and the offered findings, so a child that cannot reach
#: the store still knows what to look at and what shape to answer in.
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
    *,
    planned_questions: list[str] | None = None,
) -> tuple[list[tuple[str, str]], str | None]:
    """Validate and normalize one call's answers into question/answer pairs.

    ``answers`` is the batched spelling and accepts one to three strict
    ``{question: str, answer: str}`` objects. Question identity is normalized
    before duplicate checks, matching the producer's identity semantics.

    Most atomic turns intentionally are not persisted while they are on the
    wire, so their identities cannot be authenticated server-side. When a
    persisted question-only round does exist (for example, a legacy or plugin
    handoff), ``planned_questions`` closes that gap: the caller must answer
    exactly those identities and cannot substitute a question it invented.

    The singular and batched forms are mutually exclusive so neither can be
    silently discarded. An omitted ``answers`` and ``answer`` means reconnect.
    An explicitly malformed or empty batch is rejected rather than treated as
    reconnect.
    """
    if answers is not None and answer is not None:
        return [], ("'answer' and 'answers' are mutually exclusive; send exactly one answer form.")

    pairs: list[tuple[str, str]] = []
    if answers is not None:
        if not isinstance(answers, list):
            return [], "'answers' must be an array of one to three answer objects."
        if not 1 <= len(answers) <= 3:
            return [], "'answers' must contain between one and three answer objects."

        seen: set[str] = set()
        for entry in answers:
            if not isinstance(entry, dict):
                return [], "Each item of 'answers' must be an object with 'question' and 'answer'."
            if set(entry) != {"question", "answer"}:
                return [], ("Each item of 'answers' must contain only 'question' and 'answer'.")
            question = entry["question"]
            text = entry["answer"]
            if (
                not isinstance(question, str)
                or not question.strip()
                or not isinstance(text, str)
                or not text.strip()
            ):
                return [], (
                    "Each item of 'answers' needs a non-empty string 'question' and 'answer'. "
                    "Every answer names the question it belongs to."
                )
            identity = normalize_question_text(question)
            if identity in seen:
                return [], "'answers' contains a duplicate question identity."
            seen.add(identity)
            pairs.append((question.strip(), text))
    elif answer is not None:
        if not isinstance(answer, str) or not answer.strip():
            return [], "'answer' must be a non-empty string."
        if not isinstance(last_question, str) or not last_question.strip():
            return [], (
                "Pass the question this answer belongs to as 'last_question', or send the "
                "turn's answers together as 'answers': [{question, answer}]."
            )
        pairs = [(last_question.strip(), answer)]

    if pairs and planned_questions is not None:
        planned_identities = {normalize_question_text(question) for question in planned_questions}
        supplied_identities = {normalize_question_text(question) for question, _ in pairs}
        if supplied_identities != planned_identities:
            return [], (
                "Answer question identities must exactly match the persisted planned questions; "
                "caller-invented or missing questions are not accepted."
            )
    return pairs, None


async def record_turn_answers(engine: Any | None, state: Any, pairs: list[tuple[str, str]]) -> Any:
    """Record a turn's answers, each as a whole question-and-answer round.

    A skip sentinel is honoured wherever it arrives. The server no longer
    keeps a record of which questions it offered as skippable — that record
    was the pending state this design removed — so there is nothing to check
    the sentinel against. Honouring it costs a deferral recorded for a
    question that may not have been offered as deferrable; refusing it would
    write a control token into the transcript as though the PM had typed it.
    The first loses no words of the user's, the second invents some.

    ``engine`` is None on the runtime that dispatches to a child session.
    That runtime has no engine to reach — building one there would put an LLM
    adapter behind an answer being written down, on the very path that exists
    so the server need not hold one. What it must not have is a second reading
    of the same answer: whether a token is a skip, and what a skipped round
    then says, is decided here, above the fork. Below it lies only what an
    engine uniquely owns — the reframe map, and the open items it accumulates
    for the summary — and a runtime that plans no questions has neither.
    """
    for question, answer in pairs:
        stripped = answer.strip()
        placeholder = _SKIP_PLACEHOLDERS.get(stripped)
        if engine is not None:
            if stripped == "[decide_later]":
                result = await engine.skip_as_decide_later(state, question)
            elif stripped == "[deferred]":
                result = await engine.skip_as_deferred(state, question)
            else:
                result = await engine.record_response(state, answer, question)
        else:
            result = _record_without_engine(state, question, placeholder or answer)
        if result.is_err:
            return result
        state = result.value
        if placeholder is not None:
            state.clear_stored_ambiguity()
    return Result.ok(state)


#: The control tokens a turn's answer may be, and the round each one leaves.
_SKIP_PLACEHOLDERS = {
    "[decide_later]": DECIDE_LATER_PLACEHOLDER,
    "[deferred]": DEFERRED_PLACEHOLDER,
}


def _record_without_engine(state: Any, question: str, user_response: str) -> Any:
    """Write one round the way ``InterviewEngine.record_response`` writes it.

    The same validation guards the same field: a runtime does not get to
    accept a response the other refuses, any more than it gets to read a
    skip differently.
    """
    is_valid, error_msg = InputValidator.validate_user_response(user_response)
    if not is_valid:
        return Result.err(ValidationError(error_msg, field="user_response"))
    state.record_answer(question, user_response)
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
        # The batch relay parameter, named as itself: a generic host reading
        # this to build its reply would otherwise be told to send one answer to
        # a turn that asked several.
        "response_param": "answers",
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
        "One call records the turn. The server does not hold the questions "
        "between calls, so whatever you leave out is abandoned — the next call "
        "plans a new turn, and a question that still matters is asked again.",
        "",
        *_numbered_questions(batch_entries),
    ]
    return response_meta, "\n".join(lines)


def _without_prose(node: Any) -> Any:
    """Return a JSON Schema with its human-facing text removed.

    What the child needs from the schema is the shape it must produce — field
    names, required lists, enum literals, ``additionalProperties``. The
    descriptions explain that shape to a reader who has the brief; they are two
    thirds of its bytes and none of its meaning here.
    """
    if isinstance(node, dict):
        return {k: _without_prose(v) for k, v in node.items() if k not in ("description", "title")}
    if isinstance(node, list):
        return [_without_prose(item) for item in node]
    return node


def _lean_schema(contract: Any) -> str | None:
    """Return the child-facing schema of one lane's contract, or ``None``."""
    schema = contract.get("response_model_schema") if isinstance(contract, dict) else None
    if not isinstance(schema, dict):
        return None
    return json.dumps(_without_prose(schema), ensure_ascii=False, separators=(",", ":"))


#: What each contract asks for, said in words instead of as its schema.
#:
#: The schema is exact and nearly unreadable — a child reads ``oneOf`` branches
#: and regex patterns to work out that ``path`` is repository-relative. Naming
#: the fields and their bounds says the same in a fraction of the characters.
#:
#: Deliberately not a filled-in example. An example is a claim already written
#: in the answer's shape, and a child short on evidence has one in front of it
#: that only needs its identifiers changed — the fabricated-but-well-formed
#: finding is the one outcome this mechanism exists to prevent.
#:
#: Keyed by ``contract_id``, so a contract that changes version falls back to
#: rendering its schema rather than being described by text written for the
#: shape before it. ``tests/unit/mcp/tools/test_pm_handler_batch.py`` checks
#: that every field a contract requires is named here.
_ANSWER_SPECS: dict[str, str] = {
    "pm_code_context_answer.v2": """- `question_identity` and `lane_id` exactly as given above.
- `examined`: one entry per repository you opened — its `repo_id` from 3 and
  `policy_claims` of `{path, policy_claim, plain_statement}`. Read and found
  nothing: an entry with no claims. Never opened: no entry.
- Opened nothing at all: `examined: []` with {no_op}.
- `path` is relative to the repository.
- `plain_statement`: that claim once, in the question's language, no paths or
  identifiers.""",
    "data_evidence_answer.v1": """- `question_identity` and `lane_id` exactly as given above.
- Measured: `data_needed: true` and `read_requests`, each with
  `operation: "read"`, `tool_name`, `metric`, `aggregation`,
  `informs_decision`, and `values` — the numbers you read back. Optional:
  `filters` of `{field, comparator, value}`, `time_window`, `group_by`.
- Nothing to measure: `data_needed: false` with {no_op}.
- `aggregation`: count, distinct_count, sum, average, median, p90, p95, p99,
  min, max, rate. `comparator`: eq, neq, gt, gte, lt, lte.
- `values` is one entry, or one per group when you grouped.
- You carry aggregates — never a row, a name, or an identifier.""",
}


def _no_op_literals(schema_json: str) -> str:
    """Return the empty-state reason literals this lane's schema admits.

    Read out of the schema rather than listed here: a lane's reasons are its
    contract's to name, and a copy in this module would be a second list to
    keep in step.
    """
    try:
        schema = json.loads(schema_json)
    except ValueError:
        return ""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if (
                    key.endswith("_reason")
                    and isinstance(value, dict)
                    and isinstance(value.get("enum"), list)
                ):
                    found.append(f"`{key}`: {', '.join(str(v) for v in value['enum'])}")
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return found[0] if found else ""


def _answer_section(contract: Any, schema_json: str | None) -> str:
    """Return the ``## Answer`` block: the contract said in words, or its schema."""
    contract_id = contract.get("contract_id") if isinstance(contract, dict) else None
    spec = _ANSWER_SPECS.get(str(contract_id))
    if spec is None:
        if not schema_json:
            return ""
        return f"""## Answer
Your final message is this JSON and nothing else — no prose around it:
```json
{schema_json}
```
"""
    return f"""## Answer
Your final message is one JSON object and nothing else — no prose around it.

{spec.replace("{no_op}", _no_op_literals(schema_json) if schema_json else "its reason")}

"""


def _investigation_step(roster: Any, schema_json: str | None) -> str:
    """Return step 3 — where this lane may look when reuse was not enough.

    Which of the two it is comes from the lane's own answer shape rather than
    its name: a lane whose answer carries ``repo_id`` is bounded by the roster,
    and one that carries none measures what the host exposes. The roster
    travels with the request, so a lane that cannot cite a repository must not
    be handed one — it would be told to read what it has no way to report.
    """
    cites_repos = bool(schema_json) and '"repo_id"' in (schema_json or "")
    entries = [e for e in roster if isinstance(e, dict) and e.get("repo_id")] if roster else []
    if not cites_repos:
        return """3. **Only if 2 turned up nothing that bears on this question**, find and call
   the data tools this host exposes. An empty tool search is where you start,
   not where you stop; a store counts as unreachable only after a call failed."""
    if not entries:
        return """3. **No repository was given to you.** That is the whole answer — report the
   empty state and say so. Reading whatever is at hand would produce evidence
   nothing can check."""
    listing = "\n".join(f"   - `{e['repo_id']}` — {e.get('path')}" for e in entries)
    return f"""3. **Only if 2 turned up nothing that bears on this question**, read these
   repositories:
{listing}
   Look wherever you need to; cite only these. Follow what the question
   plainly touches — report what bears on it, not everything near it."""


def _payload_stub(
    payload: dict[str, Any],
    bundle_id: str,
    *,
    contract: Any = None,
    roster: Any = None,
    findings: Any = None,
) -> str:
    """Render the compact prompt a child receives when its brief is stored.

    Compact, not partial. What the child cannot do without travels here — the
    answer schema, where it may look, what it has already found — and only what
    explains those to a reader stays behind the fetch. A stub that carried none
    of it made one fetch the single point of failure for the whole lane: no
    schema, no roster, no rules, and nothing to do but say so.
    """
    context = payload.get("context") or {}
    lane_id = context.get("lane_id")
    schema_json = _lean_schema(contract)
    offered = [e for e in findings if isinstance(e, dict)] if findings else []
    # The ids do not travel. Twenty of them was a fifth of the prompt spent on
    # identifiers nothing could choose between, and a lane wanting none of them
    # paid for it anyway. The tool answers the same question on request, and
    # the window and the cap stay its own.
    if offered:
        reuse = f"""2. **Read what this lane already found here.** `ouroboros_fetch_artifact`
   with `lane_id: {lane_id}` and no `contract_id` lists them, newest first
   (load the tool via tool discovery if deferred); pass back a `contract_id`
   from that list to read one. This lane found it — carry it as it stands
   rather than establishing it again."""
    else:
        reuse = """2. **Nothing has been found here yet** for this lane, so there is nothing to
   reuse. Go to 3."""
    answer_section = _answer_section(contract, schema_json)
    return f"""## Task
You are an Ouroboros PM interview advisory subagent — lane {lane_id}. You
gather evidence the PM reads before deciding; you never decide for them.

## PM Question
{context.get("question")}

## Session
- session_id: {context.get("session_id")}
- question_identity: {context.get("question_identity")}

## Order of work
1. **Does this question need this lane?** If not, answer the empty state
   below and stop — do not investigate to prove it.
{reuse}
{_investigation_step(roster, schema_json)}

{answer_section}Describe, never prescribe. If two sources disagree, carry both — that
disagreement is the finding.

Full brief (rules and field descriptions): `ouroboros_fetch_artifact`
with contract_id `{bundle_id}`, lane_id `{lane_id}`."""


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

    What travels is decided by what a child cannot work without: the answer
    schema (stripped of its descriptions), the roster it may cite, and the
    findings it may reuse all ride the stub, and the fetch carries only what
    explains them. The first shape of this put everything behind the fetch, and
    a lane that could not reach the store had no schema, no roster and no rules
    — one call away from having nothing to do.

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
    request = meta.get("question_advisory_request")
    request = request if isinstance(request, dict) else {}
    contracts = {
        str(lane.get("lane_id")): lane.get("answer_contract")
        for lane in (request.get("lanes") or [])
        if isinstance(lane, dict) and lane.get("lane_id")
    }
    by_lane = request.get("recent_findings")
    by_lane = by_lane if isinstance(by_lane, dict) else {}
    for payload in payloads:
        if isinstance(payload, dict) and payload.get("prompt"):
            lane_id = str((payload.get("context") or {}).get("lane_id") or "")
            payload["prompt"] = _payload_stub(
                payload,
                bundle_id,
                contract=contracts.get(lane_id),
                roster=request.get("repository_roster"),
                findings=by_lane.get(lane_id),
            )
    if request:
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
