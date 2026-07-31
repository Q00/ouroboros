"""Observation answers must not reach any requirement-producing consumer (#1755).

Every assertion here is a decidable postcondition on an *input*: the
observation's content is absent as a string, or a user decision is present as a
string. Nothing scans generated Seed output for paraphrases — once an extractor
has rewritten ``[from-code] 3 attempts`` into "retry three times", the marker is
gone and the sentence is indistinguishable from a decision. That is precisely
why withholding happens at the input, and why absence from the input is the
proof.
"""

from __future__ import annotations

import asyncio

import pytest

from ouroboros.bigbang.answer_provenance import (
    WITHHELD_ANSWER_NOTE,
    classify_answer_provenance,
    extraction_rounds,
)
from ouroboros.bigbang.interview import InterviewRound, InterviewState, InterviewStatus
from ouroboros.bigbang.pm_document import PMDocumentGenerator
from ouroboros.bigbang.pm_interview import PMInterviewEngine
from ouroboros.bigbang.pm_seed import PMSeed
from ouroboros.bigbang.requirement_distillation import build_requirement_distillation
from ouroboros.bigbang.seed_generator import SeedGenerator
from ouroboros.core.requirement_candidate import ConfirmationAuthority
from ouroboros.mcp.tools.authoring_handlers import _format_interview_transcript

# Carried only by the observation's answer. This is what must disappear.
OBSERVED_FACT = "2s/4s/8s exponential backoff hardcoded in RetryPolicy.java"
RESEARCHED_FACT = "Competitor X ships SSO in their $99 tier"
USER_DECISION = "Make it five attempts, and total user wait must stay under 30s"
# A question written after the observation landed, restating part of it. Kept in
# full on purpose — the two are asserted separately precisely because the rule is
# per-role: the answer slot confers requirement authority, the question slot does
# not.
TAINTED_QUESTION = "You are at 3 attempts today — where should that go?"


def _state(*, with_observation: bool = True) -> InterviewState:
    """An interview whose decision was informed by two adopted facts."""
    state = InterviewState(interview_id="iv-1755", initial_context="Improve payment retry behavior")
    if with_observation:
        state.record_answer("What is the retry policy today?", f"[from-code] {OBSERVED_FACT}")
        state.record_answer("How do competitors handle this?", f"[from-research] {RESEARCHED_FACT}")
    state.record_answer(TAINTED_QUESTION, USER_DECISION)
    return state


# ---------------------------------------------------------------------------
# Classification — decided once, at the point the answer enters
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        "[from-code] fact",
        "[from-repo] fact",
        "[from-research] fact",
        "[FROM-CODE] fact",
        "  [from-code] leading whitespace",
        "[from-code][auto-confirmed] compound prefix",
        "[from-research][refined] refined suffix",
    ],
)
def test_adopted_facts_classify_as_observation(answer: str) -> None:
    assert classify_answer_provenance(answer) == "observation"


@pytest.mark.parametrize(
    "answer",
    [
        "Make it five",
        "[from-user] Make it five",
        "[from-user][refined] Make it five",
        # Generated answers are not *human* decisions, but they are decisions:
        # the auto driver commits to them on the user's behalf. Classifying
        # them as observations would withhold all of ``ooo auto``'s
        # contribution from extraction.
        "[from-auto] retry five times",
        "[from-auto][safe-default-synthesis] accepted safe defaults",
        "[from-safe-default] conservative default",
    ],
)
def test_decisions_classify_as_user(answer: str) -> None:
    assert classify_answer_provenance(answer) == "user"


def test_provenance_is_decided_when_the_answer_arrives_not_at_construction() -> None:
    """The MCP transports persist a round question-only and fill it later."""
    state = InterviewState(interview_id="iv-1755", initial_context="goal")
    state.rounds.append(InterviewRound(round_number=1, question="What exists today?"))
    assert state.rounds[-1].provenance == "user"

    state.record_answer("What exists today?", f"[from-code] {OBSERVED_FACT}")

    assert len(state.rounds) == 1, "the pending round is filled, not duplicated"
    assert state.rounds[0].provenance == "observation"


def test_a_round_persisted_before_this_field_existed_is_still_classified() -> None:
    """In-flight interviews upgraded into this change must not stay exposed.

    Their JSON has no ``provenance`` key, and defaulting to ``"user"`` would
    leave every observation already on disk carrying requirement authority.
    """
    decision = InterviewRound.model_validate(
        {"round_number": 1, "question": "q", "user_response": "make it five"}
    )
    observation = InterviewRound.model_validate(
        {"round_number": 2, "question": "q", "user_response": f"[from-code] {OBSERVED_FACT}"}
    )

    assert decision.provenance == "user"
    assert observation.provenance == "observation"


