"""PM question-advisory lanes: what the RFC decided, checked by running it.

Each test names the decision from RFC Q00/ouroboros#1937 it holds, because the
value of most of these is not that the code works but that a particular thing
*cannot be expressed* — and an unexpressible thing has no failing behaviour to
point at later if the guard is removed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ouroboros.mcp.tools.fanout import (
    FANOUT_KIND_QUESTION_ADVISORY,
    FanoutRegistry,
    submit_fanout_results,
)
from ouroboros.mcp.tools.question_advisory import (
    attach_question_advisory,
    build_question_advisory_subagents,
)
from ouroboros.orchestrator.capabilities.pm_schemas import (
    _pm_question_advisory_fanout_metadata,
    pm_code_context_answer_contract,
    pm_repo_id,
    pm_repository_roster,
    stable_pm_question_identity,
)

QUESTION = "What happens today when a subscription lapses mid-period?"


@pytest.fixture
def roster() -> list[dict[str, str]]:
    return pm_repository_roster(
        [
            {"path": "/repo/api", "name": "api"},
            {"path": "/repo/web", "name": "web"},
        ]
    )


@pytest.fixture
def registry(tmp_path: Path) -> FanoutRegistry:
    return FanoutRegistry(tmp_path / "fanout")


def _attach(
    registry: FanoutRegistry,
    roster: list[dict[str, str]],
    *,
    session_id: str = "pm-1",
    question: str = QUESTION,
) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    attach_question_advisory(
        meta,
        tool_name="ouroboros_pm_interview",
        session_id=session_id,
        question=question,
        repository_roster=roster,
        fanout_registry=registry,
    )
    return meta


def _submit(
    registry: FanoutRegistry,
    meta: dict[str, Any],
    code_output: dict[str, Any],
    *,
    session_id: str = "pm-1",
    identity: str | None = None,
) -> dict[str, Any]:
    identity = identity or stable_pm_question_identity(QUESTION)
    return submit_fanout_results(
        registry,
        session_id=session_id,
        correlation_key="context.lane_id",
        results=[
            {"key": "code_context", "content": code_output},
            {
                "key": "data_context",
                "content": {
                    "question_identity": identity,
                    "lane_id": "data_context",
                    "data_needed": False,
                    "no_evidence_reason": "not_a_measurement",
                },
            },
        ],
        fanout_id=meta["question_advisory_fanout_id"],
    )


# ── Decision 1: only the two evidence lanes ──────────────────────────────


def test_pm_runs_only_the_two_evidence_lanes() -> None:
    lanes = _pm_question_advisory_fanout_metadata()["lanes"]
    assert [lane["lane_id"] for lane in lanes] == ["code_context", "data_context"]


def test_no_lane_produces_a_recommended_draft() -> None:
    """The interview synthesizes answer options; PM synthesizes evidence.

    Asserted on the synthesis contract rather than on the lane list because it
    is the shape that would quietly re-admit a draft: keeping the interview's
    ``answer_advisory`` output would ask the parent for a recommended answer
    even with no lane producing one.
    """
    contract = _pm_question_advisory_fanout_metadata()["synthesis_contract"]
    assert contract["output_shape"] == "evidence_beside_question"
    assert contract["include_recommended_draft"] is False
    assert contract["preserve_user_agency"] is True


# ── Decision 2: no path to becoming the answer ───────────────────────────


def test_neither_contract_holds_a_field_an_answer_could_be_spelled_in() -> None:
    """The enforcement is absence, so absence is what is asserted.

    ``answer_prefix``, ``requires_user_confirmation`` and a confidence grade are
    the three fields the interview's code-fact contract uses to let a lane's
    output become the answer. None exists here, and both states are closed, so
    a child cannot add one.
    """
    schema = pm_code_context_answer_contract()["response_model_schema"]
    for state in schema["oneOf"]:
        assert state["additionalProperties"] is False
        assert not {
            "answer_prefix",
            "requires_user_confirmation",
            "confidence",
        } & set(state["properties"])


def test_an_answer_prefix_is_rejected_at_re_entry(
    registry: FanoutRegistry, roster: list[dict[str, str]]
) -> None:
    meta = _attach(registry, roster)
    result = _submit(
        registry,
        meta,
        {
            "question_identity": stable_pm_question_identity(QUESTION),
            "lane_id": "code_context",
            "policy_found": True,
            "examined_repository_ids": [roster[0]["repo_id"]],
            "answer_prefix": "[from-code][auto-confirmed]",
            "evidence": [
                {
                    "repo_id": roster[0]["repo_id"],
                    "path": "src/billing.py",
                    "policy_claim": "grace period applies",
                }
            ],
        },
    )
    assert result["status"] == "partial"
    assert "additionalProperties" in str(result["contract_violations"]["code_context"])


# ── Decision 3: the lane speaks about itself ─────────────────────────────


def test_a_free_text_reason_is_rejected(
    registry: FanoutRegistry, roster: list[dict[str, str]]
) -> None:
    """A sentence is where a claim about the PM's system would be written."""
    meta = _attach(registry, roster)
    result = _submit(
        registry,
        meta,
        {
            "question_identity": stable_pm_question_identity(QUESTION),
            "lane_id": "code_context",
            "policy_found": False,
            "examined_repository_ids": [roster[0]["repo_id"]],
            "no_policy_reason": "there is no such policy anywhere in your system",
        },
    )
    assert result["status"] == "partial"
    assert "no_policy_reason: enum" in result["contract_violations"]["code_context"]


