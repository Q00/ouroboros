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

from ouroboros.core.types import Result

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


#: A worked answer per contract, in place of the contract's own schema.
#:
#: The schema is exact and nearly unreadable — a child reads ``oneOf`` branches
#: and regex patterns to work out that ``path`` is repository-relative. One
#: filled-in answer says the same thing in a quarter of the characters, and
#: says the part regexes say worst: what a value looks like.
#:
#: Keyed by ``contract_id``, so a contract that changes version falls back to
#: its schema rather than being described by an example written for the old
#: one. What an example cannot show — bounds, enums, the rules about what may
#: not appear — is stated beside it, and only what a wrong answer would be
#: rejected for.
#:
#: **Every value in them is deliberately fictional**, and the identifiers are
#: chosen so that copying one wholesale fails: ``example-repo-a1b2c3d4`` is in
#: no roster, so an answer carrying it is rejected at submission. An example
#: whose values looked real would be the one thing this mechanism exists to
#: prevent — a fabricated claim, correctly shaped, reaching the PM as evidence.
#: Wrong-and-refused and invented-and-accepted are not the same failure, and
#: the example is written to fail the first way.
#:
#: ``tests/unit/mcp/tools/test_pm_handler_batch.py`` validates every example
#: against the contract it is keyed to, so an example cannot drift from it, and
#: checks that its identifiers are none of the ones a lane is actually given.
_ANSWER_EXAMPLES: dict[str, tuple[dict[str, Any], dict[str, Any], str]] = {
    "pm_code_context_answer.v2": (
        {
            "question_identity": "pm-question:0000000000000000",
            "lane_id": "code_context",
            "examined": [
                {
                    "repo_id": "example-repo-a1b2c3d4",
                    "policy_claims": [
                        {
                            "path": "src/main/java/com/example/booking/ReminderScheduler.java",
                            "policy_claim": (
                                "Three reminders fire at +6h, +3h and day+6 09:21 after a trial "
                                "booking, keyed {userId}-{templateCode}, and are cancelled once "
                                "the user books a class."
                            ),
                            "plain_statement": (
                                "신청 직후 개입은 전부 시점 기반 리마인드이고, 예약하면 취소됩니다."
                            ),
                        }
                    ],
                },
                {"repo_id": "example-app-e5f6a7b8", "policy_claims": []},
            ],
        },
        {
            "question_identity": "pm-question:0000000000000000",
            "lane_id": "code_context",
            "examined": [],
            "nothing_examined_reason": "not_a_policy_question",
        },
        """- `path` is relative to the repository, never absolute and never through `..`.
- `plain_statement` is the claim beside it said once, in the question's own
  language, with no paths or identifiers in it — it is what the PM reads.
- One entry per repository. A repository you read and found nothing in is an
  entry with empty `policy_claims` — that is how "I looked and it is clean" is
  said, and a repository you never opened has no entry at all.
- At most 20 claims per repository; `policy_claim` ≤ 600 characters,
  `plain_statement` ≤ 300.
- No other fields. Anything the shape does not name is rejected with the answer.""",
    ),
    "data_evidence_answer.v1": (
        {
            "question_identity": "pm-question:0000000000000000",
            "lane_id": "data_context",
            "data_needed": True,
            "read_requests": [
                {
                    "operation": "read",
                    "tool_name": "example_metrics_tool",
                    "metric": "trial bookings that reached the lesson",
                    "aggregation": "count",
                    "filters": [{"field": "status", "comparator": "eq", "value": "COMPLETED"}],
                    "time_window": "last 30 days",
                    "informs_decision": "which of the two drop-offs is the larger one today",
                    "values": [{"value": 1832}],
                }
            ],
        },
        {
            "question_identity": "pm-question:0000000000000000",
            "lane_id": "data_context",
            "data_needed": False,
            "no_evidence_reason": "not_a_measurement",
        },
        """- At most 5 read requests, each carrying the value it actually read back.
- `aggregation` is one of count, distinct_count, sum, average, median, p90,
  p95, p99, min, max, rate; `comparator` one of eq, neq, gt, gte, lt, lte.
- A count or distinct_count value is a non-negative integer.
- Without `group_by` there is exactly one value; with it, up to 20 entries of
  `{group, value}`.
- No rows, names or identifiers — an aggregate is what this lane may carry.
- No other fields. Anything the shape does not name is rejected with the answer.""",
    ),
}


