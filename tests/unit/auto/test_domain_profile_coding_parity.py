"""Parity tests for the built-in ``coding`` DomainProfile (#809 P3, PR 2/6).

These tests pin the ``coding`` DomainProfile against the existing constants
and functions in ``answerer.py``, ``grading.py``, and ``safe_defaults.py``.
They are the safety net for PR-4 and PR-5: as long as these pass, the
refactored callers produce identical behaviour to the originals.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.auto.domain_profile import DEFAULT_REGISTRY
from ouroboros.auto.grading import VAGUE_TERMS
from ouroboros.auto.profiles.coding import CODING_PROFILE
from ouroboros.auto.safe_defaults import _SAFE_DEFAULTS  # noqa: PLC2701

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_coding_profile_registered_in_default_registry() -> None:
    """Importing the profile module must auto-register it."""
    assert DEFAULT_REGISTRY.get("coding") is CODING_PROFILE


# ---------------------------------------------------------------------------
# vague_terms parity
# ---------------------------------------------------------------------------


def test_vague_terms_match_grading_constant() -> None:
    """CODING_PROFILE.vague_terms must equal frozenset(VAGUE_TERMS)."""
    assert CODING_PROFILE.vague_terms == frozenset(VAGUE_TERMS)


@pytest.mark.parametrize("term", list(VAGUE_TERMS))
def test_each_vague_term_present_in_profile(term: str) -> None:
    """Every term from grading.VAGUE_TERMS must be in the profile."""
    assert term in CODING_PROFILE.vague_terms


# ---------------------------------------------------------------------------
# safe_defaults parity
# ---------------------------------------------------------------------------


def test_safe_defaults_keys_match() -> None:
    """CODING_PROFILE.safe_defaults must have the same keys as _SAFE_DEFAULTS."""
    assert set(CODING_PROFILE.safe_defaults.keys()) == set(_SAFE_DEFAULTS.keys())


def test_safe_defaults_values_match() -> None:
    """CODING_PROFILE.safe_defaults values must be the same objects as _SAFE_DEFAULTS."""
    for key in _SAFE_DEFAULTS:
        assert CODING_PROFILE.safe_defaults[key] is _SAFE_DEFAULTS[key], (
            f"safe_defaults[{key!r}] diverged from _SAFE_DEFAULTS"
        )


# ---------------------------------------------------------------------------
# intent_classifier parity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "expected_intent"),
    [
        ("What are the non-goals of this feature?", "non_goals"),
        ("How do we verify the output?", "verification"),
        ("What are the acceptance criteria?", "acceptance_criteria"),
        ("Which framework does this project use?", "runtime_context"),
        ("Who are the actors and what are the inputs?", "actor_io"),
    ],
)
def test_intent_classifier_matches_existing_classifier(
    question: str, expected_intent: str
) -> None:
    """intent_classifier.classify() must agree with _classify_question_intents."""
    from ouroboros.auto.answerer import _classify_question_intents  # noqa: PLC2701

    raw_intents = _classify_question_intents(question)
    raw_values = {i.value for i in raw_intents}
    profile_result = CODING_PROFILE.intent_classifier.classify(question)

    # The profile must return a value that is also in the raw classifier output.
    assert profile_result in raw_values, (
        f"classify({question!r}) returned {profile_result!r}; "
        f"raw classifier returned {raw_values}"
    )
    assert expected_intent in raw_values


def test_intent_classifier_returns_none_for_unknown() -> None:
    """An unclassifiable question returns None from classify()."""
    result = CODING_PROFILE.intent_classifier.classify(
        "The weather today is pleasant and mild."
    )
    assert result is None


def test_intent_classifier_supported_intents() -> None:
    """supported_intents() must include all QuestionIntent values."""
    from ouroboros.auto.answerer import QuestionIntent

    expected = frozenset(qi.value for qi in QuestionIntent)
    assert expected <= CODING_PROFILE.intent_classifier.supported_intents()


# ---------------------------------------------------------------------------
# verifiable_predicate parity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("criterion", "expected_code"),
    [
        ("Command exits 0 on success", "exit_code"),
        ("exit_code must be zero", "exit_code"),
        ("All tests pass", "test_pass"),
        ("pytest reports no failures", "test_pass"),
        ("Linter reports zero errors", "lint_clean"),
        ("ruff check passes", "lint_clean"),
        ("mypy reports no type errors", "type_check_clean"),
        ("type check clean with pyright", "type_check_clean"),
    ],
)
def test_predicate_matches_known_criteria(criterion: str, expected_code: str) -> None:
    """find_verifiable_predicate must find the right predicate for known criteria."""
    predicate = CODING_PROFILE.find_verifiable_predicate(criterion)
    assert predicate is not None, f"No predicate matched {criterion!r}"
    assert predicate.code == expected_code


def test_predicate_repair_template_returns_string() -> None:
    """repair_template must return a non-empty string for any criterion."""
    for predicate in CODING_PROFILE.verifiable_predicates:
        result = predicate.repair_template("some criterion text")
        assert isinstance(result, str)
        assert len(result) > 0


def test_predicate_codes_are_unique() -> None:
    """All predicate codes must be unique within the profile."""
    codes = [p.code for p in CODING_PROFILE.verifiable_predicates]
    assert len(codes) == len(set(codes))


def test_predicate_no_match_returns_none() -> None:
    """find_verifiable_predicate returns None when nothing matches."""
    result = CODING_PROFILE.find_verifiable_predicate(
        "The product must feel delightful to the user."
    )
    assert result is None


# ---------------------------------------------------------------------------
# detector
# ---------------------------------------------------------------------------


def test_detector_returns_one_for_pyproject(tmp_path: Path) -> None:
    """detector returns 1.0 when pyproject.toml exists."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
    assert CODING_PROFILE.detector(tmp_path) == 1.0


def test_detector_returns_one_for_package_json(tmp_path: Path) -> None:
    """detector returns 1.0 when package.json exists."""
    (tmp_path / "package.json").write_text('{"name": "test"}\n')
    assert CODING_PROFILE.detector(tmp_path) == 1.0


def test_detector_returns_zero_for_empty_dir(tmp_path: Path) -> None:
    """detector returns 0.0 when neither marker file is present."""
    assert CODING_PROFILE.detector(tmp_path) == 0.0


# ---------------------------------------------------------------------------
# repo_context_extractor smoke test
# ---------------------------------------------------------------------------


def test_repo_context_extractor_returns_dict(tmp_path: Path) -> None:
    """extract() returns a dict (possibly empty) for an empty directory."""
    result = CODING_PROFILE.repo_context_extractor.extract(tmp_path)
    assert isinstance(result, dict)
