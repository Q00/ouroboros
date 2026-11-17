"""#3033: Seed QA repair instructions steer the repair and never become Seed constraints.

The Seed is the user-owned contract. A Seed QA repair round may append product
or runtime constraints and restore the ledger's own binding non-goals, but the
repair's Seed-authoring instructions (the lateral placeholder, the
ledger-preservation sentence, ...) must never land in the Seed's
``constraints``, ``acceptance_criteria`` or ``goal``. Every collaborator here is
a fake: no real agent CLI or LLM runs.
"""

from __future__ import annotations

from typing import Any

import pytest

from ouroboros.auto.adapters import EvaluateResult, LateralResult
from ouroboros.auto.interview_driver import (
    AutoInterviewDriver,
    FunctionInterviewBackend,
    InterviewTurn,
)
from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus, SeedDraftLedger
from ouroboros.auto.pipeline import AutoPipeline, _seed_with_repair_plan
from ouroboros.auto.seed_qa_repair import (
    AMBIGUITY_INSTRUCTION,
    DIAGNOSTIC_PROSE_INSTRUCTION,
    DIRECT_CRITERIA_INSTRUCTION,
    LEDGER_PRESERVATION_INSTRUCTION,
    LEGACY_LATERAL_PLACEHOLDER,
    seed_qa_repair_plan,
)
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
    ac_texts,
)
from ouroboros.events.base import BaseEvent
from ouroboros.resilience.lateral import ThinkingPersona

USER_CONSTRAINT = "Use existing project patterns"
USER_NON_GOAL = "Non-goal: No cloud sync"
GOAL = "Build a word_count CLI"
CRITERION = "`word_count` prints the number of words in its input"
INSTRUCTIONS = (
    AMBIGUITY_INSTRUCTION,
    LEDGER_PRESERVATION_INSTRUCTION,
    DIAGNOSTIC_PROSE_INSTRUCTION,
    DIRECT_CRITERIA_INSTRUCTION,
    LEGACY_LATERAL_PLACEHOLDER,
)
NON_GOALS_MISSING = "non_goals are missing from executable constraints"
NO_OP_MISSING = "missing explicit no-op scope"
NO_OP_CONSTRAINT = "Define explicit no-op scope for supported command behavior."
DECISION = (
    "Use this bounded implementation approach to resolve Seed QA: "
    "Decision: Count words by splitting on whitespace."
)


def _seed(*constraints: str) -> Seed:
    return Seed(
        goal=GOAL,
        constraints=constraints or (USER_CONSTRAINT,),
        acceptance_criteria=(CRITERION,),
        ontology_schema=OntologySchema(
            name="WordCount",
            description="word_count task ontology",
            fields=(OntologyField(name="text", field_type="string", description="Input text"),),
        ),
        evaluation_principles=(
            EvaluationPrinciple(name="testability", description="Observable behavior", weight=1.0),
        ),
        exit_conditions=(
            ExitCondition(
                name="verified",
                description="Checks pass",
                evaluation_criteria="All acceptance criteria pass",
            ),
        ),
        metadata=SeedMetadata(seed_id="seed_word_count", ambiguity_score=0.12),
    )


def _ledger(*, non_goal_binds: bool) -> SeedDraftLedger:
    """A seed-ready ledger whose non-goal binds only when the user confirmed it."""
    ledger = SeedDraftLedger.from_goal(GOAL)
    for section, value in {
        "actors": "Single local CLI user",
        "inputs": "Text argument",
        "outputs": "Word count on stdout",
        "constraints": USER_CONSTRAINT,
        "non_goals": "No cloud sync",
        "acceptance_criteria": CRITERION,
        "verification_plan": "Run pytest",
        "failure_modes": "Invalid input exits non-zero",
        "runtime_context": "Existing repository runtime",
    }.items():
        confirmed = section == "non_goals" and non_goal_binds
        ledger.add_entry(
            section,
            LedgerEntry(
                key=f"{section}.test",
                value=value,
                source=(
                    LedgerSource.NON_GOAL
                    if section == "non_goals"
                    else LedgerSource.CONSERVATIVE_DEFAULT
                ),
                confidence=0.9,
                status=LedgerStatus.CONFIRMED if confirmed else LedgerStatus.DEFAULTED,
            ),
        )
    return ledger


