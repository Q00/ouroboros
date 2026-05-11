"""Contract tests for DomainProfile and VerifiablePredicate (#809 P3, PR 1/6).

These tests verify the Protocol stubs, dataclass invariants, and registry
semantics introduced in ``domain_profile.py``.  No real domain profiles are
imported; every fixture is a minimal inline stub.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from ouroboros.auto.domain_profile import (
    DEFAULT_REGISTRY,
    DomainProfile,
    DomainProfileRegistry,
    IntentClassifier,
    RepoContextExtractor,
    VerifiablePredicate,
)

# ---------------------------------------------------------------------------
# Minimal stubs
# ---------------------------------------------------------------------------


class _ExitCodePredicate:
    code = "exit_code"

    def matches(self, criterion: str) -> bool:
        return "exit" in criterion.lower()

    def repair_template(self, criterion: str) -> str:
        return f"Command exits 0 when: {criterion}"


class _ContrastPredicate:
    code = "wcag_contrast"

    def matches(self, criterion: str) -> bool:
        return "contrast" in criterion.lower()

    def repair_template(self, criterion: str) -> str:
        return f"Contrast ratio ≥ 4.5:1 for: {criterion}"


class _SimpleClassifier:
    def classify(self, question: str) -> str | None:
        if "runtime" in question.lower():
            return "runtime_context"
        return None

    def supported_intents(self) -> frozenset[str]:
        return frozenset({"runtime_context", "acceptance_criteria"})


class _SimpleExtractor:
    def extract(self, cwd: Path) -> dict[str, Any]:
        return {"cwd": str(cwd)}


def _make_profile(
    name: str = "coding",
    confidence: float = 0.8,
    predicates: tuple[VerifiablePredicate, ...] = (),
) -> DomainProfile:
    return DomainProfile(
        name=name,
        repo_context_extractor=_SimpleExtractor(),
        verifiable_predicates=predicates,
        intent_classifier=_SimpleClassifier(),
        vague_terms=frozenset({"easy", "clean"}),
        safe_defaults={"runtime_context": "existing project"},
        detector=lambda _cwd: confidence,
    )


# ---------------------------------------------------------------------------
# Protocol smoke tests
# ---------------------------------------------------------------------------


def test_verifiable_predicate_protocol_smoke() -> None:
    pred = _ExitCodePredicate()
    assert isinstance(pred, VerifiablePredicate)
    assert pred.code == "exit_code"
    assert pred.matches("command exits 0")
    assert not pred.matches("stdout snapshot")
    assert "exit" in pred.repair_template("some criterion").lower()


def test_intent_classifier_protocol_smoke() -> None:
    clf = _SimpleClassifier()
    assert isinstance(clf, IntentClassifier)
    assert clf.classify("Which runtime should we use?") == "runtime_context"
    assert clf.classify("something unrelated") is None
    assert "runtime_context" in clf.supported_intents()


def test_repo_context_extractor_protocol_smoke() -> None:
    extractor = _SimpleExtractor()
    assert isinstance(extractor, RepoContextExtractor)
    result = extractor.extract(Path("/tmp"))
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# DomainProfile invariants
# ---------------------------------------------------------------------------


def test_domain_profile_is_frozen() -> None:
    profile = _make_profile()
    with pytest.raises(FrozenInstanceError):
        profile.name = "mutated"  # type: ignore[misc]


def test_find_verifiable_predicate_returns_first_match() -> None:
    exit_pred = _ExitCodePredicate()
    contrast_pred = _ContrastPredicate()
    profile = _make_profile(predicates=(exit_pred, contrast_pred))

    result = profile.find_verifiable_predicate("command exits 0 on success")
    assert result is exit_pred

    result2 = profile.find_verifiable_predicate("color contrast check")
    assert result2 is contrast_pred

    result3 = profile.find_verifiable_predicate("stdout contains expected output")
    assert result3 is None


# ---------------------------------------------------------------------------
# DomainProfileRegistry
# ---------------------------------------------------------------------------


def test_registry_rejects_duplicate_name() -> None:
    registry = DomainProfileRegistry()
    registry.register(_make_profile(name="coding"))
    with pytest.raises(ValueError, match="coding"):
        registry.register(_make_profile(name="coding"))


def test_registry_get_returns_none_for_unknown() -> None:
    registry = DomainProfileRegistry()
    assert registry.get("nonexistent") is None


def test_registry_get_returns_registered_profile() -> None:
    registry = DomainProfileRegistry()
    profile = _make_profile(name="coding")
    registry.register(profile)
    assert registry.get("coding") is profile


def test_registry_detect_best_picks_highest_confidence() -> None:
    registry = DomainProfileRegistry()
    low = _make_profile(name="low", confidence=0.2)
    high = _make_profile(name="high", confidence=0.9)
    registry.register(low)
    registry.register(high)

    best = registry.detect_best(Path("/tmp"))
    assert best is not None
    assert best.name == "high"


def test_registry_detect_best_breaks_ties_by_registration_order() -> None:
    registry = DomainProfileRegistry()
    first = _make_profile(name="first", confidence=0.7)
    second = _make_profile(name="second", confidence=0.7)
    registry.register(first)
    registry.register(second)

    best = registry.detect_best(Path("/tmp"))
    assert best is not None
    assert best.name == "first"


def test_registry_detect_best_returns_none_when_empty() -> None:
    registry = DomainProfileRegistry()
    assert registry.detect_best(Path("/tmp")) is None


def test_registry_union_predicates_applies_threshold() -> None:
    registry = DomainProfileRegistry()
    exit_pred = _ExitCodePredicate()
    contrast_pred = _ContrastPredicate()

    above = DomainProfile(
        name="above",
        repo_context_extractor=_SimpleExtractor(),
        verifiable_predicates=(exit_pred,),
        intent_classifier=_SimpleClassifier(),
        vague_terms=frozenset(),
        safe_defaults={},
        detector=lambda _: 0.8,
    )
    below = DomainProfile(
        name="below",
        repo_context_extractor=_SimpleExtractor(),
        verifiable_predicates=(contrast_pred,),
        intent_classifier=_SimpleClassifier(),
        vague_terms=frozenset(),
        safe_defaults={},
        detector=lambda _: 0.3,
    )
    registry.register(above)
    registry.register(below)

    predicates = registry.union_predicates(Path("/tmp"), threshold=0.5)
    codes = [p.code for p in predicates]
    assert "exit_code" in codes
    assert "wcag_contrast" not in codes


def test_default_registry_contains_coding_after_pr2(tmp_path: Path) -> None:
    # DEFAULT_REGISTRY is a module-level singleton.  PR-2 registers the
    # built-in ``coding`` profile; importing the profiles package is enough.
    import ouroboros.auto.profiles  # noqa: F401 — trigger registration

    from ouroboros.auto.profiles.coding import CODING_PROFILE

    assert DEFAULT_REGISTRY.get("coding") is CODING_PROFILE
    # detect_best only returns a profile when the detector scores > 0.0;
    # create a pyproject.toml marker so the coding detector fires.
    (tmp_path / "pyproject.toml").write_text("[project]\nname='t'\n")
    assert DEFAULT_REGISTRY.detect_best(tmp_path) is CODING_PROFILE
