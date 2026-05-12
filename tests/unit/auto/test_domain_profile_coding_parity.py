"""Parity tests for the built-in ``coding`` DomainProfile (#809 P3, PR 2/6).

These tests pin the ``coding`` DomainProfile against the existing constants
and functions in ``answerer.py``, ``grading.py``, and ``safe_defaults.py``.
They are the safety net for PR-4 and PR-5: as long as these pass, the
refactored callers produce identical behaviour to the originals.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest

from ouroboros.auto.domain_profile import DEFAULT_REGISTRY
from ouroboros.auto.grading import VAGUE_TERMS, _is_observable  # noqa: PLC2701
from ouroboros.auto.profiles.coding import CODING_PROFILE
from ouroboros.auto.safe_defaults import _SAFE_DEFAULTS  # noqa: PLC2701

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_coding_profile_registered_in_default_registry() -> None:
    """Querying the default registry lazily registers the coding profile."""
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


def test_safe_defaults_mapping_is_immutable() -> None:
    """Callers must not be able to mutate shared singleton defaults."""
    assert isinstance(CODING_PROFILE.safe_defaults, MappingProxyType)
    with pytest.raises(TypeError):
        CODING_PROFILE.safe_defaults["runtime_context"] = object()  # type: ignore[index]


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
        ("Can users verify their email?", "product_behavior"),
    ],
)
def test_intent_classifier_matches_existing_classifier(question: str, expected_intent: str) -> None:
    """intent_classifier.classify() must agree with existing answer routing."""
    from ouroboros.auto.answerer import _classify_question_intents  # noqa: PLC2701

    raw_intents = _classify_question_intents(question)
    raw_values = {i.value for i in raw_intents}
    profile_result = CODING_PROFILE.intent_classifier.classify(question)

    # The profile must return a value that is also in the raw classifier output.
    assert profile_result in raw_values, (
        f"classify({question!r}) returned {profile_result!r}; raw classifier returned {raw_values}"
    )
    assert profile_result == expected_intent
    assert expected_intent in raw_values


def test_intent_classifier_returns_none_for_unknown() -> None:
    """An unclassifiable question returns None from classify()."""
    result = CODING_PROFILE.intent_classifier.classify("The weather today is pleasant and mild.")
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
        ("Process returns with exit code 0", "exit_code"),
        ("Test suite passes and verifies stdout output", "test_pass"),
        ("pytest check passes and verifies stdout output", "test_pass"),
        ("Linter check passes and verifies stdout output", "lint_clean"),
        ("ruff check passes and verifies stdout output", "lint_clean"),
        ("mypy type check passes and verifies stdout output", "type_check_clean"),
        ("type check with pyright verifies stdout output", "type_check_clean"),
        ("GET /health responds with HTTP status 200", "observable_behavior"),
        ("CLI stdout contains created habits", "observable_behavior"),
        ("Export writes a JSON artifact file", "observable_behavior"),
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


def test_observable_predicate_matches_grading_observable_contract() -> None:
    """The profile must accept every AC that the current grading gate accepts."""
    criteria = (
        "`habit list` prints stable stdout containing created habits",
        "The API endpoint returns response status 200",
        "The command writes an artifact file to disk",
        "stderr contains the validation error for invalid input",
        "DELETE /items/{id} responds with HTTP 204",
    )
    for criterion in criteria:
        assert _is_observable(criterion), criterion
        assert CODING_PROFILE.find_verifiable_predicate(criterion) is not None, criterion


@pytest.mark.parametrize(
    "criterion",
    [
        "All tests pass",
        "pytest reports no failures",
        "Linter reports zero errors",
        "ruff check passes",
        "mypy reports no type errors",
        "type check clean with pyright",
        "exit_code must be zero",
    ],
)
def test_predicates_do_not_widen_grading_observable_contract(criterion: str) -> None:
    """The profile must not accept criteria rejected by the current grading gate."""
    assert not _is_observable(criterion), criterion
    assert CODING_PROFILE.find_verifiable_predicate(criterion) is None


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
    """detector returns 1.0 for a Node/JS coding repo marker."""
    (tmp_path / "package.json").write_text('{"name": "test"}\n')
    assert CODING_PROFILE.detector(tmp_path) == 1.0


@pytest.mark.parametrize("marker", ["go.mod", "Cargo.toml", "pom.xml", "Gemfile"])
def test_detector_returns_one_for_other_coding_markers(tmp_path: Path, marker: str) -> None:
    """detector recognizes common non-Python coding repo markers."""
    (tmp_path / marker).write_text("module example\n")
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