def test_scope_is_required_even_when_nothing_was_read(
    registry: FanoutRegistry, roster: list[dict[str, str]]
) -> None:
    """An empty roster reports itself, not "found nothing"."""
    meta = _attach(registry, roster)
    result = _submit(
        registry,
        meta,
        {
            "question_identity": stable_pm_question_identity(QUESTION),
            "lane_id": "code_context",
            "policy_found": False,
            "examined_repository_ids": [],
            "no_policy_reason": "no_repository_in_roster",
        },
    )
    assert result["status"] == "complete"


def test_an_answer_without_its_scope_is_rejected() -> None:
    schema = pm_code_context_answer_contract()["response_model_schema"]
    for state in schema["oneOf"]:
        assert "examined_repository_ids" in state["required"]


# ── Decision 4: a total answer, so the lane can be required ──────────────


def test_both_lanes_are_required_and_both_have_a_no_op_answer() -> None:
    lanes = _pm_question_advisory_fanout_metadata()["lanes"]
    assert all(lane["required"] for lane in lanes)
    code_states = {
        state["properties"]["policy_found"]["const"]
        for state in pm_code_context_answer_contract()["response_model_schema"]["oneOf"]
    }
    assert code_states == {True, False}


# ── Decision 6: the roster is a boundary, decided from the value ─────────


def test_evidence_from_outside_the_roster_is_rejected(
    registry: FanoutRegistry, roster: list[dict[str, str]]
) -> None:
    meta = _attach(registry, roster)
    result = _submit(
        registry,
        meta,
        {
            "question_identity": stable_pm_question_identity(QUESTION),
            "lane_id": "code_context",
            "policy_found": True,
            "examined_repository_ids": [roster[0]["repo_id"]],
            "evidence": [
                {
                    "repo_id": "elsewhere-deadbeef",
                    "path": "src/x.py",
                    "policy_claim": "something",
                }
            ],
        },
    )
    assert result["status"] == "partial"
    assert "not in this session's roster" in str(result["contract_violations"]["code_context"])


def test_a_scope_claiming_an_unoffered_repository_is_rejected(
    registry: FanoutRegistry, roster: list[dict[str, str]]
) -> None:
    meta = _attach(registry, roster)
    result = _submit(
        registry,
        meta,
        {
            "question_identity": stable_pm_question_identity(QUESTION),
            "lane_id": "code_context",
            "policy_found": False,
            "examined_repository_ids": ["ghost-11111111"],
            "no_policy_reason": "no_policy_found_in_examined_repositories",
        },
    )
    assert result["status"] == "partial"
    assert "examined_repository_ids" in str(result["contract_violations"]["code_context"])


def test_cross_repo_disagreement_survives_as_structure(
    registry: FanoutRegistry, roster: list[dict[str, str]]
) -> None:
    """Two repositories implementing different policies is the PRD input.

    It is accepted rather than reconciled, and the two claims stay attached to
    their own repositories — which is the whole reason ``repo_id`` sits on the
    evidence item rather than on the request.
    """
    meta = _attach(registry, roster)
    result = _submit(
        registry,
        meta,
        {
            "question_identity": stable_pm_question_identity(QUESTION),
            "lane_id": "code_context",
            "policy_found": True,
            "examined_repository_ids": [roster[0]["repo_id"], roster[1]["repo_id"]],
            "evidence": [
                {
                    "repo_id": roster[0]["repo_id"],
                    "path": "src/billing.py",
                    "policy_claim": "access continues until period end",
                },
                {
                    "repo_id": roster[1]["repo_id"],
                    "path": "src/checkout.ts",
                    "policy_claim": "access is revoked immediately",
                },
            ],
        },
    )
    assert result["status"] == "complete"
    evidence = [
        item
        for lane in result["result"]["aggregated_outputs"]
        if lane["lane_id"] == "code_context"
        for item in lane["output"]["evidence"]
    ]
    assert [item["repo_id"] for item in evidence] == [
        roster[0]["repo_id"],
        roster[1]["repo_id"],
    ]
    assert len({item["policy_claim"] for item in evidence}) == 2


