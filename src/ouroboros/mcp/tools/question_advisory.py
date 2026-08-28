"""The question-advisory fan-out, shared by the interview and the PM interview.

The lanes are described entirely by the calling tool's catalog in the capability
registry — ``orchestration.question_advisory_fanout`` — read by tool name. What
each lane asks of its child, which are required, and which answer contract each
carries are read from there rather than written here.

One builder serves both tools. Where the two genuinely differ the branch is on
what the request carries rather than on which tool is calling, so no tool is
named on the payload path; where they do not differ the lane is reused outright
— the ``data_context`` lane's task and brief come from ``advisory_prompts``
unchanged.

The rule about what a child may do with a clear finding is the one thing that
does differ, and it is not written here either. It lives in each tool's catalog
as ``child_answer_rule`` and is rendered into the lane brief, so the rule the
child reads and the rule the catalog declares are one text rather than two
copies that can drift. In the interview a code fact may stand in for the
answer; in PM it may not — a PM finding is shown to the user for confirmation
and then travels the ordinary ``answer`` parameter, recorded as an adopted fact
and never as their decision.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any

import structlog

from ouroboros.backends.capabilities import build_runtime_subagent_orchestration_contract
from ouroboros.mcp.telemetry_boundary import record_subagent_dispatch_emitted
from ouroboros.mcp.tools.advisory_prompts import (
    _INTERVIEW_DATA_CONTRACT_MAX_JSON_CHARS,
    _advisory_output_section,
    _bounded_json,
    _data_context_lane_brief,
    _data_context_lane_task,
)
from ouroboros.mcp.tools.fanout import FanoutRegistry, stamp_question_advisory_fanout
from ouroboros.mcp.tools.recent_findings import recent_findings_by_lane
from ouroboros.mcp.tools.subagent import (
    _INTERVIEW_ADVISORY_MAX_JSON_CHARS,
    _INTERVIEW_ADVISORY_MAX_QUESTION_CHARS,
    SubagentDispatchMode,
    SubagentPayload,
    _truncate_head,
    build_subagent_payload,
    stamp_fanout_meta,
)
from ouroboros.orchestrator.capabilities import ouroboros_tool_capability_metadata
from ouroboros.orchestrator.capabilities.question_text import normalize_question_text

log = structlog.get_logger()


def _question_identity(advisory: Mapping[str, Any], question: str) -> str:
    """Return the identity a contracted lane must echo back for this question.

    The namespace comes from the calling tool's catalog. Two tools can run
    against the same repository in one session, and an identity that only said
    "a question" would let one tool's answer validate against the other's
    fan-out — shaped correctly, meaning nothing.
    """
    prefix = str(advisory.get("question_identity_prefix") or "interview-question")
    digest = hashlib.sha256(normalize_question_text(question).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def _tool_advisory_catalog(tool_name: str) -> Mapping[str, Any]:
    """Return one tool's advisory catalog, or an empty mapping if it has none."""
    try:
        orchestration = ouroboros_tool_capability_metadata(tool_name)["orchestration"]
        catalog = orchestration["question_advisory_fanout"]
    except (KeyError, TypeError):
        return {}
    return catalog if isinstance(catalog, Mapping) else {}


#: ``name`` and ``desc`` are free text typed at ``ooo brownfield`` registration,
#: and shortening them is render-only: the child cites by ``repo_id``, which
#: ``pm_repo_id`` has already derived, and reads at ``path``.
_PM_ROSTER_FREETEXT_MAX_CHARS = 200
_PM_ROSTER_FREETEXT_FIELDS = ("name", "desc")