def _answer_section(contract: Any, schema_json: str | None) -> str:
    """Return the ``## Answer`` block: a worked example, or the schema itself."""
    contract_id = contract.get("contract_id") if isinstance(contract, dict) else None
    example = _ANSWER_EXAMPLES.get(str(contract_id))
    if example is None:
        if not schema_json:
            return ""
        return f"""## Answer
Your final message is this JSON and nothing else — no prose around it:
```json
{schema_json}
```
"""
    answer, empty, notes = example
    return f"""## Answer
Your final message is one JSON object and nothing else — no prose around it,
shaped like this:
```json
{json.dumps(answer, ensure_ascii=False, indent=2)}
```
Nothing to report is its own answer, not an empty version of the one above:
```json
{json.dumps(empty, ensure_ascii=False, indent=2)}
```
**Every value above is invented.** Take the identity from the Session block,
the identifiers from 3, and every claim from what you actually read — an
answer shaped like this one but not read from anywhere is the one thing that
must never reach the PM.
{notes}

"""


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
        return """3. **Still not enough?** Find the data tools this host exposes and call them —
   registering one is the willingness to have it called. An empty tool search
   is where you start looking, never where you stop, and a store is only
   unreachable once a call to it has actually failed."""
    if not entries:
        return """3. **No repository was given to you.** That is the whole answer — report the
   empty state and say so. Reading whatever is at hand would produce evidence
   nothing can check."""
    listing = "\n".join(f"   - `{e['repo_id']}` — {e.get('path')}" for e in entries)
    return f"""3. **Still not enough?** Read these repositories:
{listing}
   Where you look is open; what you cite is not. Every `repo_id` you report must
   be one of the above — anything else is rejected at submission."""


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
    if offered:
        # To the minute. Sub-second precision and a UTC offset decide nothing
        # here and are twenty lines of it.
        listing = "\n".join(
            f"   - `{e.get('contract_id')}` — {str(e.get('published_at') or '')[:16]}"
            for e in offered
        )
        # Recency is the only thing separating these entries, so it is the only
        # thing the instruction may lean on. Telling a child to pick the ones
        # whose subject fits would name a signal the offer does not carry, and
        # telling it to fetch each in turn would spend the budget before the
        # work started. These are a head start, not a checklist.
        reuse = f"""2. **Read what this lane already found here.** `ouroboros_fetch_artifact`
   takes a `contract_id` below plus `lane_id: {lane_id}` (load the tool via
   your runtime's tool discovery if deferred). They are newest first and that
   is all you can tell them apart by, so start at the top and open a few
   rather than all of them. Use what helps, investigate the rest yourself, and
   if what you read answers the question, stop there.
{listing}"""
    else:
        reuse = """2. **Nothing has been found here yet** for this lane, so there is nothing to
   reuse. Go to 3."""
    no_op = _no_op_literals(schema_json) if schema_json else ""
    no_op_hint = f" ({no_op})" if no_op else ""
    answer_section = _answer_section(contract, schema_json)
    return f"""## Task
You are an Ouroboros PM interview advisory subagent — lane {lane_id}. You gather
evidence the PM reads before answering; you never answer for them.

## PM Question
{context.get("question")}

## Session
- session_id: {context.get("session_id")}
- question_identity: {context.get("question_identity")}

## Order of work
1. **Does this question need this lane at all?** If not, answer the empty
   state{no_op_hint} and stop. Do not investigate to prove it.
{reuse}
{_investigation_step(roster, schema_json)}

{answer_section}Describe, never prescribe: what you find is an input to the PM's decision, not
the decision. If two sources disagree, carry both — the disagreement is what the
PM most needs.

Full brief (rules, worked examples, field descriptions): `ouroboros_fetch_artifact`
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
