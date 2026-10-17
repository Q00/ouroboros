"""Guessed ``ooo auto`` answers never bind a Seed; they travel as assumptions.

The driver declares each answer's provenance with the answer itself. A
conservative default, an assumption, and a draft kept because the requester gave
no answer are declared ``"assumption"``: the interview withholds them from Seed
extraction, the ledger route keeps them out of ``constraints`` and
``acceptance_criteria``, and the worker sees them as non-binding assumptions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ouroboros.auto.adapters import HandlerInterviewBackend
from ouroboros.auto.answer_binding import answer_provenance, with_provenance
from ouroboros.auto.answerer import AutoAnswer, AutoAnswerSource
from ouroboros.auto.interview_driver import AutoInterviewDriver, InterviewTurn
from ouroboros.auto.ledger import (
    REQUIRED_SECTIONS,
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)
from ouroboros.auto.ledger_seed import synthesize_seed_from_ledger, with_ledger_assumptions
from ouroboros.auto.requester import RequesterQuestion, RequesterReply
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.bigbang.answer_provenance import (
    ANSWER_PROVENANCE_ARGUMENT,
    WITHHELD_ASSUMPTION_NOTE,
    declare_provenance,
    extraction_rounds,
)
from ouroboros.bigbang.interview import InterviewState
from ouroboros.bigbang.seed_generator import SeedGenerator
from ouroboros.core.seed_contract import SeedContract
from ouroboros.core.seed_contract_prompt import render_seed_contract_for_execution

GOAL = "Make collectstatic keep working when max_post_process_passes is 0"
FIRST_QUESTION = "Which output format should the CLI use?"
DEFAULTED_CONSTRAINT = (
    "With max_post_process_passes = 0, collectstatic must still write staticfiles.json "
    'with paths["site.css"] equal to "site.css"'
)
UNSPECIFIED_DRAFT_CRITERION = "Only extend='both' is in scope; min and max keep existing behavior"
REQUESTER_CONSTRAINT = "Do not raise UnboundLocalError when max_post_process_passes is 0"


class _RecordingBackend:
    """Interview backend that takes the declared provenance, like the real one."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def start(self, goal: str, *, cwd: str, interview_id: str | None = None) -> InterviewTurn:  # noqa: ARG002
        return InterviewTurn(FIRST_QUESTION, "s")

    async def answer(
        self,
        session_id: str,
        answer: str,
        *,
        last_question: str | None = None,  # noqa: ARG002
        provenance: str | None = None,
    ) -> InterviewTurn:
        self.calls.append((answer, provenance))
        return InterviewTurn("What else should we consider?", session_id)

    async def resume(self, session_id: str) -> InterviewTurn:
        return InterviewTurn(FIRST_QUESTION, session_id)


class _StubRequester:
    def __init__(self, reply: RequesterReply) -> None:
        self.reply = reply

    async def ask(self, question: RequesterQuestion) -> RequesterReply:  # noqa: ARG002
        return self.reply


async def _first_answer(tmp_path: Path, requester: Any = None) -> tuple[str, str | None]:
    backend = _RecordingBackend()
    driver = AutoInterviewDriver(
        backend,
        store=AutoStore(tmp_path / "store"),
        max_rounds=1,
        requester=requester,
    )
    state = AutoPipelineState(goal=GOAL, cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    await driver.run(state, SeedDraftLedger.from_goal(GOAL))
    assert backend.calls, "the driver must answer the first question"
    return backend.calls[0]


# -- the driver declares provenance with each answer ---------------------------


@pytest.mark.asyncio
async def test_self_answered_draft_is_declared_an_assumption(tmp_path: Path) -> None:
    text, provenance = await _first_answer(tmp_path)

    assert text.startswith("[from-auto]")
    assert provenance == "assumption"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        RequesterReply("empty_answer", detail="empty stdout"),
        RequesterReply("timeout"),
        RequesterReply("nonzero_exit", detail="exit code 3"),
    ],
)
async def test_draft_kept_after_unanswered_requester_is_an_assumption(
    tmp_path: Path, reply: RequesterReply
) -> None:
    _text, provenance = await _first_answer(tmp_path, _StubRequester(reply))

    assert provenance == "assumption"