def _pm_roster_json(roster: Any) -> str:
    """Render every roster entry, shortening only the free-text fields.

    Not ``_bounded_json``: it cuts mid-array, and a dropped entry is one the
    child cannot know it never saw.
    """
    if not isinstance(roster, (list, tuple)):
        # As it came, not as ``[]`` -- an empty list would say there are no
        # repositories.
        return json.dumps(roster, ensure_ascii=False, sort_keys=True, indent=2)
    entries: list[Any] = []
    for entry in roster:
        if not isinstance(entry, Mapping):
            entries.append(entry)
            continue
        shortened = dict(entry)
        for field_name in _PM_ROSTER_FREETEXT_FIELDS:
            value = shortened.get(field_name)
            if isinstance(value, str) and len(value) > _PM_ROSTER_FREETEXT_MAX_CHARS:
                shortened[field_name] = value[:_PM_ROSTER_FREETEXT_MAX_CHARS].rstrip() + "…"
        entries.append(shortened)
    return json.dumps(entries, ensure_ascii=False, sort_keys=True, indent=2)


def _pm_code_context_lane_brief(
    roster: Any,
    child_answer_rule: str,
    answer_contract: Any,
) -> str:
    """Render the PM code lane's standing rules, its answer contract, and the roster.

    The roster is printed rather than described because the boundary is decided
    by value: an ``examined`` entry whose ``repo_id`` is not in this list is
    rejected at re-entry, so a child that cannot see the list cannot satisfy the
    contract except by luck. Everything else here is a boundary the child cannot be
    trusted to rediscover -- what it may not do with a disagreement (resolve
    it), what it must not claim (that a policy does not exist, as opposed to not
    being found in what it read), and which fields the contract requires of a
    carried finding.

    ``child_answer_rule`` comes in from the calling tool's catalog rather than
    being written here. It was written here once, and when the rule changed the
    catalog was updated while this copy was not -- one prompt then told the
    child both that its finding is confirmed and recorded, and that there is
    nothing to confirm. A rule with two spellings is a rule that stops agreeing
    with itself, so this brief renders the declared one.

    ``answer_contract`` is rendered for the same reason the roster is: the prose
    above names the fields a finding needs but not the closed part -- the
    ``nothing_examined_reason`` literals, ``additionalProperties``, the bounds --
    which the Output section already claims is "rendered in full above".
    """
    roster_json = _pm_roster_json(roster)
    contract_json = _bounded_json(answer_contract, _INTERVIEW_DATA_CONTRACT_MAX_JSON_CHARS)
    contract_id = ""
    if isinstance(answer_contract, Mapping):
        contract_id = str(answer_contract.get("contract_id") or "")
    # From the contract's own id, so the heading cannot outlive a rename.
    contract_heading = (
        f"## Answer Contract ({contract_id})" if contract_id else "## Answer Contract"
    )
    return f"""Read these repositories and report what they implement today for this
question.

{child_answer_rule}

**One entry per repository you opened.** `examined` is a list of entries, and
each carries the repository's `repo_id` and the `policy_claims` you found in it.
A repository you read and found nothing in is an entry with an empty
`policy_claims` — that is how "I looked and it is clean" is said. A repository
you never opened has no entry at all. Give one repository two entries and the
answer is rejected. If nothing opened at all, `examined` is empty and
`nothing_examined_reason` says why: an empty roster and a repository that would
not open are their own reasons, never "no policy found".

**Disagreement is the finding, not a defect.** If two repositories implement
different policies, carry both under their own entries. Do not reconcile them,
pick a winner, or describe them as one policy with an exception. That
contradiction is the most useful thing you can hand a PRD author.

**Evidence comes from the roster.** If the answer is one directory over, outside
this list, that is not evidence and it will be rejected: report it in your
finding as a repository worth adding, and give it no entry.

**Stop while the answer is still useful.** Read the roster in order and stop
once you can answer, giving entries only to what you actually opened. A partial
scope named honestly is a complete answer; an exhaustive search that has not
returned is not one at all.

## Repository Roster
```json
{roster_json}
```

{contract_heading}
```json
{contract_json}
```"""


