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
from ouroboros.mcp.tools.pm_batch import record_turn_answers, turn_answers
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
    """RFC #2222: the explanation is fetched; what the child works from is not.

    A turn's response used to carry every lane's full brief twice plus a copy
    of the capability metadata per envelope, and a batch outgrew what a host
    accepts inline. The stub keeps what a child cannot work without — its
    answer schema, where it may look, what it has already found — and leaves
    behind only the prose that explains them, one lane-scoped fetch away.
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
            lane_id = payload["context"]["lane_id"]
            # The stub names the fetch, and carries no brief prose.
            assert "ouroboros_fetch_artifact" in stub
            assert bundle_id in stub
            assert "Answer Contract" not in stub
            # A failed fetch no longer ends the lane, so nothing tells it to say so.
            assert "UNDISPATCHED" not in stub
            # What it cannot work without rides the stub: the answer shape, the
            # lane's own empty state, and where it may look.
            assert "`question_identity` and `lane_id`" in stub
            assert "answer the empty" in stub
            # Step 3 follows the lane's own answer shape, not its name.
            assert ("data tools" in stub) == (lane_id == "data_context")
            # The full brief is one scoped fetch away.
            fetched = store.fetch_lane(bundle_id, lane_id).body
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


def test_batch_answer_schema_is_closed_and_bounded() -> None:
    schema = PMInterviewHandler().definition.to_input_schema()["properties"]["answers"]

    assert schema["minItems"] == 1
    assert schema["maxItems"] == 3
    assert schema["items"] == {
        "type": "object",
        "properties": {
            "question": {"type": "string", "minLength": 1},
            "answer": {"type": "string", "minLength": 1},
        },
        "required": ["question", "answer"],
        "additionalProperties": False,
    }


def test_answer_schema_makes_singular_and_batch_forms_mutually_exclusive() -> None:
    schema = PMInterviewHandler().definition.to_input_schema()

    assert schema["not"] == {"required": ["answer", "answers"]}


@pytest.mark.parametrize(
    ("answers", "error_fragment"),
    [
        (
            [{"question": f"Question {index}?", "answer": "A"} for index in range(4)],
            "between one and three",
        ),
        ([], "between one and three"),
        ([{"question": Q_PRIMARY, "answer": "A", "identity": "invented"}], "only"),
        ([{"question": 42, "answer": "A"}], "non-empty string"),
        ([{"question": Q_PRIMARY, "answer": 42}], "non-empty string"),
        (
            [
                {"question": Q_PRIMARY, "answer": "A"},
                {"question": f"  {Q_PRIMARY}  ", "answer": "B"},
            ],
            "duplicate question identity",
        ),
    ],
)
def test_malformed_and_duplicate_batch_answers_are_rejected(
    answers: object,
    error_fragment: str,
) -> None:
    pairs, error = turn_answers(answers, None, None)

    assert pairs == []
    assert error is not None
    assert error_fragment in error


def test_singular_and_batch_answer_forms_are_rejected_together() -> None:
    pairs, error = turn_answers(
        [{"question": Q_PRIMARY, "answer": "The review workflow."}],
        "A singular answer that must not be discarded.",
        Q_PRIMARY,
    )

    assert pairs == []
    assert error is not None
    assert "mutually exclusive" in error


def test_valid_batch_answers_preserve_the_producer_attention_budget() -> None:
    answers = [
        {"question": Q_PRIMARY, "answer": "The review workflow."},
        {"question": Q_SECOND, "answer": "Retention is 90 days."},
        {"question": Q_THIRD, "answer": "After launch."},
    ]

    pairs, error = turn_answers(answers, None, None)

    assert error is None
    assert pairs == [(entry["question"], entry["answer"]) for entry in answers]


@pytest.mark.asyncio
async def test_persisted_planned_question_rejects_a_caller_invented_identity(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    state = _answered_state("pm_batch_identity", pending=Q_PRIMARY)
    assert (await engine.save_state(state)).is_ok
    _save_pm_meta(state.interview_id, engine, cwd=str(tmp_path), data_dir=tmp_path)
    engine.plan_next_turns = AsyncMock()
    handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

    result = await handler.handle(
        {
            "session_id": state.interview_id,
            "answers": [{"question": "A different question?", "answer": "A"}],
            "cwd": str(tmp_path),
        }
    )

    assert result.is_err
    assert "persisted planned questions" in str(result.error)
    engine.plan_next_turns.assert_not_called()
    reloaded = (await engine.load_state(state.interview_id)).value
    assert reloaded.rounds[-1].question == Q_PRIMARY
    assert reloaded.rounds[-1].user_response is None


@pytest.mark.asyncio
async def test_legacy_single_answer_may_replace_a_stale_pending_question(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    state = _answered_state("pm_legacy_question_repair", pending="Stale placeholder?")
    assert (await engine.save_state(state)).is_ok
    _save_pm_meta(state.interview_id, engine, cwd=str(tmp_path), data_dir=tmp_path)
    engine.plan_next_turns = AsyncMock(return_value=Result.ok([_plan("Next question?")]))
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
    reloaded = (await engine.load_state(state.interview_id)).value
    assert reloaded.rounds[-1].question == Q_PRIMARY
    assert reloaded.rounds[-1].user_response == "The review workflow."


def test_persisted_planned_question_accepts_its_normalized_identity() -> None:
    pairs, error = turn_answers(
        [{"question": f"  {Q_PRIMARY}  ", "answer": "The review workflow."}],
        None,
        None,
        planned_questions=[Q_PRIMARY],
    )

    assert error is None
    assert pairs == [(Q_PRIMARY, "The review workflow.")]


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


@pytest.mark.asyncio
async def test_the_batch_transport_means_the_same_thing_in_plugin_mode(
    tmp_path: Path,
) -> None:
    """One public answer shape, one meaning, on every runtime.

    The plugin branch records answers on its own path. When `answers` was added
    to the in-process branch alone, a host that sent it here was answered with
    success while its decisions were never written — the shape changed meaning
    with the runtime, which is worse than not accepting it at all.

    Plugin mode still never *plans* a batch (RFC #2222 keeps the producer
    in-process); this is about what it does with the answers it is handed.
    """
    from ouroboros.mcp.tools.authoring_handlers import _plugin_load_state, _plugin_save_state

    state = _answered_state("pm_plugin_answers")
    assert (await _plugin_save_state(tmp_path, state)).is_ok
    handler = PMInterviewHandler(
        data_dir=tmp_path,
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )

    result = await handler.handle(
        {
            "session_id": state.interview_id,
            "answers": [
                {"question": Q_PRIMARY, "answer": "The review workflow."},
                {"question": Q_SECOND, "answer": "Retention is 90 days."},
            ],
            "cwd": str(tmp_path),
        }
    )

    assert result.is_ok
    reloaded = (await _plugin_load_state(tmp_path, state.interview_id)).value
    recorded = [(r.question, r.user_response) for r in reloaded.rounds[3:]]
    assert recorded == [
        (Q_PRIMARY, "The review workflow."),
        (Q_SECOND, "Retention is 90 days."),
    ]

    # Writing them down is half the turn; the child has to be handed them.
    # A turn's answers arrive under `answers`, which leaves the singular
    # `answer` empty — and the prompt used to decide whether there was
    # anything to resume with by reading exactly that field. The turns
    # carrying the most history took the branch that names none of it.
    prompt = result.value.meta["_subagent"]["prompt"]
    assert "## Conversation History" in prompt
    assert "The review workflow." in prompt
    assert "Retention is 90 days." in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("sentinel", ["[decide_later]", "[deferred]"])
async def test_a_skip_control_is_a_skip_on_every_runtime(tmp_path: Path, sentinel: str) -> None:
    """A control token is not something the user said.

    ``[decide_later]`` and ``[deferred]`` ask the server to leave a decision
    open. Recorded literally they become the opposite: a transcript in which
    the PM answered the question with the token's own text, and an open item
    the generated seed never hears about. Whether that happens turned on which
    runtime took the call.

    The postcondition is equality, not a spelling: the same call recorded on
    either runtime leaves the same round. Pinning the placeholder text instead
    would pass a plugin path that had merely copied today's wording.
    """
    from ouroboros.mcp.tools.authoring_handlers import _plugin_load_state, _plugin_save_state

    in_process_engine = _engine(tmp_path)
    in_process_state = _answered_state("pm_skip_in_process")
    assert (await in_process_engine.save_state(in_process_state)).is_ok
    settled = await record_turn_answers(
        in_process_engine, in_process_state, [(Q_PRIMARY, sentinel)]
    )
    assert settled.is_ok
    expected = settled.value.rounds[-1].user_response

    plugin_state = _answered_state("pm_skip_plugin")
    assert (await _plugin_save_state(tmp_path, plugin_state)).is_ok
    handler = PMInterviewHandler(
        data_dir=tmp_path,
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )

    result = await handler.handle(
        {
            "session_id": plugin_state.interview_id,
            "answers": [{"question": Q_PRIMARY, "answer": sentinel}],
            "cwd": str(tmp_path),
        }
    )

    assert result.is_ok
    reloaded = (await _plugin_load_state(tmp_path, plugin_state.interview_id)).value
    recorded = reloaded.rounds[-1]
    assert recorded.question == Q_PRIMARY
    assert recorded.user_response == expected
    assert recorded.user_response != sentinel
    # And the child reads the decision as open, not as an answer it may build on.
    prompt = result.value.meta["_subagent"]["prompt"]
    assert expected in prompt
    assert sentinel not in prompt


@pytest.mark.asyncio
async def test_a_stub_carries_what_the_lane_cannot_work_without(tmp_path: Path) -> None:
    """The stub is compact, not partial (RFC #2222 decision 6).

    Three things decide whether a lane can do its job at all: what shape its
    answer must take, what it has already found here, and where it may look.
    All three ride the stub, so a child that cannot reach the store still
    works. Only the prose explaining them stays behind the fetch.

    The two lanes are told different things about step 3, and the difference is
    read from each lane's own answer shape: the lane that cites ``repo_id`` is
    bounded by the roster, the one that does not measures what the host
    exposes.
    """
    from ouroboros.mcp.tools.pm_batch import externalize_advisory_payloads
    from ouroboros.orchestrator.capabilities.pm_schemas import (
        _interview_data_evidence_answer_contract,
        pm_code_context_answer_contract,
        pm_repository_roster,
    )
    from ouroboros.persistence.artifact_store import ArtifactStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = ArtifactStore.for_project(workspace)
    store.initialize()
    roster = pm_repository_roster([str(tmp_path / "podo-backend")])
    meta = {
        "question_advisory_fanout_id": "fanout_stub",
        "question_advisory_subagents": [
            {
                "prompt": "FULL BRIEF",
                "context": {"lane_id": lane, "question": Q_PRIMARY, "session_id": "s"},
            }
            for lane in ("code_context", "data_context")
        ],
        "question_advisory_request": {
            "lanes": [
                {"lane_id": "code_context", "answer_contract": pm_code_context_answer_contract()},
                {
                    "lane_id": "data_context",
                    "answer_contract": _interview_data_evidence_answer_contract(),
                },
            ],
            "repository_roster": roster,
            "recent_findings": {
                "code_context": [{"contract_id": "fanout_earlier", "lane_id": "code_context"}]
            },
        },
    }

    await externalize_advisory_payloads(meta, store)

    code, data = (p["prompt"] for p in meta["question_advisory_subagents"])
    # 1 — the empty state, named from each lane's own contract.
    assert "not_a_policy_question" in code
    assert "not_a_measurement" in data
    # 2 — the findings this lane may reuse, ids only, offered as a shelf to
    # choose from rather than a list to work through.
    assert "`lane_id: code_context` and no `contract_id`" in code
    assert "fanout_earlier" not in code, "ids do not travel; the tool answers on request"
    assert "nothing to" in data and "reuse" in data
    # 3 — where each may look.
    assert roster[0]["repo_id"] in code and str(tmp_path / "podo-backend") in code
    assert "data tools" in data and roster[0]["repo_id"] not in data
    # The answer shape, named in words — no filled-in claim for a child short on
    # evidence to adopt, and nothing in the block a copy could be made from.
    assert "`examined`: one entry per repository" in code
    assert "`data_needed: true`" in data
    assert "`path` is relative to the repository" in code
    assert "never a row, a name, or an identifier" in data
    assert "```json" not in code.split("## Answer")[1]
    assert "plain_statement" in code and "plain_statement" not in data
    # Compact: the full brief is several times this, and stays fetchable.
    assert len(code) < 6000
    stored = store.fetch_lane("advisory-prompts:fanout_stub", "code_context").body
    assert stored == "FULL BRIEF"


def test_every_answer_spec_names_what_its_contract_requires() -> None:
    """The prompt describes the contract in words, so the words must be complete.

    No worked example: an example is a claim already written in the answer's
    shape, and a child short on evidence would have one in front of it needing
    only its identifiers changed. What replaces it is the field list, which can
    go stale in the one way that matters — a contract gaining a required field
    nothing tells the child to send. Every required name is checked against the
    text, and the text is keyed by ``contract_id`` so a version bump falls back
    to the schema rather than to a description of the shape before it.
    """
    from ouroboros.mcp.tools.pm_batch import _ANSWER_SPECS, _answer_section, _lean_schema
    from ouroboros.orchestrator.capabilities.pm_schemas import (
        _interview_data_evidence_answer_contract,
        pm_code_context_answer_contract,
    )

    contracts = {
        c["contract_id"]: c
        for c in (pm_code_context_answer_contract(), _interview_data_evidence_answer_contract())
    }
    assert set(_ANSWER_SPECS) == set(contracts)

    def required_names(node: object) -> set[str]:
        found: set[str] = set()
        if isinstance(node, dict):
            if isinstance(node.get("required"), list):
                found |= {str(name) for name in node["required"]}
            for value in node.values():
                found |= required_names(value)
        elif isinstance(node, list):
            for item in node:
                found |= required_names(item)
        return found

    for contract_id in _ANSWER_SPECS:
        contract = contracts[contract_id]
        # What the child is shown, not the template it is built from: the
        # empty state's reasons are filled in from the schema at render time.
        rendered = _answer_section(contract, _lean_schema(contract))
        missing = sorted(
            name
            for name in required_names(contract["response_model_schema"])
            if name not in rendered
        )
        assert not missing, f"{contract_id} does not tell the child about: {missing}"