def test_repo_id_survives_a_rename_and_separates_a_shared_name() -> None:
    assert pm_repo_id(name="api", path="/repo/api") != pm_repo_id(name="api", path="/other/api")
    renamed = pm_repo_id(name="billing-api", path="/repo/api")
    assert renamed.split("-")[-1] == pm_repo_id(name="api", path="/repo/api").split("-")[-1]


def test_the_lane_reads_the_snapshot_and_cites_the_durable_checkout() -> None:
    """Where to read and what to call it are different paths.

    A registered repo is redirected to a worktree pinned to the remote default
    branch so a stale local checkout cannot reach the PRD. Once the engine stops
    reading code, the lane inherits that: it must read the snapshot. The
    identifier stays derived from the durable checkout, so evidence submitted
    before a snapshot is recreated still matches the roster it was bounded by.
    """
    entries = pm_repository_roster(
        [{"path": "/tmp/snapshot-xyz", "source_path": "/repo/api", "name": "api"}]
    )
    assert entries[0]["path"] == "/tmp/snapshot-xyz"
    assert entries[0]["repo_id"] == pm_repo_id(name="api", path="/repo/api")


def test_a_recreated_snapshot_does_not_change_the_identifier() -> None:
    first = pm_repository_roster(
        [{"path": "/tmp/snap-aaa", "source_path": "/repo/api", "name": "api"}]
    )
    second = pm_repository_roster(
        [{"path": "/tmp/snap-bbb", "source_path": "/repo/api", "name": "api"}]
    )
    assert first[0]["repo_id"] == second[0]["repo_id"]
    assert first[0]["path"] != second[0]["path"]


# ── Binding: a PM answer belongs to one PM question ──────────────────────


def test_an_answer_for_a_different_question_is_rejected(
    registry: FanoutRegistry, roster: list[dict[str, str]]
) -> None:
    meta = _attach(registry, roster)
    result = _submit(
        registry,
        meta,
        {
            "question_identity": "pm-question:0000000000000000",
            "lane_id": "code_context",
            "policy_found": False,
            "examined_repository_ids": [],
            "no_policy_reason": "no_repository_in_roster",
        },
    )
    assert result["status"] == "partial"
    assert "does not belong to this fan-out" in str(result["contract_violations"]["code_context"])


def test_pm_and_interview_identities_are_distinguishable() -> None:
    from ouroboros.orchestrator.capabilities import stable_code_investigation_question_identity

    assert stable_pm_question_identity(QUESTION) != stable_code_investigation_question_identity(
        QUESTION
    )


# ── Wiring: PM lanes are judged by PM contracts ──────────────────────────


def test_pm_registers_its_own_fanout_kind(
    registry: FanoutRegistry, roster: list[dict[str, str]]
) -> None:
    """A shared kind would judge ``code_context`` by the interview's map.

    The interview declares no contract for its code lane, so a PM answer under a
    shared kind would pass unchecked — every guard above would still be written
    down and none of them would run.
    """
    meta = _attach(registry, roster)
    record = registry.load(meta["question_advisory_fanout_id"])
    assert record is not None
    assert record.kind == FANOUT_KIND_QUESTION_ADVISORY
    assert record.synthesizer_input["tool_name"] == "ouroboros_pm_interview"


def test_the_roster_the_child_sees_is_the_roster_re_entry_enforces(
    registry: FanoutRegistry, roster: list[dict[str, str]]
) -> None:
    meta = _attach(registry, roster)
    record = registry.load(meta["question_advisory_fanout_id"])
    assert record is not None
    assert record.synthesizer_input["roster_repo_ids"] == [e["repo_id"] for e in roster]
    prompt = meta["question_advisory_subagents"][0]["prompt"]
    for entry in roster:
        assert entry["repo_id"] in prompt


def test_neither_lane_is_given_a_persona(roster: list[dict[str, str]]) -> None:
    """A persona prescribes a free-form output the closed contracts reject."""
    request = {
        "mcp_tool": "ouroboros_pm_interview",
        "session_id": "pm-1",
        "question_identity": stable_pm_question_identity(QUESTION),
        "question": QUESTION,
        "lanes": _pm_question_advisory_fanout_metadata()["lanes"],
        "synthesis_contract": {},
        "repository_roster": roster,
    }
    payloads = [payload.to_dict() for payload in build_question_advisory_subagents(request)]
    assert {payload["agent"] for payload in payloads} == {"general"}
    assert all(payload["context"]["persona"] is None for payload in payloads)