def _lane_instructions(
    lane_id: str,
    raw_lane: Mapping[str, Any],
    request: Mapping[str, Any],
    catalog: Mapping[str, Any],
) -> tuple[str, str] | None:
    """Return ``(task, extra)`` for one declared lane, or ``None`` to skip it.

    Keyed by lane id rather than by tool, because a lane id names a job and two
    tools declaring the same lane want the same child. ``data_context`` is
    exactly that case: PM reuses the interview's lane unchanged, so it reuses
    its task and brief here rather than getting a second wording that would have
    to be kept in step.

    ``catalog`` is the calling tool's advisory catalog, and it is passed in for
    the same reason: a rule the catalog declares is rendered from there rather
    than restated here.
    """
    if lane_id == "code_context":
        # Two tools declare this lane and each hands it a different bounding
        # input -- the interview a code-fact request, PM a repository roster.
        # The branch is on what the request carries rather than on which tool
        # is calling, so neither tool is named here.
        roster = request.get("repository_roster")
        if roster is not None:
            return (
                "Find the policy the roster repositories implement today for this "
                "question, and report it descriptively. If they do not implement "
                "one, say which repositories you read and give the reason.",
                _pm_code_context_lane_brief(
                    roster,
                    str(catalog.get("child_answer_rule") or ""),
                    raw_lane.get("answer_contract"),
                ),
            )
        return (
            "Inspect the local repository for facts that directly answer or "
            "constrain the question. Use exact file/config evidence. Do not "
            "make product decisions. If the code does not answer it, say so.",
            "## Code Investigation Request\n```json\n"
            + _bounded_json(
                request.get("code_investigation_request"),
                _INTERVIEW_ADVISORY_MAX_JSON_CHARS,
            )
            + "\n```",
        )
    if lane_id == "web_context":
        return (
            "Decide whether current external knowledge is needed. If yes, "
            "research the minimum necessary current facts and cite sources. "
            "If no current web facts are needed, return that no-op finding.",
            "Use web research only when the answer depends on current external facts.",
        )
    if lane_id == "ambiguity_contrarian":
        return (
            "Challenge the question and the likely answer. Identify hidden "
            "assumptions, overloaded terms, missing constraints, and decisions "
            "the human might accidentally skip.",
            "Lean into the contrarian role, but keep the advice user-safe and actionable.",
        )
    if lane_id == "answer_simplifier":
        return (
            "Turn the question into an easy response surface: 2-3 concrete "
            "answer options or one recommended draft the user can approve or edit.",
            "Prefer concise choices over a broad essay.",
        )
    if lane_id == "architecture_implications":
        return (
            "Check whether the answer would affect system shape, ownership, "
            "interfaces, rollout, data model, or verification strategy.",
            "Only raise architecture implications that materially affect implementation.",
        )
    if lane_id == "data_context":
        return _data_context_lane_task(), _data_context_lane_brief(raw_lane.get("answer_contract"))
    return None


def _lane_agent(raw_lane: Mapping[str, Any], persona: str, capability: str) -> str:
    """Return the child agent for one lane.

    A contracted lane never gets a persona-derived researcher. ``researcher.md``
    carries its own ``## OUTPUT`` section -- "states what was unknown, shows what
    evidence was gathered, presents a hypothesis" -- which is the free-form shape
    a closed contract rejects, so handing it to a contracted lane reintroduces
    from the persona side the defect ``_advisory_output_section`` exists to
    prevent. It is keyed on the contract rather than on the lane id so a lane
    that gains a contract later does not have to remember to change this too.
    """
    if persona:
        return persona
    if isinstance(raw_lane.get("answer_contract"), Mapping):
        return "general"
    return "researcher" if capability in {"inspect_code", "web_research"} else "general"


def _declared_lanes(catalog: Mapping[str, Any]) -> set[str]:
    """Return every lane id one tool's catalog declares.

    This is the offer set handed to ``recent_findings_by_lane``, which narrows
    it to the lanes RFC Q00/ouroboros#2167 admits (``code_context``,
    ``data_context``). Whether a lane answers under a closed contract is not
    part of the test: eligibility follows what a lane *produces*, and the shape
    of its answer is a validation concern, not a reuse one (#2223).

    It was gated on answer shape once -- a contracted lane was offered nothing,
    on the reasoning that its closed answer had no field in which to confess a
    failed fetch. That withheld the head start from exactly the lanes doing the
    most repeated work (both PM lanes are contracted) to protect a report
    nobody consumes: a contracted answer carries no reuse statement a reader
    could be misled by, a fetch that fails degrades to the investigation the
    lane would have run anyway, and whether reuse happens at all is visible
    server-side from the ``ouroboros_fetch_artifact`` calls. What the shape
    still decides is the offer *text* -- a contracted lane is not told to
    confess in-band; see ``_recent_findings_section``.

    Decided here rather than at render time so the request carries only what
    some lane will read. A key nothing renders is a promise the schema makes and
    the prompt never keeps.
    """
    lanes = catalog.get("lanes")
    if not isinstance(lanes, (list, tuple)):
        return set()
    return {
        str(lane.get("lane_id"))
        for lane in lanes
        if isinstance(lane, Mapping) and lane.get("lane_id")
    }


