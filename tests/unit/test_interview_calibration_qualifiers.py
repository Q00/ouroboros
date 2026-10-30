"""Bounded qualifier-token regression cases; no transport or provider calls."""

import pytest

from ouroboros.interview_calibration import infer_interview_calibration

QUALIFIERS = ["security", "front-end", "C++", "back-end", "A+B", "C#", "F#", "A/B", "API_v2"]


@pytest.mark.parametrize("qualifier", QUALIFIERS)
def test_negated_short_qualifier(qualifier):
    evidence = f"I am not a {qualifier} expert"
    result = infer_interview_calibration(evidence)
    assert (result.level, result.confidence) == ("working", "low")
    assert result.evidence == evidence


@pytest.mark.parametrize("qualifier", QUALIFIERS)
def test_affirmative_short_qualifier(qualifier):
    assert infer_interview_calibration(f"I am a {qualifier} expert").level == "fluent"


@pytest.mark.parametrize(
    "separator", [". ", "! ", "? ", "; ", ", ", ": ", " but ", " however ", " yet "]
)
def test_qualifier_negation_stops_at_existing_clause_boundaries(separator):
    evidence = f"I am not a front-end specialist{separator}expert in C++"
    assert infer_interview_calibration(evidence).level == "fluent"


@pytest.mark.parametrize("qualifier", QUALIFIERS)
def test_three_intervening_tokens_remain_within_scope(qualifier):
    assert infer_interview_calibration(f"I am not a senior {qualifier} expert").level == "working"


@pytest.mark.parametrize("qualifier", QUALIFIERS)
def test_four_intervening_tokens_remain_outside_scope(qualifier):
    assert (
        infer_interview_calibration(f"I am not a very senior {qualifier} expert").level == "fluent"
    )


@pytest.mark.parametrize("verb", ["cannot explain", "can not explain", "can't explain"])
def test_unknown_marker_control(verb):
    result = infer_interview_calibration(f"I {verb} PKCE")
    assert (result.level, result.unknown_terms) == ("foundational", ("PKCE",))


@pytest.mark.parametrize("symbol", ["+", "-", "#"])
def test_standalone_punctuation_is_not_an_intervening_word(symbol):
    evidence = f"I am not a {symbol} expert"
    assert infer_interview_calibration(evidence).level == "fluent"