class _RecordingEventStore:
    def __init__(self) -> None:
        self.appended: list[BaseEvent] = []

    async def append(self, event: BaseEvent, **_: Any) -> None:
        self.appended.append(event)


def _pipeline(
    tmp_path,
    *,
    seed_qa,
    run_seed,
    lateral_thinker=None,
    event_store: _RecordingEventStore | None = None,
    generate_seed=None,
) -> AutoPipeline:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn(
            "done", "interview_3033", seed_ready=True, completed=True, ambiguity_score=0.12
        )

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn(
            "done", session_id, seed_ready=True, completed=True, ambiguity_score=0.12
        )

    async def default_generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed()

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
        event_store=event_store,
    )
    return AutoPipeline(
        driver,
        generate_seed or default_generate_seed,
        run_starter=run_seed,
        store=AutoStore(tmp_path),
        seed_qa_evaluator=seed_qa,
        lateral_thinker=lateral_thinker,
    )


def _state(tmp_path, ledger: SeedDraftLedger) -> AutoPipelineState:
    state = AutoPipelineState(goal=GOAL, cwd=str(tmp_path))
    state.max_repair_rounds = 2
    state.ledger = ledger.to_dict()
    return state


def _assert_user_contract_only(seed: Seed) -> None:
    """The Seed keeps the user's constraints and non-goal and no repair instruction."""
    surfaces = "\n".join((seed.goal, *seed.constraints, *ac_texts(seed.acceptance_criteria)))
    for instruction in INSTRUCTIONS:
        assert instruction not in surfaces
    assert "executable Seed surfaces" not in surfaces
    assert "Resolve Seed QA feedback" not in surfaces
    assert USER_CONSTRAINT in seed.constraints
    assert USER_NON_GOAL in seed.constraints
    assert seed.goal == GOAL
    assert ac_texts(seed.acceptance_criteria) == (CRITERION,)


def _non_goal_judge(calls: list[Seed]):
    """Fail until the user's non-goal is in the Seed's constraints, then pass."""

    async def seed_qa(seed: Seed, ledger: SeedDraftLedger) -> EvaluateResult:  # noqa: ARG001
        calls.append(seed)
        if USER_NON_GOAL in seed.constraints:
            return EvaluateResult(passed=True, score=0.9, verdict="pass")
        return EvaluateResult(
            passed=False, score=0.5, verdict="revise", differences=(NON_GOALS_MISSING,)
        )

    return seed_qa


@pytest.mark.asyncio
async def test_ledger_preservation_round_restores_non_goal_without_instruction(tmp_path) -> None:
    judged: list[Seed] = []
    ran: list[Seed] = []

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str]:  # noqa: ARG001
        ran.append(seed)
        return {"job_id": "job_3033_ledger"}

    state = _state(tmp_path, _ledger(non_goal_binds=True))
    pipeline = _pipeline(tmp_path, seed_qa=_non_goal_judge(judged), run_seed=run_seed)

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert len(judged) == 2
    assert USER_NON_GOAL not in judged[0].constraints  # the repair round restored it
    _assert_user_contract_only(judged[1])
    _assert_user_contract_only(ran[0])
    _assert_user_contract_only(Seed.from_dict(state.seed_artifact))