def _recent_findings_section(request: Mapping[str, Any], lane_id: str, *, contracted: bool) -> str:
    """Return the block carrying what has already been found in this project.

    Rendered by presence, like the scores above it: a project with nothing
    recent carries no key and its children are handed no line about it. Per lane,
    too -- a lane whose own lane published nothing is handed no line either
    (RFC Q00/ouroboros#2167).

    Which lanes have a key at all is decided once, where the catalog is read
    (see ``_declared_lanes``); this only renders what it was handed.

    ``contracted`` selects what the block asks of a lane that cannot reach the
    fetch tool. A prose lane says so in its finding -- silence there would read
    as "nothing to reuse", the confusion RFC Q00/ouroboros#2167 exists to
    prevent. A contracted lane has no field for that sentence and no reader who
    would be misled by its absence: its answer carries claims about the
    system, validated against the roster identically whether the fetch worked,
    so it is told to keep its shape and investigate -- the offer's failure mode
    is the exact behaviour the lane had without the offer. Whether it fetched
    is the server's to observe, from the fetch calls themselves (#2223).

    **A place to find them, and nothing to work out about it.** This block used
    to hand over paths and then explain how to arrange what was inside them --
    which lanes to select, against which shape. A lane read that explanation,
    selected against the wrong shape, found nothing and re-investigated; nothing
    failed loudly, because a lane reporting no reusable findings looks exactly
    like a project having none. What removes that failure is not withholding the
    place: each entry pairs the contract with this lane's id, the fetch passes
    both back, and the store returns this lane's output alone -- so there is no
    arrangement left to describe and no instruction to carry out incorrectly.

    **The bodies stay in the store, and that is not a preference.** Carried
    inline they were duplicated into every lane of the turn; the tool result
    outgrew what a host accepts inline, was written to a file, and the host spent
    the turn parsing its own output rather than dispatching the fan-out. A block
    a lane never receives helps nobody.

    **The count is what makes fetching safe to require.** Naming how many are
    offered before naming the tool means a lane that cannot reach it knows
    something is there. Without that number, an unreachable tool and an empty
    project produce the same silence -- the confusion this whole mechanism
    exists to prevent. Offered, not published: the list is capped, so a count
    stated as a total would be a number this cannot know.

    **They may not be this session's.** A finding describes the system, and the
    system does not change at session granularity -- so the boundary is recency
    and another session's finding about this project is as good as this one's
    (RFC Q00/ouroboros#2153). What does not carry across is the roster: a
    session chooses which repositories it is asking about, and evidence from
    outside this session's roster is rejected at submission. The child is told
    so here, where it is deciding what to trust.

    What this must not become is a second set of rules. There is nothing here
    about proving a stored finding sufficient, reporting what a reuse left
    unsettled, or marking an answer as reused. Those would be bookkeeping about
    incompleteness, and what the answer contracts guard is that an answer cannot
    lie about its sources -- which holds whatever the child read.
    """
    by_lane = request.get("recent_findings")
    found = by_lane.get(lane_id) if isinstance(by_lane, Mapping) else None
    entries = list(found) if isinstance(found, (list, tuple)) else []
    if not entries:
        return ""
    listing = "\n".join(
        f"- `contract_id`: `{entry.get('contract_id')}`,"
        f" `lane_id`: `{entry.get('lane_id', lane_id)}`"
        f" — published {entry.get('published_at')}"
        for entry in entries
    )
    count = len(entries)
    plural = "" if count == 1 else "s"
    if contracted:
        unreachable = """**If you cannot reach that tool, investigate as usual.** Either way your answer
keeps its contracted shape — do not add fields about reuse."""
    else:
        unreachable = f"""**If you cannot reach that tool, say so in your finding** — you were offered
{count}, so reporting nothing to reuse would be false — then investigate as you
would have without them."""
    return f"""## Recently Found Here
You are offered {count} recent finding{plural} your lane published in this project
within the last day. The bodies are not here. Fetch each with the MCP tool
`ouroboros_fetch_artifact`, passing both values below — the `lane_id` is what
narrows the artifact to your lane's own output:

{listing}

Read what you fetch, use what helps, and investigate the rest yourself. These
are a head start, not a substitute.

{unreachable}

These may come from other sessions, which chose their own repositories. Report
only what is true of the repositories *this* question gave you; a claim about
any other is rejected when your answer is submitted."""