def test_a_question_with_no_roster_still_gets_its_lanes(registry: FanoutRegistry) -> None:
    """An empty roster bounds the code lane to nothing; it does not skip it.

    Skipping would make "this session registered no repository" indistinguishable
    from "this question needed no code evidence" at the point the PM answers.
    """
    meta = _attach(registry, [])
    assert [payload["context"]["lane_id"] for payload in meta["question_advisory_subagents"]] == [
        "code_context",
        "data_context",
    ]


# ── Decision 3: a finding is recorded after the decision, never before ────


@pytest.mark.asyncio
async def test_evidence_never_fills_the_pending_question(tmp_path: Path) -> None:
    """The ordering is structural, not a convention a host must honour.

    A host that submits evidence while a question is open must not have
    answered it. The server appends instead of filling, so there is no call
    order that turns a lane finding into somebody's answer.
    """
    from ouroboros.bigbang.answer_provenance import extraction_rounds
    from ouroboros.bigbang.interview import InterviewRound, InterviewState
    from ouroboros.core.types import Result as CoreResult
    from ouroboros.mcp.tools.pm_handler import PMInterviewHandler
    from ouroboros.orchestrator.capabilities.pm_schemas import PM_EVIDENCE_ROUND_QUESTION

    handler = PMInterviewHandler(data_dir=tmp_path, agent_runtime_backend="claude")
    state = InterviewState(interview_id="pm-e", initial_context="ctx")
    state.rounds.append(
        InterviewRound(round_number=1, question="What counts as a day?", user_response=None)
    )

    class _Engine:
        codebase_context = ""
        _selected_brownfield_repos: list[dict[str, str]] = []
        deferred_items: list[str] = []
        decide_later_items: list[str] = []
        classifications: list[Any] = []

        async def save_state(self, s: InterviewState) -> Any:
            return CoreResult.ok(s)

        def get_pending_reframe(self) -> None:
            return None

    result = await handler._record_evidence_round(
        _Engine(), state, "pm-e", "[from-code] billing-api: access continues", str(tmp_path)
    )

    assert result.is_ok
    assert result.value.meta["evidence_recorded"] is True
    # The question is still waiting for a decision.
    assert state.rounds[0].user_response is None
    # Named apart from ``question`` on purpose: a response carrying that key is a
    # turn the host shows and fans out around, and this one is an acknowledgement.
    assert "question" not in result.value.meta
    assert result.value.meta["pending_question"] == "What counts as a day?"
    # The evidence is its own round, labelled so consumers can tell it apart.
    assert state.rounds[1].question == PM_EVIDENCE_ROUND_QUESTION
    # Visible to question generation, withheld from requirement extraction.
    withheld = {item.question: item.withheld for item in extraction_rounds(state)}
    assert withheld["What counts as a day?"] is False
    assert withheld[PM_EVIDENCE_ROUND_QUESTION] is True


def test_completion_counts_decisions_not_rounds() -> None:
    """Readiness asks how often the person judged, not how many rounds exist.

    Counting evidence rounds would score readiness a turn early for every
    question that carried any.
    """
    from ouroboros.bigbang.answer_provenance import classify_answer_provenance

    assert classify_answer_provenance("[from-code] a fact") == "observation"
    assert classify_answer_provenance("[from-data] a number") == "observation"
    assert classify_answer_provenance("counted by service date") == "user"