@pytest.mark.asyncio
async def test_requester_answer_is_declared_binding(tmp_path: Path) -> None:
    requester = _StubRequester(RequesterReply("answered", answer=REQUESTER_CONSTRAINT))

    text, provenance = await _first_answer(tmp_path, requester)

    assert REQUESTER_CONSTRAINT in text
    assert provenance == "user"


def test_answer_provenance_follows_source_and_ledger_status() -> None:
    defaulted = LedgerEntry(
        key="non_goals.mvp_scope",
        value="auth is a non-goal",
        source=LedgerSource.NON_GOAL,
        confidence=0.86,
        status=LedgerStatus.DEFAULTED,
    )
    repo_fact = LedgerEntry(
        key="runtime.repo_fact",
        value="Python 3.12, pytest",
        source=LedgerSource.REPO_FACT,
        confidence=0.9,
        status=LedgerStatus.CONFIRMED,
    )

    # A templated "grounded" label on a DEFAULTED entry is still a guess.
    assert (
        answer_provenance(
            AutoAnswer("x", AutoAnswerSource.NON_GOAL, 0.86, [("non_goals", defaulted)])
        )
        == "assumption"
    )
    assert answer_provenance(AutoAnswer("x", AutoAnswerSource.CONSERVATIVE_DEFAULT, 0.8)) == (
        "assumption"
    )
    assert (
        answer_provenance(
            AutoAnswer("x", AutoAnswerSource.REPO_FACT, 0.9, [("runtime_context", repo_fact)])
        )
        == "user"
    )
    assert answer_provenance(AutoAnswer("x", AutoAnswerSource.REQUESTER, 0.83)) == "user"


@pytest.mark.asyncio
async def test_backend_without_provenance_keyword_gets_the_original_call() -> None:
    received: list[tuple[str, str, str | None]] = []

    class _Legacy:
        async def answer(
            self, session_id: str, answer: str, *, last_question: str | None = None
        ) -> InterviewTurn:
            received.append((session_id, answer, last_question))
            return InterviewTurn("next", session_id)

    await with_provenance(_Legacy(), "assumption").answer("s", "a", last_question="q")  # type: ignore[arg-type]

    assert received == [("s", "a", "q")]


@pytest.mark.asyncio
async def test_handler_backend_forwards_declared_provenance_argument() -> None:
    seen: list[dict[str, Any]] = []

    class _Handler:
        async def handle(self, arguments: dict[str, Any]) -> Any:
            seen.append(dict(arguments))
            raise RuntimeError("stop after capture")

    backend = HandlerInterviewBackend(_Handler(), cwd=".")  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        await backend.answer("s", "[from-auto][assumption] x", provenance="assumption")
    with pytest.raises(RuntimeError):
        await backend.answer("s", "[from-auto][requester] y")

    assert seen[0][ANSWER_PROVENANCE_ARGUMENT] == "assumption"
    assert ANSWER_PROVENANCE_ARGUMENT not in seen[1]


# -- the interview withholds a declared assumption from Seed extraction ---------


def _state_with_assumption_round() -> InterviewState:
    state = InterviewState(interview_id="interview_0000000000000001", initial_context=GOAL)
    declare_provenance(
        state.record_answer("Must staticfiles.json keep identity paths?", DEFAULTED_CONSTRAINT),
        {ANSWER_PROVENANCE_ARGUMENT: "assumption"},
    )
    state.record_answer("Which error must go away?", REQUESTER_CONSTRAINT)
    return state


def test_declared_assumption_survives_a_persistence_round_trip() -> None:
    state = _state_with_assumption_round()

    reloaded = InterviewState.model_validate(state.model_dump(mode="json"))

    assert [round_data.provenance for round_data in reloaded.rounds] == ["assumption", "user"]