def build_question_advisory_subagents(request: Mapping[str, Any]) -> list[SubagentPayload]:
    """Build one advisory subagent payload per lane the catalog declares.

    The parent session owns the user-facing question; these payloads are the
    assist layer around it. Which lanes exist, which are required, and which
    carry an answer contract are all read from the request, which carries the
    calling tool's catalog verbatim.
    """
    capability = request.get("mcp_tool_capability")
    tool_name = (
        str((capability or {}).get("tool_name") if isinstance(capability, Mapping) else "")
        or "ouroboros_interview"
    )
    # Read from the catalog rather than carried on the request: the request
    # schemas are closed, and a host-UI label is not part of the wire contract.
    catalog = _tool_advisory_catalog(tool_name)
    title_prefix = str(catalog.get("payload_title_prefix") or "Advisory lane")
    task_preamble = str(catalog.get("task_preamble") or "")
    question_heading = str(catalog.get("question_heading") or "## Question")
    session_id = str(request.get("session_id") or "")
    question_identity = str(request.get("question_identity") or "")
    question = str(request.get("question") or "")
    if not session_id:
        raise ValueError("request.session_id must not be empty")
    if not question_identity:
        raise ValueError("request.question_identity must not be empty")
    if not question:
        raise ValueError("request.question must not be empty")

    raw_lanes = request.get("lanes")
    if not isinstance(raw_lanes, (list, tuple)) or not raw_lanes:
        raise ValueError("request.lanes must be a non-empty list")

    bounded_question = _truncate_head(question, _INTERVIEW_ADVISORY_MAX_QUESTION_CHARS)
    synthesis_contract = request.get("synthesis_contract")
    synthesis_contract_json = _bounded_json(synthesis_contract, _INTERVIEW_ADVISORY_MAX_JSON_CHARS)
    session_lines = [
        f"- session_id: {session_id}",
        f"- question_identity: {question_identity}",
    ]
    # Rendered by presence in the request, not by value. A tool that scores
    # ambiguity always sets the key and may set it to ``None``; a tool that has
    # no such concept never sets it, and its child is not handed a line about a
    # thing its tool does not measure.
    for label in ("ambiguity_score", "milestone"):
        if label in request:
            session_lines.append(f"- {label}: {request[label]}")
    session_block = "\n".join(session_lines)
    payloads: list[SubagentPayload] = []
    seen: set[str] = set()
    for raw_lane in raw_lanes:
        if not isinstance(raw_lane, Mapping):
            continue
        lane_id = str(raw_lane.get("lane_id") or "").strip()
        lane_capability = str(raw_lane.get("capability") or "").strip()
        if not lane_id or lane_id in seen:
            continue
        instructions = _lane_instructions(lane_id, raw_lane, request, catalog)
        if instructions is None:
            # A lane this build has no instructions for is skipped rather than
            # given a generic prompt: a child told only "help with this" against
            # a closed contract fails validation, and the fan-out then cannot
            # complete at all if the lane is required.
            log.warning(
                "mcp.tool.question_advisory.unknown_lane",
                tool_name=tool_name,
                lane_id=lane_id,
            )
            continue
        seen.add(lane_id)
        lane_task, extra = instructions

        persona = str(raw_lane.get("persona") or "").strip()
        purpose = str(raw_lane.get("purpose") or "Help answer the question.").strip()
        required = bool(raw_lane.get("required"))

        # Built per lane: a lane is offered only what its own lane published
        # (RFC Q00/ouroboros#2167), so this differs between lanes and is absent
        # for the four that read nothing.
        recent_findings = _recent_findings_section(
            request,
            lane_id,
            contracted=isinstance(raw_lane.get("answer_contract"), Mapping),
        )
        recent_findings_section = f"\n{recent_findings}\n" if recent_findings else ""
        prompt = f"""## Task
{task_preamble}

{question_heading}
{bounded_question}

## Session
{session_block}

## Advisory Lane
- lane_id: {lane_id}
- capability: {lane_capability}
- required: {str(required).lower()}
- purpose: {purpose}

## Lane Task
{lane_task}

{extra}
{recent_findings_section}
## Synthesis Contract
```json
{synthesis_contract_json}
```

{_advisory_output_section(raw_lane.get("answer_contract"))}"""

        payloads.append(
            build_subagent_payload(
                tool_name=tool_name,
                title=f"{title_prefix}: {lane_id}",
                agent=_lane_agent(raw_lane, persona, lane_capability),
                prompt=prompt,
                context={
                    "session_id": session_id,
                    "question_identity": question_identity,
                    "question": question,
                    "lane_id": lane_id,
                    "capability": lane_capability,
                    "required": required,
                    "persona": persona or None,
                    "user_question_first": bool(request.get("user_question_first")),
                    "synthesis_contract": dict(synthesis_contract)
                    if isinstance(synthesis_contract, Mapping)
                    else {},
                },
            )
        )

    if not payloads:
        raise ValueError("request.lanes did not contain any known advisory lanes")
    return payloads