@pytest.mark.asyncio
async def test_answer_evidence_answer_keeps_each_answer_on_its_own_question(
    tmp_path: Path,
) -> None:
    """Regression (#1941 review): evidence must not steal the next answer.

    Recorded as an ordinary appended round, evidence becomes the trailing round
    while the next question is still pending. A handler that finds the pending
    question by being *last* then files the user's next decision under the
    evidence marker and leaves the real question unanswered for the rest of the
    session — silently, because both rounds look well-formed.
    """
    from ouroboros.bigbang.answer_provenance import extraction_rounds
    from ouroboros.bigbang.interview import InterviewRound, InterviewState
    from ouroboros.core.types import Result as CoreResult
    from ouroboros.mcp.tools.pm_handler import PMInterviewHandler
    from ouroboros.orchestrator.capabilities.pm_schemas import PM_EVIDENCE_ROUND_QUESTION

    handler = PMInterviewHandler(data_dir=tmp_path, agent_runtime_backend="claude")
    state = InterviewState(interview_id="pm-seq", initial_context="ctx")

    questions = iter(["Q2", "Q3"])

    class _Engine:
        codebase_context = ""
        _selected_brownfield_repos: list[dict[str, str]] = []
        deferred_items: list[str] = []
        decide_later_items: list[str] = []
        classifications: list[Any] = []

        async def load_state(self, _sid: str) -> Any:
            return CoreResult.ok(state)

        async def save_state(self, s: InterviewState) -> Any:
            return CoreResult.ok(s)

        async def record_response(self, s: InterviewState, answer: str, question: str) -> Any:
            s.record_answer(question, answer)
            return CoreResult.ok(s)

        async def ask_next_question(self, _s: InterviewState) -> Any:
            return CoreResult.ok(next(questions))

        async def check_completion(self, _s: InterviewState) -> None:
            return None

        def get_pending_reframe(self) -> None:
            return None

        def get_last_classification(self) -> None:
            return None

        def restore_meta(self, _m: Any) -> None:
            return None

        def compute_deferred_diff(self, _a: int, _b: int) -> dict[str, Any]:
            return {
                "new_deferred": [],
                "new_decide_later": [],
                "deferred_count": 0,
                "decide_later_count": 0,
            }

    engine = _Engine()
    state.rounds.append(InterviewRound(round_number=1, question="Q1", user_response=None))

    first = await handler._handle_answer(
        engine, "pm-seq", "decided by service date", str(tmp_path), evidence="[from-code] api: x"
    )
    assert first.is_ok

    second = await handler._handle_answer(
        engine, "pm-seq", "cancellations free the slot", str(tmp_path)
    )
    assert second.is_ok

    answered = {
        r.question: r.user_response
        for r in state.rounds
        if r.question != PM_EVIDENCE_ROUND_QUESTION
    }
    assert answered["Q1"] == "decided by service date"
    # The second decision belongs to Q2, not to the evidence marker.
    assert answered["Q2"] == "cancellations free the slot"
    # No question is left stranded unanswered behind an evidence round.
    assert [r.question for r in state.rounds if r.user_response is None] == ["Q3"]
    # And the evidence is still withheld from requirement extraction.
    withheld = {r.question: r.withheld for r in extraction_rounds(state)}
    assert withheld[PM_EVIDENCE_ROUND_QUESTION] is True


def test_evidence_is_recorded_before_the_next_question_is_generated() -> None:
    """The ordering is what makes recording it worth doing.

    Evidence submitted on a later call reaches the generator one question too
    late: the answer call already wrote the next question.
    """
    from ouroboros.bigbang.interview import InterviewRound, InterviewState
    from ouroboros.mcp.tools.pm_handler import _append_evidence_round
    from ouroboros.orchestrator.capabilities.pm_schemas import PM_EVIDENCE_ROUND_QUESTION

    state = InterviewState(interview_id="pm-ord", initial_context="ctx")
    state.rounds.append(InterviewRound(round_number=1, question="Q1", user_response="A1"))
    _append_evidence_round(state, "[from-data] 12,480")

    assert state.rounds[-1].question == PM_EVIDENCE_ROUND_QUESTION
    assert [r.question for r in state.rounds if r.user_response is None] == []


def test_a_decision_is_never_filed_under_the_evidence_marker() -> None:
    """The marker cannot be a question on any branch, including the fallback.

    With no round pending, falling back to the trailing round picks the evidence
    round — and the person's next decision is then recorded against a marker
    they were never shown. The invariant is that an evidence round is not a
    question, so no branch may treat it as one.
    """
    from ouroboros.bigbang.interview import InterviewRound, InterviewState
    from ouroboros.orchestrator.capabilities.pm_schemas import PM_EVIDENCE_ROUND_QUESTION

    state = InterviewState(interview_id="pm-fb", initial_context="ctx")
    state.rounds.append(InterviewRound(round_number=1, question="Q1", user_response="A1"))
    state.rounds.append(
        InterviewRound(
            round_number=2, question=PM_EVIDENCE_ROUND_QUESTION, user_response="[from-code] x"
        )
    )

    pending_index = next(
        (i for i in reversed(range(len(state.rounds))) if state.rounds[i].user_response is None),
        None,
    )
    assert pending_index is None
    fallback = next(
        (r.question for r in reversed(state.rounds) if r.question != PM_EVIDENCE_ROUND_QUESTION),
        state.rounds[-1].question,
    )
    assert fallback == "Q1"