def test_declaration_only_lowers_authority() -> None:
    state = InterviewState(interview_id="interview_0000000000000002", initial_context=GOAL)
    round_data = state.record_answer("q", "an answer")

    declare_provenance(round_data, {ANSWER_PROVENANCE_ARGUMENT: "user"})
    declare_provenance(round_data, {ANSWER_PROVENANCE_ARGUMENT: "observation"})
    declare_provenance(round_data, {})
    assert round_data.provenance == "user"

    declare_provenance(None, {ANSWER_PROVENANCE_ARGUMENT: "assumption"})


def test_seed_extraction_context_withholds_the_assumption_text() -> None:
    state = _state_with_assumption_round()

    projected = extraction_rounds(state)
    context = SeedGenerator(llm_adapter=None)._build_interview_context(state)  # type: ignore[arg-type]

    assert projected[0].withheld is True
    assert projected[0].answer == WITHHELD_ASSUMPTION_NOTE
    assert DEFAULTED_CONSTRAINT not in context
    assert WITHHELD_ASSUMPTION_NOTE in context
    assert REQUESTER_CONSTRAINT in context


# -- the ledger route binds only confirmed content ------------------------------


def _ready_ledger() -> SeedDraftLedger:
    ledger = SeedDraftLedger.from_goal(GOAL)
    for section in REQUIRED_SECTIONS:
        if section == "goal":
            continue
        ledger.add_entry(
            section,
            LedgerEntry(
                key=f"{section}.safe_default_finalization",
                value=f"generic safe default for {section}",
                source=LedgerSource.ASSUMPTION,
                confidence=0.68,
                status=LedgerStatus.DEFAULTED,
            ),
        )
    ledger.add_entry(
        "constraints",
        LedgerEntry(
            key="constraints.staticfiles_mapping",
            value=DEFAULTED_CONSTRAINT,
            source=LedgerSource.CONSERVATIVE_DEFAULT,
            confidence=0.8,
            status=LedgerStatus.DEFAULTED,
        ),
    )
    # A draft kept because the requester answered "unspecified" (no answer).
    ledger.add_entry(
        "acceptance_criteria",
        LedgerEntry(
            key="acceptance_criteria.scope",
            value=UNSPECIFIED_DRAFT_CRITERION,
            source=LedgerSource.ASSUMPTION,
            confidence=0.7,
            status=LedgerStatus.DEFAULTED,
        ),
    )
    ledger.add_entry(
        "constraints",
        LedgerEntry(
            key="constraints.error.requester",
            value=REQUESTER_CONSTRAINT,
            source=LedgerSource.USER_PREFERENCE,
            confidence=0.83,
            status=LedgerStatus.CONFIRMED,
        ),
    )
    assert ledger.is_seed_ready()
    return ledger


def test_ledger_seed_keeps_defaulted_and_unspecified_drafts_out_of_binding_fields() -> None:
    seed = synthesize_seed_from_ledger(_ready_ledger())

    binding_text = " ".join(
        (*seed.constraints, *(criterion.description for criterion in seed.acceptance_criteria))
    )
    assert DEFAULTED_CONSTRAINT not in binding_text
    assert UNSPECIFIED_DRAFT_CRITERION not in binding_text
    assert "generic safe default" not in binding_text
    assert REQUESTER_CONSTRAINT in seed.constraints

    assert DEFAULTED_CONSTRAINT in seed.assumptions
    assert UNSPECIFIED_DRAFT_CRITERION in seed.assumptions
    assert all("generic safe default" not in item for item in seed.assumptions)
    # No confirmed criterion: the criterion is proof of the goal, not a guess.
    assert len(seed.acceptance_criteria) == 1


