"""Requester answers reach the worker verbatim, as quoted questions and answers.

``ooo auto`` relays some interview questions to the requester. An answer is a
confirmed fact, but rewriting it into a constraint or criterion changed what it
said (matplotlib-22865: "My issue does not mention extend='min' or 'max'"
became a binding criterion and the worker wrote ``if self.extend != 'both'``).
The answer is declared ``"requester"``, so Seed extraction withholds it, the
ledger route keeps it out of the binding fields, and the Seed carries it as
``requester_answers``: the question and the answer, word for word. A question
the requester did not answer reaches the worker as nothing at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ouroboros.auto.interview_driver import AutoInterviewDriver, InterviewTurn
from ouroboros.auto.ledger import (
    REQUESTER_KEY_SUFFIX,
    REQUIRED_SECTIONS,
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)
from ouroboros.auto.ledger_seed import synthesize_seed_from_ledger
from ouroboros.auto.requester import RequesterQuestion, RequesterReply
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.bigbang.answer_provenance import (
    ANSWER_PROVENANCE_ARGUMENT,
    declare_provenance,
    extraction_rounds,
)
from ouroboros.bigbang.interview import InterviewState
from ouroboros.bigbang.seed_generator import SeedGenerator
from ouroboros.core.seed import Seed
from ouroboros.core.seed_contract import SeedContract
from ouroboros.core.seed_contract_prompt import render_seed_contract_for_execution

GOAL = "Colorbar with drawedges=True and extend='both' does not draw edges at extremities"
QUESTION = "Should the fix cover only extend='both', or also extend='min' and 'max'?"
ANSWER = "My issue does not mention extend='min' or extend='max'."
MULTILINE_ANSWER = "Here is the reproduction:\n\n    import foobar\n    assert False\n"
DRAFT = "Only extend='both' is in scope; min and max keep existing behavior"


class _Backend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def start(self, goal: str, *, cwd: str, interview_id: str | None = None) -> InterviewTurn:  # noqa: ARG002
        return InterviewTurn(QUESTION, "s")

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
        return InterviewTurn(QUESTION, session_id)


class _Requester:
    def __init__(self, reply: RequesterReply) -> None:
        self.reply = reply

    async def ask(self, question: RequesterQuestion) -> RequesterReply:  # noqa: ARG002
        return self.reply


async def _driver_ledger(tmp_path: Path, reply: RequesterReply) -> SeedDraftLedger:
    driver = AutoInterviewDriver(
        _Backend(),
        store=AutoStore(tmp_path / "store"),
        max_rounds=1,
        requester=_Requester(reply),
    )
    state = AutoPipelineState(goal=GOAL, cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    result = await driver.run(state, SeedDraftLedger.from_goal(GOAL))
    return result.ledger


def _ready_ledger(*extra: tuple[str, LedgerEntry]) -> SeedDraftLedger:
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
    for section, entry in extra:
        ledger.add_entry(section, entry)
    assert ledger.is_seed_ready()
    return ledger


def _requester_entry(section: str, value: str, question: str = QUESTION) -> LedgerEntry:
    return LedgerEntry(
        key=f"{section}.extend_scope{REQUESTER_KEY_SUFFIX}",
        value=value,
        source=LedgerSource.USER_PREFERENCE,
        confidence=0.83,
        status=LedgerStatus.CONFIRMED,
        question=question,
    )


# -- extraction never sees the requester's text ---------------------------------


def _state_with_requester_round() -> InterviewState:
    state = InterviewState(interview_id="interview_0000000000000009", initial_context=GOAL)
    round_data = state.record_answer(QUESTION, f"[from-auto][requester] {ANSWER}")
    declare_provenance(round_data, {ANSWER_PROVENANCE_ARGUMENT: "requester"})
    return state


def test_seed_extraction_withholds_the_requester_answer() -> None:
    from ouroboros.bigbang.answer_provenance import WITHHELD_REQUESTER_NOTE

    state = _state_with_requester_round()

    projected = extraction_rounds(state)
    context = SeedGenerator(llm_adapter=None)._build_interview_context(state)  # type: ignore[arg-type]

    assert projected[0].withheld is True
    assert projected[0].answer == WITHHELD_REQUESTER_NOTE
    assert ANSWER not in context
    assert WITHHELD_REQUESTER_NOTE in context


def test_declared_requester_provenance_survives_a_persistence_round_trip() -> None:
    state = _state_with_requester_round()

    reloaded = InterviewState.model_validate(state.model_dump(mode="json"))

    assert [round_data.provenance for round_data in reloaded.rounds] == ["requester"]


# -- the ledger keeps the pair verbatim and out of the binding fields -----------


@pytest.mark.asyncio
async def test_requester_answer_keeps_its_question_in_the_ledger(tmp_path: Path) -> None:
    from ouroboros.auto.ledger_seed import ledger_requester_answers

    ledger = await _driver_ledger(tmp_path, RequesterReply("answered", answer=ANSWER))

    entries = [
        entry
        for section in ledger.sections.values()
        for entry in section.entries
        if entry.key.endswith(REQUESTER_KEY_SUFFIX)
    ]
    assert entries, "the requester's answer must reach the ledger"
    assert all(entry.question == QUESTION and entry.value == ANSWER for entry in entries)
    assert [(item.question, item.answer) for item in ledger_requester_answers(ledger)] == [
        (QUESTION, ANSWER)
    ]


def test_ledger_seed_quotes_requester_answers_instead_of_binding_them() -> None:
    ledger = _ready_ledger(
        ("constraints", _requester_entry("constraints", ANSWER)),
        ("acceptance_criteria", _requester_entry("acceptance_criteria", ANSWER)),
        ("outputs", _requester_entry("outputs", ANSWER)),
    )

    seed = synthesize_seed_from_ledger(ledger)

    binding_text = " ".join(
        (
            *seed.constraints,
            *(criterion.description for criterion in seed.acceptance_criteria),
            *(field.description for field in seed.ontology_schema.fields),
        )
    )
    assert ANSWER not in binding_text
    assert all(ANSWER not in item for item in seed.assumptions)
    # One answer filled three sections; the Seed quotes it once.
    assert [(item.question, item.answer) for item in seed.requester_answers] == [(QUESTION, ANSWER)]


def test_generated_seed_carries_requester_answers_verbatim_to_the_worker(
    tmp_path: Path,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from ouroboros.auto.pipeline import AutoPipeline

    ledger = _ready_ledger(
        ("constraints", _requester_entry("constraints", ANSWER)),
        ("verification_plan", _requester_entry("verification_plan", MULTILINE_ANSWER, "How?")),
    )
    # The authoring backend's Seed never sees the requester's text.
    generated = synthesize_seed_from_ledger(ledger).model_copy(update={"requester_answers": ()})
    pipeline = AutoPipeline(MagicMock(), AsyncMock(), store=AutoStore(tmp_path / "store"))
    state = AutoPipelineState(goal=GOAL, cwd=str(tmp_path))

    pipeline._record_generated_seed(state, ledger, generated)
    persisted = Seed.from_dict(state.seed_artifact)

    assert [(item.question, item.answer) for item in persisted.requester_answers] == [
        (QUESTION, ANSWER),
        ("How?", MULTILINE_ANSWER.strip()),
    ]
    prompt = render_seed_contract_for_execution(SeedContract.from_seed(persisted))
    before, block = prompt.split("## Requester answers (confirmed)", 1)
    block = block.split("\n## ", 1)[0]
    assert ANSWER not in before
    assert f"> {QUESTION}" in block
    assert f"> {ANSWER}" in block
    # Line structure survives: the reproduction stays runnable as written.
    assert ">     import foobar\n>     assert False" in block
    assert "## Constraints" in before
    assert "## Assumptions (non-binding)" not in before


def test_seed_without_requester_answers_serializes_as_before() -> None:
    ledger = _ready_ledger(("constraints", _requester_entry("constraints", ANSWER)))
    seed = synthesize_seed_from_ledger(ledger)

    assert "requester_answers" in seed.to_dict()
    assert Seed.from_dict(seed.to_dict()).requester_answers == seed.requester_answers
    bare = seed.model_copy(update={"requester_answers": ()})
    assert "requester_answers" not in bare.to_dict()
    assert "Requester answers" not in render_seed_contract_for_execution(
        SeedContract.from_seed(bare)
    )


# -- an unanswered question renders nothing -------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        RequesterReply("empty_answer", detail="empty stdout"),
        RequesterReply("timeout"),
    ],
)
async def test_draft_kept_after_an_unanswered_question_is_marked(
    tmp_path: Path, reply: RequesterReply
) -> None:
    ledger = await _driver_ledger(tmp_path, reply)

    asked = [
        entry
        for section in ledger.sections.values()
        for entry in section.entries
        if entry.question == QUESTION
    ]
    assert asked
    assert all(entry.requester_unanswered for entry in asked)


def test_unanswered_question_and_its_draft_never_reach_the_worker() -> None:
    draft = LedgerEntry(
        key="acceptance_criteria.extend_scope",
        value=DRAFT,
        source=LedgerSource.ASSUMPTION,
        confidence=0.7,
        status=LedgerStatus.DEFAULTED,
        question=QUESTION,
        requester_unanswered=True,
    )
    seed = synthesize_seed_from_ledger(_ready_ledger(("acceptance_criteria", draft)))

    prompt = render_seed_contract_for_execution(SeedContract.from_seed(seed))

    assert QUESTION not in prompt
    assert DRAFT not in prompt
    assert seed.requester_answers == ()


def test_requester_unanswered_round_trips_and_is_omitted_when_false() -> None:
    marked: dict[str, Any] = LedgerEntry(
        key="k",
        value="v",
        source="assumption",
        confidence=0.5,
        status="defaulted",
        requester_unanswered=True,
    ).to_dict()
    plain = LedgerEntry(key="k", value="v", source="assumption", confidence=0.5, status="defaulted")

    assert LedgerEntry.from_dict(marked).requester_unanswered is True
    assert "requester_unanswered" not in plain.to_dict()
