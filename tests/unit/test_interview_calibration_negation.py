"""Negated self-reports must not become evidence for more demanding wording."""

import pytest

from ouroboros.interview_calibration import infer_interview_calibration
from ouroboros.mcp.tools.interview_calibration import handle_interview_calibration_turn


@pytest.mark.parametrize(
    "evidence",
    [
        "I am not an expert in OAuth",
        "I am not a security expert in SAML",
        "I am no expert in distributed systems",
        "I do not teach OAuth",
        "I never teach SAML",
        "I can't teach cryptography",
        "I have not implemented OAuth",
    ],
)
def test_negated_positive_markers_are_not_proficiency_evidence(evidence):
    calibration = infer_interview_calibration(evidence)
    assert calibration.level == "working"
    assert calibration.confidence == "low"
    assert calibration.evidence == evidence
    assert calibration.unknown_terms == ()


@pytest.mark.parametrize("term", ["PKCE", "event sourcing", "refresh tokens"])
@pytest.mark.parametrize("verb", ["cannot explain", "can't explain", "can not explain"])
def test_negative_explanation_spellings_agree(term, verb):
    calibration = infer_interview_calibration(f"I {verb} {term}")
    assert calibration.level == "foundational"
    assert calibration.unknown_terms == (term,)


@pytest.mark.parametrize(
    ("evidence", "level"),
    [
        ("I am an expert in OAuth", "fluent"),
        ("I teach SAML deeply", "fluent"),
        ("I have implemented OAuth", "working"),
        ("I am not an expert. I teach SAML", "fluent"),
        ("I am not an expert but I teach SAML", "fluent"),
        ("I teach OAuth but I cannot explain PKCE", "foundational"),
    ],
)
def test_affirmative_and_mixed_evidence_keep_existing_precedence(evidence, level):
    assert infer_interview_calibration(evidence).level == level


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence",
    ["I cannot explain PKCE", "I have implemented OAuth", "I teach OAuth deeply"],
)
async def test_notice_uses_the_selected_prompt_wording(evidence):
    calibration = infer_interview_calibration(evidence)
    result = await handle_interview_calibration_turn(None, evidence, session_id=None)
    assert result.is_ok
    wording = calibration.prompt_guidance().split("- Wording rule: ", 1)[1]
    assert f"- Adaptation: {wording}" in result.value.text_content
    assert result.value.meta["interview_calibration"] == calibration.model_dump(mode="json")
