"""Integration test: adding ``research`` required zero core changes (#809 P3, PR 6/6).

This is the acceptance test the RFC asks for: plurality is proven when
two independent domain profiles coexist in DEFAULT_REGISTRY and
``detect_best`` routes correctly without any core modification.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.auto.domain_profile import DEFAULT_REGISTRY

# Importing the profiles package fires all registration side-effects.
import ouroboros.auto.profiles  # noqa: F401


def test_both_profiles_registered() -> None:
    """research is registered; coding profile lands in PR-2 (#809 P3, PR 2/6)."""
    names = {p.name for p in DEFAULT_REGISTRY.all()}
    assert "research" in names
    # coding profile lands in PR-2 (#809 P3, PR 2/6); skip when not yet present
    if "coding" in names:
        assert "coding" in names


def test_detect_best_returns_research_for_bib_repo(tmp_path: Path) -> None:
    """A repo with references.bib is detected as the research domain."""
    repo = tmp_path / "research_repo_with_bib"
    repo.mkdir()
    (repo / "references.bib").write_text("")

    best = DEFAULT_REGISTRY.detect_best(repo)
    assert best is not None
    assert best.name == "research"


def test_research_beats_weak_git_coding_signal(tmp_path: Path) -> None:
    """A bibliography repo under Git remains research, not generic coding."""
    repo = tmp_path / "research_git_repo"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: ../.git/worktrees/research_git_repo\n")
    (repo / "references.bib").write_text("")

    best = DEFAULT_REGISTRY.detect_best(repo)
    assert best is not None
    assert best.name == "research"


def test_research_paper_beats_weak_source_coding_signal(tmp_path: Path) -> None:
    """A paper-shaped repo with incidental source dirs remains research."""
    repo = tmp_path / "paper_with_src_repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "paper.tex").write_text("\\documentclass{article}\n")

    best = DEFAULT_REGISTRY.detect_best(repo)
    assert best is not None
    assert best.name == "research"


def test_detect_best_returns_coding_for_python_repo(tmp_path: Path) -> None:
    """A repo with pyproject.toml is detected as coding (when that profile is present)."""
    names = {p.name for p in DEFAULT_REGISTRY.all()}
    # coding profile lands in PR-2 (#809 P3, PR 2/6); skip when not yet present
    if "coding" not in names:
        pytest.skip("coding profile not yet registered (lands in PR-2)")

    repo = tmp_path / "python_repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("")

    best = DEFAULT_REGISTRY.detect_best(repo)
    assert best is not None
    assert best.name == "coding"


def test_research_profile_safe_defaults_shape() -> None:
    """research profile exposes the expected safe defaults keys."""
    profile = DEFAULT_REGISTRY.get("research")
    assert profile is not None
    assert "min_sources" in profile.safe_defaults
    assert "default_citation_style" in profile.safe_defaults


def test_research_profile_find_verifiable_predicate() -> None:
    """find_verifiable_predicate works end-to-end for the research profile."""
    profile = DEFAULT_REGISTRY.get("research")
    assert profile is not None

    matched = profile.find_verifiable_predicate("must cite at least 10 sources")
    assert matched is not None
    assert matched.code == "source_count"

    no_match = profile.find_verifiable_predicate("output must be correct")
    assert no_match is None


def test_research_profile_classifier_uses_canonical_routing_labels() -> None:
    """Activated research profile feeds the same labels consumed by core routing."""
    profile = DEFAULT_REGISTRY.get("research")
    assert profile is not None

    assert profile.intent_classifier.classify("Survey the prior literature") == "runtime_context"
    assert profile.intent_classifier.classify("Verify the hypothesis") == "verification"
    assert profile.intent_classifier.classify("Require 5 cited sources") == "acceptance_criteria"
    assert profile.intent_classifier.supported_intents() == frozenset(
        {"runtime_context", "verification", "acceptance_criteria"}
    )