def test_legacy_reframed_pm_answer_recovers_observation_provenance() -> None:
    """The old PM envelope put its label before the caller's marker."""
    round_data = InterviewRound.model_validate(
        {
            "round_number": 1,
            "question": (
                "[Original technical question: Which retry policy?]\n"
                "[PM was asked (reframed): What behavior exists today?]"
            ),
            "user_response": f"PM answer: [from-code] {OBSERVED_FACT}",
        }
    )

    assert round_data.provenance == "observation"
    restored = InterviewRound.model_validate(round_data.model_dump(mode="json"))
    assert restored.provenance == "observation"

    state = InterviewState(interview_id="legacy-pm", initial_context="goal", rounds=[restored])
    assert extraction_rounds(state)[0].answer == WITHHELD_ANSWER_NOTE
    distillation = build_requirement_distillation(state)
    rendered = " ".join(
        [*(c.text for c in distillation.candidates), *(e.text for e in distillation.evidence)]
    )
    assert OBSERVED_FACT not in rendered


def test_an_unanswered_round_persisted_without_the_field_stays_user() -> None:
    revived = InterviewRound.model_validate({"round_number": 1, "question": "q"})
    assert revived.provenance == "user"


def test_a_distillation_cached_before_this_change_is_not_reused() -> None:
    """Provenance is a canonical input, so it has to move the fingerprint.

    Otherwise a distillation cached when the observation was still promotable
    is judged current and handed back unchanged.
    """
    state = _state()
    stale = state.requirement_input_fingerprint()

    for round_data in state.rounds:
        round_data.provenance = "user"

    assert state.requirement_input_fingerprint() != stale


# ---------------------------------------------------------------------------
# The projection every requirement-producing consumer reads
# ---------------------------------------------------------------------------


def test_projection_withholds_observation_answers_and_keeps_questions() -> None:
    projected = extraction_rounds(_state())

    assert projected[0].answer == WITHHELD_ANSWER_NOTE
    assert projected[1].answer == WITHHELD_ANSWER_NOTE
    assert projected[2].answer == USER_DECISION
    # The question that restates the observation is preserved deliberately:
    # sharpening the next question is what the observation was collected for.
    assert projected[2].question == TAINTED_QUESTION


def test_projection_leaves_unanswered_rounds_alone() -> None:
    state = InterviewState(interview_id="iv-1755", initial_context="goal")
    state.rounds.append(InterviewRound(round_number=1, question="pending?"))

    projected = extraction_rounds(state)

    assert projected[0].answer is None
    assert projected[0].withheld is False


# ---------------------------------------------------------------------------
# Per consumer: the observation is absent, the decision survives
# ---------------------------------------------------------------------------


def _dev_seed_context(state: InterviewState) -> str:
    return SeedGenerator(llm_adapter=None)._build_interview_context(state)


def _pm_seed_context(state: InterviewState) -> str:
    return PMInterviewEngine._build_interview_context(
        object.__new__(PMInterviewEngine), state, withhold_observations=True
    )


def _plugin_transcript(state: InterviewState) -> str:
    return _format_interview_transcript(state, withhold_observations=True)


def _prd_qa_text(state: InterviewState) -> str:
    generator = object.__new__(PMDocumentGenerator)
    pairs = [(r.question, r.answer or "") for r in extraction_rounds(state)]
    return generator._build_generation_prompt(PMSeed(goal="g"), pairs)


CONSUMERS = {
    "dev_seed": _dev_seed_context,
    "pm_seed": _pm_seed_context,
    "plugin_seed": _plugin_transcript,
    "prd": _prd_qa_text,
}


@pytest.mark.parametrize("render", CONSUMERS.values(), ids=list(CONSUMERS))
def test_observation_content_never_reaches_a_requirement_consumer(render) -> None:
    rendered = render(_state())

    assert OBSERVED_FACT not in rendered
    assert RESEARCHED_FACT not in rendered
    assert WITHHELD_ANSWER_NOTE in rendered


@pytest.mark.parametrize("render", CONSUMERS.values(), ids=list(CONSUMERS))
def test_the_user_decision_in_the_same_interview_is_unaffected(render) -> None:
    assert USER_DECISION in render(_state())


@pytest.mark.parametrize("render", CONSUMERS.values(), ids=list(CONSUMERS))
def test_question_lines_render_in_full_after_a_withheld_answer(render) -> None:
    """Intended behavior, not a conceded leak.

    An observation is collected in order to sharpen the next question — that is
    where it is supposed to arrive. Redacting the question would also make the
    user's own decision uninterpretable ("make it five" of what?). Pinned so a
    later reader does not "finish the job".
    """
    assert TAINTED_QUESTION in render(_state())


@pytest.mark.parametrize("render", CONSUMERS.values(), ids=list(CONSUMERS))
def test_an_interview_with_no_observation_renders_exactly_as_before(render) -> None:
    rendered = render(_state(with_observation=False))

    assert WITHHELD_ANSWER_NOTE not in rendered
    assert USER_DECISION in rendered


# ---------------------------------------------------------------------------
# The consumer that assembles no prompt at all
# ---------------------------------------------------------------------------


def test_distillation_does_not_promote_an_observation() -> None:
    """This path emits a Seed with no LLM in it, so prompt-level withholding
    would never have reached it."""
    state = InterviewState(interview_id="iv-1755", initial_context="Improve payment retry behavior")
    # Phrased so the explicit-requirement regex would promote it if allowed.
    state.record_answer("What exists today?", f"[from-code] The service must use {OBSERVED_FACT}")
    state.record_answer("How many attempts?", f"{USER_DECISION}. This is required.")

    distillation = build_requirement_distillation(state)
    rendered = " ".join(
        [*(c.text for c in distillation.candidates), *(e.text for e in distillation.evidence)]
    )

    assert OBSERVED_FACT not in rendered
    assert USER_DECISION in rendered