@pytest.mark.parametrize(
    ("summary", "text"),
    (
        pytest.param(
            "{persona}: Challenges assumptions",
            "## Persona: {persona}\nEVALUATE failed. Current Approach (Not Working):\n"
            "Most recent run artifact: goal: bad yaml",
            id="transcript-only",
        ),
        pytest.param(
            LEDGER_PRESERVATION_INSTRUCTION,
            f"Decision: {DIRECT_CRITERIA_INSTRUCTION}",
            id="echoed-instructions",
        ),
    ),
)
@pytest.mark.asyncio
async def test_lateral_fallback_round_writes_no_placeholder(
    tmp_path, summary: str, text: str
) -> None:
    judged: list[Seed] = []
    ran: list[Seed] = []
    briefs: list[tuple[str, ...]] = []

    async def lateral(*, persona, qa_differences, qa_suggestions, run_artifact) -> LateralResult:  # noqa: ARG001
        briefs.append(tuple(qa_differences))
        name = persona.value.title()
        return LateralResult(
            persona=persona.value,
            approach_summary=summary.format(persona=name),
            text=text.format(persona=name),
        )

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str]:  # noqa: ARG001
        ran.append(seed)
        return {"job_id": "job_3033_lateral"}

    state = _state(tmp_path, _ledger(non_goal_binds=True))
    pipeline = _pipeline(
        tmp_path, seed_qa=_non_goal_judge(judged), run_seed=run_seed, lateral_thinker=lateral
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    # The instruction still briefs the persona: guidance steers, it is not persisted.
    assert briefs == [(LEDGER_PRESERVATION_INSTRUCTION,)]
    assert state.personas_invoked  # the persona counts as tried
    assert len(judged) == 2
    _assert_user_contract_only(judged[1])
    _assert_user_contract_only(ran[0])
    _assert_user_contract_only(Seed.from_dict(state.seed_artifact))


@pytest.mark.asyncio
async def test_instruction_only_feedback_with_nothing_to_restore_is_advisory(tmp_path) -> None:
    """No product constraint, and the only non-goal is a defaulted guess.

    The guess must not be promoted to a constraint, and the unchanged Seed is
    handed to run → evaluate instead of being re-judged as a "repair".
    """
    judged: list[Seed] = []
    ran: list[Seed] = []
    events = _RecordingEventStore()

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str]:  # noqa: ARG001
        ran.append(seed)
        return {"job_id": "job_3033_advisory"}

    state = _state(tmp_path, _ledger(non_goal_binds=False))
    pipeline = _pipeline(
        tmp_path, seed_qa=_non_goal_judge(judged), run_seed=run_seed, event_store=events
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert len(judged) == 1
    assert ran[0].constraints == (USER_CONSTRAINT,)
    advisory = [e for e in events.appended if e.type == "auto.seed_qa.advisory_override"]
    assert [event.data["reason"] for event in advisory] == ["seed_qa_feedback_unmapped"]
    assert state.last_qa_passed is False


def test_repair_drops_instruction_sentences_persisted_by_earlier_releases() -> None:
    seed = _seed(USER_CONSTRAINT, LEDGER_PRESERVATION_INSTRUCTION, LEGACY_LATERAL_PLACEHOLDER)
    qa_result = EvaluateResult(
        passed=False, score=0.5, verdict="revise", differences=("missing explicit no-op scope",)
    )

    repaired = _seed_with_repair_plan(seed, seed_qa_repair_plan(qa_result), ledger=None)

    assert repaired.constraints == (
        USER_CONSTRAINT,
        "Define explicit no-op scope for supported command behavior.",
    )


def _judge_until(differences: tuple[str, ...], required: tuple[str, ...], calls: list[Seed]):
    """Fail with ``differences`` until every ``required`` constraint is present."""

    async def seed_qa(seed: Seed, ledger: SeedDraftLedger) -> EvaluateResult:  # noqa: ARG001
        calls.append(seed)
        if all(item in seed.constraints for item in required):
            return EvaluateResult(passed=True, score=0.9, verdict="pass")
        return EvaluateResult(passed=False, score=0.5, verdict="revise", differences=differences)

    return seed_qa


@pytest.mark.parametrize(
    ("differences", "brief", "expected"),
    (
        pytest.param(
            (NO_OP_MISSING,),
            (NO_OP_CONSTRAINT,),
            (USER_CONSTRAINT, NO_OP_CONSTRAINT, DECISION),
            id="product-constraint",
        ),
        pytest.param(
            (NON_GOALS_MISSING,),
            (LEDGER_PRESERVATION_INSTRUCTION,),
            (USER_CONSTRAINT, USER_NON_GOAL, DECISION),
            id="missing-non-goals",
        ),
        pytest.param(
            (NO_OP_MISSING, NON_GOALS_MISSING),
            (LEDGER_PRESERVATION_INSTRUCTION, NO_OP_CONSTRAINT),
            (USER_CONSTRAINT, NO_OP_CONSTRAINT, USER_NON_GOAL, DECISION),
            id="both",
        ),
    ),
)
@pytest.mark.asyncio
async def test_lateral_decision_lands_on_top_of_the_mapped_plan(
    tmp_path,
    differences: tuple[str, ...],
    brief: tuple[str, ...],
    expected: tuple[str, ...],
) -> None:
    """A concrete persona decision adds to the deterministic repair, never replaces it."""
    judged: list[Seed] = []
    ran: list[Seed] = []
    briefs: list[tuple[str, ...]] = []

    async def lateral(*, persona, qa_differences, qa_suggestions, run_artifact) -> LateralResult:  # noqa: ARG001
        briefs.append(tuple(qa_differences))
        return LateralResult(
            persona=persona.value,
            approach_summary="",
            text="Decision: Count words by splitting on whitespace.",
        )

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str]:  # noqa: ARG001
        ran.append(seed)
        return {"job_id": "job_3033_lateral_success"}

    state = _state(tmp_path, _ledger(non_goal_binds=True))
    pipeline = _pipeline(
        tmp_path,
        seed_qa=_judge_until(differences, expected, judged),
        run_seed=run_seed,
        lateral_thinker=lateral,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert briefs == [brief]
    assert len(judged) == 2
    for seed in (judged[1], ran[0], Seed.from_dict(state.seed_artifact)):
        assert seed.constraints == expected  # stable order, no instruction sentence
        assert seed.goal == GOAL
        assert ac_texts(seed.acceptance_criteria) == (CRITERION,)


@pytest.mark.asyncio
async def test_exhausted_persona_chain_applies_the_mapped_plan(tmp_path) -> None:
    async def lateral(**kwargs: Any) -> LateralResult:
        raise AssertionError(f"every persona was already tried: {kwargs}")

    pipeline = _pipeline(
        tmp_path, seed_qa=_non_goal_judge([]), run_seed=None, lateral_thinker=lateral
    )
    state = _state(tmp_path, _ledger(non_goal_binds=True))
    state.personas_invoked = [persona.value for persona in ThinkingPersona]
    qa_result = EvaluateResult(
        passed=False,
        score=0.5,
        verdict="revise",
        differences=(NO_OP_MISSING, NON_GOALS_MISSING),
    )

    repaired = await pipeline._repair_seed_after_qa(
        state, _seed(), qa_result, attempt=1, ledger=_ledger(non_goal_binds=True)
    )

    assert repaired.constraints == (USER_CONSTRAINT, NO_OP_CONSTRAINT, USER_NON_GOAL)


@pytest.mark.parametrize(
    ("text", "claims_lateral", "expected"),
    (
        pytest.param(
            "Decision: Count words by splitting on whitespace.",
            True,
            (USER_CONSTRAINT, NO_OP_CONSTRAINT, DECISION),
            id="decision",
        ),
        pytest.param(
            "## Persona: Hacker\nMost recent run artifact: goal: bad yaml",
            False,
            (USER_CONSTRAINT, NO_OP_CONSTRAINT),
            id="transcript-only",
        ),
    ),
)
@pytest.mark.asyncio
async def test_lateral_repair_is_claimed_only_when_a_decision_lands(
    tmp_path, text: str, claims_lateral: bool, expected: tuple[str, ...]
) -> None:
    async def lateral(*, persona, qa_differences, qa_suggestions, run_artifact) -> LateralResult:  # noqa: ARG001
        return LateralResult(persona=persona.value, approach_summary="", text=text)

    pipeline = _pipeline(
        tmp_path, seed_qa=_non_goal_judge([]), run_seed=None, lateral_thinker=lateral
    )
    state = _state(tmp_path, _ledger(non_goal_binds=True))
    state.last_progress_message = None
    qa_result = EvaluateResult(
        passed=False, score=0.5, verdict="revise", differences=(NO_OP_MISSING,)
    )

    repaired = await pipeline._repair_seed_after_qa(
        state, _seed(), qa_result, attempt=1, ledger=_ledger(non_goal_binds=True)
    )

    assert repaired.constraints == expected
    assert state.personas_invoked  # tried either way, so the next round moves on
    claimed = (state.last_progress_message or "").startswith("Seed QA lateral repair via")
    assert claimed is claims_lateral


@pytest.mark.asyncio
async def test_resumed_review_repairs_from_the_persisted_ledger(tmp_path) -> None:
    """Resume re-enters the gate with the ledger rebuilt from ``state.ledger``."""
    judged: list[Seed] = []
    ran: list[Seed] = []

    async def forbidden_generate_seed(session_id: str) -> Seed:
        raise AssertionError(f"resume must reuse the persisted Seed, not regenerate {session_id}")

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str]:  # noqa: ARG001
        ran.append(seed)
        return {"job_id": "job_3033_resume"}

    state = _state(tmp_path, _ledger(non_goal_binds=True))
    state.interview_completed = True
    state.interview_session_id = "interview_3033"
    state.phase = AutoPhase.REVIEW
    state.seed_artifact = _seed().to_dict()
    pipeline = _pipeline(
        tmp_path,
        seed_qa=_non_goal_judge(judged),
        run_seed=run_seed,
        generate_seed=forbidden_generate_seed,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert len(judged) == 2
    _assert_user_contract_only(ran[0])


@pytest.mark.parametrize(
    ("difference", "line", "is_constraint"),
    (
        (
            "metadata.ambiguity_score must be at most 0.20",
            AMBIGUITY_INSTRUCTION,
            False,
        ),
        (NON_GOALS_MISSING, LEDGER_PRESERVATION_INSTRUCTION, False),
        ("runtime_context is not preserved", LEDGER_PRESERVATION_INSTRUCTION, False),
        ("constraints are polluted with diagnostic text", DIAGNOSTIC_PROSE_INSTRUCTION, False),
        ("acceptance criteria are templated", DIRECT_CRITERIA_INSTRUCTION, False),
        (
            "transcript schema differs between criteria",
            "Use one transcript JSON schema consistently across acceptance criteria.",
            True,
        ),
        (
            "missing explicit no-op scope",
            "Define explicit no-op scope for supported command behavior.",
            True,
        ),
        (
            "omits the review-blocking post-QA constraint",
            "Introduce the review-blocking post-QA constraint before execution.",
            True,
        ),
        (
            "no binding contract chosen",
            "Define one explicit binding contract before execution.",
            True,
        ),
        (
            "failure leaves partial output files",
            "Failure paths must leave no partial output artifacts.",
            True,
        ),
    ),
)
def test_plan_routes_each_mapped_line(difference: str, line: str, is_constraint: bool) -> None:
    plan = seed_qa_repair_plan(
        EvaluateResult(passed=False, score=0.5, verdict="revise", differences=(difference,))
    )

    assert plan.guidance == (line,)
    assert plan.constraints == ((line,) if is_constraint else ())
    assert plan.restore_ledger_non_goals is (difference == NON_GOALS_MISSING)