def test_generated_seed_carries_ledger_drafts_as_assumptions_only() -> None:
    ledger = _ready_ledger()
    seed = synthesize_seed_from_ledger(ledger).model_copy(update={"assumptions": ()})

    carried = with_ledger_assumptions(seed, ledger)

    assert DEFAULTED_CONSTRAINT in carried.assumptions
    assert UNSPECIFIED_DRAFT_CRITERION in carried.assumptions
    assert carried.constraints == seed.constraints
    assert carried.acceptance_criteria == seed.acceptance_criteria
    assert with_ledger_assumptions(carried, ledger) is carried


def test_worker_prompt_labels_assumptions_as_non_binding() -> None:
    seed = synthesize_seed_from_ledger(_ready_ledger())

    prompt = render_seed_contract_for_execution(SeedContract.from_seed(seed))

    constraints_block, assumptions_block = prompt.split("## Assumptions (non-binding)", 1)
    assert DEFAULTED_CONSTRAINT not in constraints_block
    assert DEFAULTED_CONSTRAINT in assumptions_block
    assert "override" in assumptions_block.split("\n- ", 1)[0]


def test_seed_without_assumptions_serializes_as_before() -> None:
    seed = synthesize_seed_from_ledger(_ready_ledger())

    assert "assumptions" in seed.to_dict()
    assert "assumptions" not in seed.model_copy(update={"assumptions": ()}).to_dict()


# -- end to end: the pipeline persists assumptions; withheld rounds never block --


def test_pipeline_persists_ledger_drafts_as_assumptions_on_the_generated_seed(
    tmp_path: Path,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from ouroboros.auto.pipeline import AutoPipeline
    from ouroboros.core.seed import Seed

    ledger = _ready_ledger()
    generated = synthesize_seed_from_ledger(ledger).model_copy(update={"assumptions": ()})
    pipeline = AutoPipeline(MagicMock(), AsyncMock(), store=AutoStore(tmp_path / "store"))
    state = AutoPipelineState(goal=GOAL, cwd=str(tmp_path))

    pipeline._record_generated_seed(state, ledger, generated)
    persisted = Seed.from_dict(state.seed_artifact)

    assert DEFAULTED_CONSTRAINT in persisted.assumptions
    assert UNSPECIFIED_DRAFT_CRITERION in persisted.assumptions
    assert DEFAULTED_CONSTRAINT not in persisted.constraints
    prompt = render_seed_contract_for_execution(SeedContract.from_seed(persisted))
    assert DEFAULTED_CONSTRAINT in prompt.split("## Assumptions (non-binding)", 1)[1]


def test_interview_with_only_assumption_answers_is_ready_for_seed_generation() -> None:
    from ouroboros.bigbang.requirement_distillation import (
        build_requirement_distillation,
        is_reference_aware_distillation,
        seed_readiness_details,
    )
    from ouroboros.core.requirement_candidate import evaluate_promotion

    state = InterviewState(interview_id="interview_0000000000000003", initial_context=GOAL)
    for question, answer in (
        ("Must staticfiles.json keep identity paths?", f"It must hold: {DEFAULTED_CONSTRAINT}"),
        ("Which extend values are in scope?", f"Required: {UNSPECIFIED_DRAFT_CRITERION}"),
    ):
        declare_provenance(
            state.record_answer(question, answer), {ANSWER_PROVENANCE_ARGUMENT: "assumption"}
        )

    distillation = build_requirement_distillation(state)
    promotion = evaluate_promotion(distillation)
    details = seed_readiness_details(
        promotion,
        require_promoted_acceptance_criteria=is_reference_aware_distillation(distillation),
    )
    context = SeedGenerator(llm_adapter=None)._build_interview_context(state)  # type: ignore[arg-type]

    assert details["blockers"] == []
    # A withheld "must" answer is not promoted, so the verbatim-anchor backstop
    # cannot re-append it to the Seed either.
    assert all(DEFAULTED_CONSTRAINT not in candidate.text for candidate in promotion.promoted)
    assert GOAL in context
    assert DEFAULTED_CONSTRAINT not in context
