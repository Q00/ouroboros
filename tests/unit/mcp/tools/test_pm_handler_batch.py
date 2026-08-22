"""Batched PM turns (RFC #2222 revision 4): asked whole, answered whole.

The decisions these hold:

* **A turn persists nothing when it asks.** No question-only round, no pending
  member list. The transcript holds finished rounds only, so there is no second
  place a turn is remembered and nothing to reconcile after an interruption.
* **A turn's answers arrive together**, each holding its own question. Answer
  and question are never matched against remembered state, so a question whose
  text recurs later is just another question.
* **An answer without its question is refused.** Nothing is remembered to file
  it under, and the round behind it is one somebody already answered.
* **A call with no answers plans a fresh turn** rather than restoring one.
* Every question shown keeps its evidence: one advisory envelope per question,
  none shared, none skipped.
"""

from __future__ import annotations

import asyncio
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
    state = _answered_state("pm_batch_issue")
    assert (await engine.save_state(state)).is_ok
    _save_pm_meta(state.interview_id, engine, cwd=str(tmp_path), data_dir=tmp_path)
    engine.plan_next_turns = AsyncMock(
        return_value=Result.ok([_plan(Q_PRIMARY), _plan(Q_SECOND), _plan(Q_THIRD)])
    )
    handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

    result = await handler.handle(
        {
            "session_id": state.interview_id,
            "answer": "A4",
            "last_question": "Q4",
            "cwd": str(tmp_path),
        }
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

    # Asking persists nothing. The transcript holds finished rounds only, and
    # this turn's questions live on the wire — the second place they used to be
    # remembered is what every replay and ordering defect lived in.
    reloaded = (await engine.load_state(state.interview_id)).value
    assert all(r.user_response is not None for r in reloaded.rounds)
    assert not any(r.question in (Q_PRIMARY, Q_SECOND, Q_THIRD) for r in reloaded.rounds)
    saved = _load_meta(state.interview_id, tmp_path)
    assert "pending_batch" not in saved


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
        {
            "session_id": state.interview_id,
            "answer": "A4",
            "last_question": "Q4",
            "cwd": str(tmp_path),
        }
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
    state = _answered_state("pm_batch_inline")
    assert (await engine.save_state(state)).is_ok
    _save_pm_meta(state.interview_id, engine, cwd=str(tmp_path), data_dir=tmp_path)
    engine.plan_next_turns = AsyncMock(return_value=Result.ok([_plan(Q_PRIMARY), _plan(Q_SECOND)]))
    handler = PMInterviewHandler(
        pm_engine=engine,
        data_dir=tmp_path,
        fanout_registry=registry,
    )

    result = await handler.handle(
        {
            "session_id": state.interview_id,
            "answer": "A4",
            "last_question": "Q4",
            "cwd": str(tmp_path),
        }
    )

    assert result.is_ok
    for envelope in result.value.meta["question_advisories"]:
        for payload in envelope["question_advisory_subagents"]:
            assert "Answer Contract" in payload["prompt"]


@pytest.mark.asyncio
async def test_two_turns_answered_concurrently_both_survive(tmp_path: Path) -> None:
    """Two calls for one interview do not overwrite each other's rounds.

    A turn is one write now, but two of them can still be in flight: a host
    that retried, or two sessions on one interview. Unserialized, the later
    write carries a state that never saw the earlier one and those answers are
    gone. ``save_state`` is made to yield so the interleave is scheduled rather
    than hoped for — without the per-interview lock this test loses a turn.
    """
    engine = _engine(tmp_path)
    state = _answered_state("pm_batch_concurrent")
    assert (await engine.save_state(state)).is_ok
    _save_pm_meta(state.interview_id, engine, cwd=str(tmp_path), data_dir=tmp_path)
    engine.plan_next_turns = AsyncMock(return_value=Result.ok([_plan("Next question?")]))

    inner_save = engine.save_state

    async def _yielding_save(saved_state: InterviewState) -> object:
        await asyncio.sleep(0)
        return await inner_save(saved_state)

    engine.save_state = _yielding_save  # type: ignore[method-assign]
    handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

    first, second = await asyncio.gather(
        handler.handle(
            {
                "session_id": state.interview_id,
                "answers": [{"question": Q_PRIMARY, "answer": "The review workflow."}],
                "cwd": str(tmp_path),
            }
        ),
        handler.handle(
            {
                "session_id": state.interview_id,
                "answers": [{"question": Q_SECOND, "answer": "Retention is 90 days."}],
                "cwd": str(tmp_path),
            }
        ),
    )

    assert first.is_ok and second.is_ok
    reloaded = (await engine.load_state(state.interview_id)).value
    answered = {r.question: r.user_response for r in reloaded.rounds}
    assert answered[Q_PRIMARY] == "The review workflow."
    assert answered[Q_SECOND] == "Retention is 90 days."


@pytest.mark.asyncio
async def test_a_turn_is_recorded_whole(tmp_path: Path) -> None:
    """Three questions asked, three answers back in one call, three rounds.

    This is the shape that removed the pending list: a turn arrives complete,
    so there is no interval in which some of it is recorded and the rest is
    remembered somewhere else.
    """
    engine = _engine(tmp_path)
    state = _answered_state("pm_batch_whole")
    assert (await engine.save_state(state)).is_ok
    _save_pm_meta(state.interview_id, engine, cwd=str(tmp_path), data_dir=tmp_path)
    engine.plan_next_turns = AsyncMock(return_value=Result.ok([_plan("Next question?")]))
    handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

    result = await handler.handle(
        {
            "session_id": state.interview_id,
            "answers": [
                {"question": Q_PRIMARY, "answer": "The review workflow."},
                {"question": Q_SECOND, "answer": "Retention is 90 days."},
                {"question": Q_THIRD, "answer": "[decide_later]"},
            ],
            "cwd": str(tmp_path),
        }
    )

    assert result.is_ok
    reloaded = (await engine.load_state(state.interview_id)).value
    recorded = [(r.question, r.user_response) for r in reloaded.rounds[3:]]
    assert [q for q, _ in recorded] == [Q_PRIMARY, Q_SECOND, Q_THIRD]
    assert recorded[0][1] == "The review workflow."
    assert all(r.user_response is not None for r in reloaded.rounds)
    # The turn resolved, so the next one was planned.
    engine.plan_next_turns.assert_awaited_once()
    assert "pending_batch" not in _load_meta(state.interview_id, tmp_path)


@pytest.mark.asyncio
async def test_an_answer_without_its_question_is_refused(tmp_path: Path) -> None:
    """Nothing is remembered to file it under, so nothing is guessed.

    The round behind an unnamed answer is one somebody already answered;
    binding a second answer to it would overwrite a decision that was made.
    """
    engine = _engine(tmp_path)
    state = _answered_state("pm_batch_unnamed")
    assert (await engine.save_state(state)).is_ok
    _save_pm_meta(state.interview_id, engine, cwd=str(tmp_path), data_dir=tmp_path)
    engine.plan_next_turns = AsyncMock()
    handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

    result = await handler.handle(
        {"session_id": state.interview_id, "answer": "A decision.", "cwd": str(tmp_path)}
    )

    assert result.is_err
    assert "last_question" in str(result.error)
    reloaded = (await engine.load_state(state.interview_id)).value
    assert len(reloaded.rounds) == 3


@pytest.mark.asyncio
async def test_a_question_whose_text_recurs_is_just_another_round(tmp_path: Path) -> None:
    """No answer is suppressed for resembling an older one.

    The guard that did that existed to make an interrupted retry idempotent
    across two files. With one write there is no interruption to repair, and a
    PM who is asked something again and answers it again has made a decision
    that belongs in the transcript.
    """
    engine = _engine(tmp_path)
    state = _answered_state("pm_batch_recurs")
    state.rounds.append(
        InterviewRound(round_number=4, question=Q_PRIMARY, user_response="The review workflow.")
    )
    assert (await engine.save_state(state)).is_ok
    _save_pm_meta(state.interview_id, engine, cwd=str(tmp_path), data_dir=tmp_path)
    engine.plan_next_turns = AsyncMock(return_value=Result.ok([_plan("Next question?")]))
    handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

    result = await handler.handle(
        {
            "session_id": state.interview_id,
            "answers": [{"question": Q_PRIMARY, "answer": "The review workflow."}],
            "cwd": str(tmp_path),
        }
    )

    assert result.is_ok
    reloaded = (await engine.load_state(state.interview_id)).value
    assert [r.question for r in reloaded.rounds].count(Q_PRIMARY) == 2
    assert len(reloaded.rounds) == 5


@pytest.mark.asyncio
async def test_a_reconnect_plans_a_fresh_turn_and_leaves_nothing_half_written(
    tmp_path: Path,
) -> None:
    """A host that lost its turn gets a new one, not the old one restored.

    Restoring it would need the turn to have been remembered somewhere, which
    is the second place revision 4 removed. Planning again costs one question
    and leaves the transcript what it always is: finished rounds.
    """
    engine = _engine(tmp_path)
    state = _answered_state("pm_batch_reconnect")
    assert (await engine.save_state(state)).is_ok
    _save_pm_meta(state.interview_id, engine, cwd=str(tmp_path), data_dir=tmp_path)
    engine.plan_next_turns = AsyncMock(
        return_value=Result.ok([_plan("A freshly planned question?")])
    )
    handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

    result = await handler.handle({"session_id": state.interview_id, "cwd": str(tmp_path)})

    assert result.is_ok
    assert result.value.meta["question"] == "A freshly planned question?"
    reloaded = (await engine.load_state(state.interview_id)).value
    assert len(reloaded.rounds) == 3
    assert all(r.user_response is not None for r in reloaded.rounds)


@pytest.mark.asyncio
async def test_plugin_mode_never_enters_the_batch_contract(tmp_path: Path) -> None:
    """The plugin runtime has no batch to persist, so it persists none.

    Batching rides the in-process planner (RFC #2222, "Deliberately not decided
    here"). This pins the property that keeps that true: the runtime that
    dispatches to a child session neither plans a batch nor writes pending
    members, so there is no batch state for its answer path to bypass.
    """
    engine = _engine(tmp_path)
    state = _answered_state("pm_batch_plugin", pending=Q_PRIMARY)
    assert (await engine.save_state(state)).is_ok
    _save_pm_meta(state.interview_id, engine, cwd=str(tmp_path), data_dir=tmp_path)
    engine.plan_next_turns = AsyncMock()
    handler = PMInterviewHandler(
        pm_engine=engine,
        data_dir=tmp_path,
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )

    result = await handler.handle(
        {
            "session_id": state.interview_id,
            "answer": "The review workflow.",
            "last_question": Q_PRIMARY,
            "cwd": str(tmp_path),
        }
    )

    assert result.is_ok
    engine.plan_next_turns.assert_not_called()
    assert "pending_batch" not in _load_meta(state.interview_id, tmp_path)
    assert "question_batch" not in (result.value.meta or {})
