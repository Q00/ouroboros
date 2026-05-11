"""Unit tests for the research DomainProfile (#809 P3, PR 6/6)."""

from __future__ import annotations

from pathlib import Path

from ouroboros.auto.profiles.research import (
    RESEARCH_PROFILE,
    _CitationFormatPredicate,
    _research_detector,
    _ResearchIntentClassifier,
    _SourceCountPredicate,
)


def test_detector_zero_on_empty_dir(tmp_path: Path) -> None:
    assert _research_detector(tmp_path) == 0.0


def test_detector_scores_bibliography_directory(tmp_path: Path) -> None:
    (tmp_path / "references.bib").write_text("")
    score = _research_detector(tmp_path)
    assert score >= 0.6


def test_detector_scores_bibliography_dot_dir(tmp_path: Path) -> None:
    (tmp_path / ".bibliography").mkdir()
    score = _research_detector(tmp_path)
    assert score >= 0.6


def test_intent_classifier_picks_literature_review() -> None:
    clf = _ResearchIntentClassifier()
    assert clf.classify("Write a survey of transformer models") == "literature_review"


def test_intent_classifier_picks_hypothesis_check() -> None:
    clf = _ResearchIntentClassifier()
    assert clf.classify("Verify the hypothesis that X causes Y") == "hypothesis_check"


def test_intent_classifier_returns_none_for_unmatched_question() -> None:
    clf = _ResearchIntentClassifier()
    assert clf.classify("What is the weather today?") is None


def test_source_count_predicate_matches_numeric_criterion() -> None:
    pred = _SourceCountPredicate()
    assert pred.matches("must cite at least 5 sources")
    assert not pred.matches("no numbers here")
    assert not pred.matches("source without digit")


def test_citation_format_predicate_matches_citation_keyword() -> None:
    pred = _CitationFormatPredicate()
    assert pred.matches("citation style must be APA")
    assert pred.matches("bibliography entries are required")
    assert pred.matches("include references section")
    assert not pred.matches("run all unit tests")


def test_vague_terms_contain_research_specific_words() -> None:
    vague = RESEARCH_PROFILE.vague_terms
    assert "thorough" in vague
    assert "comprehensive" in vague
    assert "rigorous" in vague


def test_research_profile_is_registered_in_default_registry() -> None:
    from ouroboros.auto.domain_profile import DEFAULT_REGISTRY

    names = {p.name for p in DEFAULT_REGISTRY.all()}
    assert "research" in names