def build_question_advisory_request(
    *,
    tool_name: str,
    session_id: str,
    question: str,
    phase: str | None = None,
    ambiguity_score: float | None = None,
    milestone: str | None = None,
    code_investigation_request: Mapping[str, Any] | None = None,
    repository_roster: list[dict[str, str]] | None = None,
    last_question: str | None = None,
    recent_findings: Mapping[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Build the per-question advisory request for one tool's question turn.

    The catalog is read from the tool capability registry, so a tool joins the
    fan-out by declaring ``orchestration.question_advisory_fanout`` and nothing
    else. ``code_investigation_request`` and ``repository_roster`` are the two
    tool-shaped inputs, and each is attached only when its caller supplies one —
    the interview has a code-fact request and no roster; PM has a roster and no
    code-fact request, because a code fact cannot become a PRD answer.
    """
    mcp_tool_capability = ouroboros_tool_capability_metadata(tool_name)
    advisory = mcp_tool_capability["orchestration"]["question_advisory_fanout"]
    request: dict[str, Any] = {
        "contract_id": advisory["contract_id"],
        "session_id": session_id,
        "question_identity": _question_identity(advisory, question),
        "question": question,
        "user_question_first": True,
        "advisory_goal": advisory["advisory_goal"],
        "parallel_preference": advisory["parallel_preference"],
        "sequential_fallback": dict(advisory["sequential_fallback"]),
        "allowed_capabilities": list(advisory["allowed_capabilities"]),
        "lanes": list(advisory["lanes"]),
        "synthesis_contract": dict(advisory["synthesis_contract"]),
        "mcp_tool_capability": mcp_tool_capability,
    }

    if phase is not None:
        request["phase"] = phase
    if advisory.get("scores_ambiguity"):
        request["ambiguity_score"] = ambiguity_score
        request["milestone"] = milestone
    if last_question:
        request["last_question"] = last_question
    if code_investigation_request is not None:
        request["code_investigation_request"] = dict(code_investigation_request)
    if repository_roster is not None:
        request["repository_roster"] = repository_roster
    # Attached only when there is something to attach. A project with nothing
    # recent carries no key, and a child is not handed a block that says nothing
    # has been found here -- that is a sentence it would have to reason about.
    if recent_findings:
        # Keyed by lane: a lane is offered only its own findings
        # (RFC Q00/ouroboros#2167), so this is a mapping rather than a pool.
        request["recent_findings"] = {
            lane_id: list(found) for lane_id, found in recent_findings.items()
        }
    return request


def attach_question_advisory(
    meta: dict[str, Any],
    *,
    tool_name: str,
    session_id: str,
    question: str,
    phase: str | None = None,
    ambiguity_score: float | None = None,
    milestone: str | None = None,
    code_investigation_request: Mapping[str, Any] | None = None,
    repository_roster: list[dict[str, str]] | None = None,
    last_question: str | None = None,
    dispatch_mode: SubagentDispatchMode = SubagentDispatchMode.SEQUENTIAL,
    runtime_backend: str | None = None,
    opencode_mode: str | None = None,
    fanout_registry: FanoutRegistry | None = None,
    findings_store: Any | None = None,
) -> None:
    """Attach the advisory fan-out to a turn that shows a question to the user.

    Called wherever a question becomes visible, which is the rule rather than a
    list of call sites: a question the user can see with no lanes attached is a
    decision made without the evidence, and nothing downstream can tell that
    apart from a decision made with it.

    A build failure leaves the turn otherwise intact. The question is what the
    user needs; losing the lanes costs them evidence, while raising here would
    cost them the question.

    ``findings_store`` is the store this project's completed fan-outs were
    published into, taken from the side that publishes rather than rebuilt here.
    A caller without one still gets its lanes, and they investigate the way they
    always did.
    """
    if not question:
        return
    request = build_question_advisory_request(
        tool_name=tool_name,
        session_id=session_id,
        question=question,
        phase=phase,
        ambiguity_score=ambiguity_score,
        milestone=milestone,
        code_investigation_request=code_investigation_request,
        repository_roster=repository_roster,
        last_question=last_question,
        recent_findings=recent_findings_by_lane(
            findings_store,
            lanes=_declared_lanes(_tool_advisory_catalog(tool_name)),
        ),
    )
    try:
        payloads = build_question_advisory_subagents(request)
    except ValueError as exc:
        log.warning(
            "mcp.tool.question_advisory.build_failed",
            tool_name=tool_name,
            session_id=session_id,
            error=str(exc),
        )
        return

    meta["question_advisory_recommended"] = True
    meta["question_advisory_request"] = request
    meta["question_advisory_contract_id"] = request["contract_id"]
    meta["question_advisory_subagents"] = [payload.to_dict() for payload in payloads]
    meta["question_advisory_preserve_content"] = True
    if code_investigation_request is not None:
        meta["code_investigation_request"] = dict(code_investigation_request)

    contract = build_runtime_subagent_orchestration_contract(
        runtime_backend or "unknown",
        directive_metadata=request,
        opencode_mode=opencode_mode,
        dispatch_mode=dispatch_mode,
    )
    meta["subagent_orchestration_instruction"] = contract.runtime_instruction_handling
    # Advisory lanes are keyed by lane_id; their persona is absent on some
    # lanes (code_context, web_context, code_context), so correlate by lane_id.
    stamp_fanout_meta(
        meta,
        prefix="question_advisory",
        dispatch_mode=dispatch_mode,
        payloads=payloads,
        correlation_key="context.lane_id",
    )
    # Register from the SAME request this response stamps, and record which tool
    # issued it: re-entry resolves the answer contracts from that tool's catalog,
    # so a lane is judged by the contract its own tool declared.
    stamp_question_advisory_fanout(
        meta,
        fanout_registry,
        session_id=session_id,
        payloads=payloads,
        tool_name=tool_name,
        roster_repo_ids=(
            [entry["repo_id"] for entry in repository_roster]
            if repository_roster is not None
            else None
        ),
    )
    record_subagent_dispatch_emitted(
        fanout_kind="question_advisory",
        payload_count=len(payloads),
        dispatch_mode=dispatch_mode,
        worker_backend=runtime_backend,
        fanout_reentry_available="question_advisory_fanout_id" in meta,
    )


__all__ = [
    "attach_question_advisory",
    "build_question_advisory_request",
    "build_question_advisory_subagents",
]