# ---------------------------------------------------------------------------
# Boundary: the interview itself keeps seeing everything
# ---------------------------------------------------------------------------


def test_the_interview_subagent_history_is_not_withheld() -> None:
    """Question generation reads the unredacted transcript on purpose."""
    rendered = _format_interview_transcript(_state())

    assert OBSERVED_FACT in rendered
    assert RESEARCHED_FACT in rendered
    assert WITHHELD_ANSWER_NOTE not in rendered


def test_the_pm_question_classifier_is_not_withheld() -> None:
    rendered = PMInterviewEngine._build_interview_context(
        object.__new__(PMInterviewEngine), _state()
    )

    assert OBSERVED_FACT in rendered
    assert WITHHELD_ANSWER_NOTE not in rendered


# ---------------------------------------------------------------------------
# Reference contrasts: a fact cannot stand in for the user's confirmation
# ---------------------------------------------------------------------------
#
# "Make it like Stripe" does not say what to copy and what to drop, so the
# interview asks a contrast question and holds a required `contrast-required`
# candidate at UNKNOWN / authority NONE until the user answers it. Answering
# that question with a fact *about* Stripe used to resolve it — a fact conferring
# a confirmation, which is this issue's defect expressed in the reference lane.
#
# It no longer does, so an interview whose only contrast answer is an adopted
# fact is refused. That refusal is the pre-existing reference gate no longer
# being satisfied falsely, not a new gate this rule adds.

CONTRAST_QUESTION = (
    "Reference `Stripe`\nWhat should this project copy or avoid from that reference?"
)
CONTRAST_FACT = "[from-code] Stripe uses a hosted payment page"


def _reference_state(contrast_answer: str) -> InterviewState:
    from ouroboros.interview_adapters import (
        ReferenceContrastResolution,
        ReferenceCue,
        ReferenceResolutionStatus,
    )
    from ouroboros.interview_adapters.models import ReferenceOrigin

    state = InterviewState(interview_id="iv-ref", initial_context="Build a Stripe-like checkout")
    state.reference_cues = (
        ReferenceCue(reference_id="stripe", label="Stripe", origin=ReferenceOrigin.USER_TEXT),
    )
    state.reference_resolutions = (
        ReferenceContrastResolution(
            reference_id="stripe",
            status=ReferenceResolutionStatus.RESOLVED,
            asked_question=CONTRAST_QUESTION,
            answer=contrast_answer,
        ),
    )
    state.record_answer(CONTRAST_QUESTION, contrast_answer)
    state.record_answer("How many retries?", USER_DECISION)
    state.status = InterviewStatus.COMPLETED
    return state


def test_a_fact_does_not_resolve_a_reference_contrast() -> None:
    distillation = build_requirement_distillation(_reference_state(CONTRAST_FACT))

    unresolved = [
        candidate
        for candidate in distillation.candidates
        if candidate.candidate_id.endswith(":contrast-required")
    ]
    assert unresolved, "the contrast requirement must survive an observation answer"
    assert unresolved[0].confirmation_authority is ConfirmationAuthority.NONE
    assert CONTRAST_FACT.removeprefix("[from-code] ") not in " ".join(
        candidate.text for candidate in distillation.candidates
    )


def test_a_user_decided_contrast_still_resolves() -> None:
    """The gate is not simply always closed — a real decision opens it."""
    distillation = build_requirement_distillation(
        _reference_state("Copy the hosted page, avoid their embedded form")
    )

    assert not [
        candidate
        for candidate in distillation.candidates
        if candidate.candidate_id.endswith(":contrast-required")
        and candidate.confirmation_authority is ConfirmationAuthority.NONE
    ]


def test_an_unconfirmed_contrast_is_refused_as_a_typed_result_not_a_crash() -> None:
    from ouroboros.bigbang.ambiguity import AmbiguityScore, ComponentScore, ScoreBreakdown

    breakdown = ScoreBreakdown(
        **{
            name: ComponentScore(name=name, clarity_score=0.9, weight=0.25, justification="t")
            for name in ScoreBreakdown.model_fields
        }
    )
    score = AmbiguityScore(overall_score=0.15, breakdown=breakdown)

    result = asyncio.run(
        SeedGenerator(llm_adapter=None).generate(_reference_state(CONTRAST_FACT), score)
    )

    assert result.is_err
    assert "reopened" in str(result.error)
    blockers = result.error.details["blockers"]
    assert any(item["code"] == "reference_confirmation_required" for item in blockers)


def test_the_user_decision_still_renders_even_when_the_contrast_is_unconfirmed() -> None:
    """The decision is not what is blocked — a missing confirmation is."""
    rendered = _dev_seed_context(_reference_state(CONTRAST_FACT))

    assert USER_DECISION in rendered
    assert WITHHELD_ANSWER_NOTE in rendered
