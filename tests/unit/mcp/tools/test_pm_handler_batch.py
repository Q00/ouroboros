"""Batched PM turns (RFC #2222): issue, partial answering, per-member routing.

The decisions these hold:

* A batched turn leaves **no** question-only rounds in core interview state —
  the engine fills the trailing unanswered round on record, so a second
  pending round is a question waiting to be silently overwritten. Batch
  pending state lives in PM meta, and every member is recorded question and
  answer together.
* Every question shown keeps its evidence: one advisory envelope per batch
  member, none shared, none skipped.
* An unanswered member stays pending. Answering one member returns the rest;
  only the answer that resolves the batch reaches completion checking and
  next-turn generation.
* Skip sentinels are guarded by the member's own classification, not by
  whichever question was classified last.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.bigbang.interview import InterviewRound, InterviewState
from ouroboros.bigbang.pm_interview import PMInterviewEngine, PMInterviewTurnPlan
from ouroboros.bigbang.question_classifier import (
    ClassificationResult,
    QuestionCategory,
)
from ouroboros.core.types import Result
from ouroboros.mcp.tools.pm_handler import (
    PMInterviewHandler,
    _meta_path,
    _save_pm_meta,
)

Q_PRIMARY = "Which user workflow matters most?"
Q_SECOND = "What data retention constraint applies?"
Q_THIRD = "Which decisions can wait until launch scope is known?"


def _plan(
    question: str,
    *,
    category: QuestionCategory = QuestionCategory.PLANNING,
    decide_later: bool = False,
) -> PMInterviewTurnPlan:
    return PMInterviewTurnPlan(
        question=question,
        ambiguity=None,
        classification=ClassificationResult(
            original_question=question,
            category=category,
            reframed_question=question,
            reasoning="test",
            decide_later=decide_later,
        ),
        raw_payload={"next_question": question},
    )


def _engine(tmp_path: Path) -> PMInterviewEngine:
    return PMInterviewEngine.create(
        llm_adapter=MagicMock(),
        model="test-model",
        state_dir=tmp_path,
    )


def _answered_state(interview_id: str, *, pending: str | None = None) -> InterviewState:
    rounds = [
        InterviewRound(round_number=1, question="Q1", user_response="A1"),
        InterviewRound(round_number=2, question="Q2", user_response="A2"),
        InterviewRound(round_number=3, question="Q3", user_response="A3"),
    ]
    if pending is not None:
        rounds.append(InterviewRound(round_number=4, question=pending, user_response=None))
    return InterviewState(
        interview_id=interview_id,
        initial_context="Build an analytics workflow",
        rounds=rounds,
    )


def _load_meta(session_id: str, data_dir: Path) -> dict:
    return json.loads(_meta_path(session_id, data_dir).read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_a_batched_turn_issues_every_question_with_its_own_evidence(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    state = _answered_state("pm_batch_issue", pending="Q4")
    assert (await engine.save_state(state)).is_ok
    _save_pm_meta(state.interview_id, engine, cwd=str(tmp_path), data_dir=tmp_path)
    engine.plan_next_turns = AsyncMock(
        return_value=Result.ok([_plan(Q_PRIMARY), _plan(Q_SECOND), _plan(Q_THIRD)])
    )
    handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

    result = await handler.handle(
        {"session_id": state.interview_id, "answer": "A4", "cwd": str(tmp_path)}
    )

    assert result.is_ok
    meta = result.value.meta
    assert [entry["question"] for entry in meta["question_batch"]] == [
        Q_PRIMARY,
        Q_SECOND,
        Q_THIRD,
    ]
    # One advisory envelope per question, each with its own payloads.
    advisories = meta["question_advisories"]
    assert [envelope["question"] for envelope in advisories] == [Q_PRIMARY, Q_SECOND, Q_THIRD]
    for envelope in advisories:
        lane_ids = [
            payload["context"]["lane_id"] for payload in envelope["question_advisory_subagents"]
        ]
        assert sorted(lane_ids) == ["code_context", "data_context"]
    # The dispatch text carries every question and every envelope's block.
    text = result.value.text_content
    for question in (Q_PRIMARY, Q_SECOND, Q_THIRD):
        assert question in text

    # Core state holds no question-only rounds for the batch: the answer
    # filled Q4, and nothing pending was appended behind it.
    reloaded = (await engine.load_state(state.interview_id)).value
    assert all(r.user_response is not None for r in reloaded.rounds)

    # Batch pending state is persisted in PM meta, with per-member routing.
    saved = _load_meta(state.interview_id, tmp_path)
    assert [entry["question"] for entry in saved["pending_batch"]] == [
        Q_PRIMARY,
        Q_SECOND,
        Q_THIRD,
    ]


@pytest.mark.asyncio
async def test_answering_one_member_returns_the_rest_without_generating(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    state = _answered_state("pm_batch_partial")
    assert (await engine.save_state(state)).is_ok
    _save_pm_meta(
        state.interview_id,
        engine,
        cwd=str(tmp_path),
        data_dir=tmp_path,
        extra={
            "pending_batch": [
                {"question": Q_PRIMARY, "classification": "passthrough", "skip_eligible": False},
                {"question": Q_SECOND, "classification": "passthrough", "skip_eligible": False},
            ]
        },
    )
    engine.plan_next_turns = AsyncMock()
    handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

    result = await handler.handle(
        {
            "session_id": state.interview_id,
            "answer": "Retention is 90 days.",
            "last_question": Q_SECOND,
            "cwd": str(tmp_path),
        }
    )

    assert result.is_ok
    meta = result.value.meta
    assert [entry["question"] for entry in meta["question_batch"]] == [Q_PRIMARY]
    assert meta["is_complete"] is False
    engine.plan_next_turns.assert_not_called()

    # The answered member is a full question-and-answer round; the pending
    # member is nowhere in core state.
    reloaded = (await engine.load_state(state.interview_id)).value
    assert reloaded.rounds[-1].question == Q_SECOND
    assert reloaded.rounds[-1].user_response == "Retention is 90 days."
    assert all(r.user_response is not None for r in reloaded.rounds)
    saved = _load_meta(state.interview_id, tmp_path)
    assert [entry["question"] for entry in saved["pending_batch"]] == [Q_PRIMARY]


@pytest.mark.asyncio
async def test_resolving_the_batch_reaches_generation_and_clears_pending(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    state = _answered_state("pm_batch_resolve")
    assert (await engine.save_state(state)).is_ok
    _save_pm_meta(
        state.interview_id,
        engine,
        cwd=str(tmp_path),
        data_dir=tmp_path,
        extra={
            "pending_batch": [
                {"question": Q_PRIMARY, "classification": "passthrough", "skip_eligible": False},
            ]
        },
    )
    engine.plan_next_turns = AsyncMock(return_value=Result.ok([_plan("Next single question?")]))
    handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

    result = await handler.handle(
        {
            "session_id": state.interview_id,
            "answer": "The review workflow.",
            "last_question": Q_PRIMARY,
            "cwd": str(tmp_path),
        }
    )

    assert result.is_ok
    engine.plan_next_turns.assert_awaited_once()
    assert result.value.meta["question"] == "Next single question?"
    assert "question_batch" not in result.value.meta
    saved = _load_meta(state.interview_id, tmp_path)
    assert "pending_batch" not in saved


@pytest.mark.asyncio
async def test_batch_answer_without_last_question_is_refused_while_ambiguous(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    state = _answered_state("pm_batch_ambiguous")
    assert (await engine.save_state(state)).is_ok
    _save_pm_meta(
        state.interview_id,
        engine,
        cwd=str(tmp_path),
        data_dir=tmp_path,
        extra={
            "pending_batch": [
                {"question": Q_PRIMARY, "classification": "passthrough", "skip_eligible": False},
                {"question": Q_SECOND, "classification": "passthrough", "skip_eligible": False},
            ]
        },
    )
    handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

    result = await handler.handle(
        {"session_id": state.interview_id, "answer": "ambiguous", "cwd": str(tmp_path)}
    )

    assert result.is_err
    assert "last_question" in str(result.error)
    # Nothing was recorded against either pending member.
    reloaded = (await engine.load_state(state.interview_id)).value
    assert len(reloaded.rounds) == 3


@pytest.mark.asyncio
async def test_skip_sentinel_is_guarded_by_the_members_own_classification(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    state = _answered_state("pm_batch_sentinel")
    assert (await engine.save_state(state)).is_ok
    _save_pm_meta(
        state.interview_id,
        engine,
        cwd=str(tmp_path),
        data_dir=tmp_path,
        extra={
            "pending_batch": [
                {"question": Q_PRIMARY, "classification": "passthrough", "skip_eligible": False},
                {"question": Q_THIRD, "classification": "decide_later", "skip_eligible": True},
            ]
        },
    )
    engine.plan_next_turns = AsyncMock()
    handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

    # The decide-later member honours the sentinel...
    result = await handler.handle(
        {
            "session_id": state.interview_id,
            "answer": "[decide_later]",
            "last_question": Q_THIRD,
            "cwd": str(tmp_path),
        }
    )
    assert result.is_ok
    assert Q_THIRD in engine.decide_later_items

    # ...while the passthrough member records the same sentinel as a plain
    # answer rather than silently discarding it.
    result = await handler.handle(
        {
            "session_id": state.interview_id,
            "answer": "[decide_later]",
            "last_question": Q_PRIMARY,
            "cwd": str(tmp_path),
        }
    )
    assert result.is_ok
    reloaded = (await engine.load_state(state.interview_id)).value
    primary_round = next(r for r in reloaded.rounds if r.question == Q_PRIMARY)
    assert primary_round.user_response == "[decide_later]"
    assert Q_PRIMARY not in engine.decide_later_items


@pytest.mark.asyncio
async def test_briefs_travel_as_references_when_a_store_is_wired(tmp_path: Path) -> None:
    """RFC #2222: the wire carries questions and references; briefs are fetched.

    A turn's response used to carry every lane's full brief twice plus a copy
    of the capability metadata per envelope, and a batch outgrew what a host
    accepts inline. With a store wired, each payload prompt becomes a compact
    stub naming the bundle to fetch; the bundle returns the full brief, lane-
    scoped, through the same fetch path findings use.
    """
    from ouroboros.mcp.tools.fanout import FanoutRegistry
    from ouroboros.persistence.artifact_store import ArtifactStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = ArtifactStore.for_project(workspace)
    store.initialize()
    registry = FanoutRegistry(tmp_path / "fanout")

    engine = _engine(tmp_path)
    state = _answered_state("pm_batch_reference", pending="Q4")
    assert (await engine.save_state(state)).is_ok
    _save_pm_meta(state.interview_id, engine, cwd=str(tmp_path), data_dir=tmp_path)
    engine.plan_next_turns = AsyncMock(return_value=Result.ok([_plan(Q_PRIMARY), _plan(Q_SECOND)]))
    handler = PMInterviewHandler(
        pm_engine=engine,
        data_dir=tmp_path,
        fanout_registry=registry,
        findings_store=store,
    )

    result = await handler.handle(
        {"session_id": state.interview_id, "answer": "A4", "cwd": str(tmp_path)}
    )

    assert result.is_ok
    for envelope in result.value.meta["question_advisories"]:
        bundle_id = f"advisory-prompts:{envelope['question_advisory_fanout_id']}"
        for payload in envelope["question_advisory_subagents"]:
            stub = payload["prompt"]
            # The stub names the fetch, and carries no brief body.
            assert "ouroboros_fetch_artifact" in stub
            assert bundle_id in stub
            assert "Answer Contract" not in stub
            assert "UNDISPATCHED" in stub
            # The full brief is one scoped fetch away.
            fetched = store.fetch_lane(bundle_id, payload["context"]["lane_id"]).body
            assert "Answer Contract" in fetched
            assert envelope["question"] in fetched
        # The request no longer ships the capability metadata or lane catalog.
        request = envelope["question_advisory_request"]
        assert request["mcp_tool_capability"] == {"tool_name": "ouroboros_pm_interview"}
        assert "lanes" not in request
    # The dispatch text carries the stubs, not the briefs.
    assert "Answer Contract" not in result.value.text_content


@pytest.mark.asyncio
async def test_without_a_store_the_full_briefs_stay_inline(tmp_path: Path) -> None:
    """Fail-open: no store means the oversized-but-whole wire of today."""
    from ouroboros.mcp.tools.fanout import FanoutRegistry

    registry = FanoutRegistry(tmp_path / "fanout")
    engine = _engine(tmp_path)
    state = _answered_state("pm_batch_inline", pending="Q4")
    assert (await engine.save_state(state)).is_ok
    _save_pm_meta(state.interview_id, engine, cwd=str(tmp_path), data_dir=tmp_path)
    engine.plan_next_turns = AsyncMock(return_value=Result.ok([_plan(Q_PRIMARY), _plan(Q_SECOND)]))
    handler = PMInterviewHandler(
        pm_engine=engine,
        data_dir=tmp_path,
        fanout_registry=registry,
    )

    result = await handler.handle(
        {"session_id": state.interview_id, "answer": "A4", "cwd": str(tmp_path)}
    )

    assert result.is_ok
    for envelope in result.value.meta["question_advisories"]:
        for payload in envelope["question_advisory_subagents"]:
            assert "Answer Contract" in payload["prompt"]
