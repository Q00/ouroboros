"""Contract tests for DomainProfile and VerifiablePredicate (#809 P3, PR 1/6).

These tests verify the Protocol stubs, dataclass invariants, and registry
semantics introduced in ``domain_profile.py``.  No real domain profiles are
imported; every fixture is a minimal inline stub.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path
import subprocess
import sys
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
    predicates: Iterable[VerifiablePredicate] = (),
    safe_defaults: Mapping[str, Any] | None = None,
    detector: Callable[[Path], float] | None = None,
) -> DomainProfile:
    return DomainProfile(
        name=name,
        repo_context_extractor=_SimpleExtractor(),
        verifiable_predicates=predicates,
        intent_classifier=_SimpleClassifier(),
        vague_terms=frozenset({"easy", "clean"}),
        safe_defaults=(
            safe_defaults if safe_defaults is not None else {"runtime_context": "existing project"}
        ),
        detector=detector or (lambda _cwd: confidence),
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


def test_domain_profile_safe_defaults_are_deeply_frozen() -> None:
    source_defaults: dict[str, Any] = {
        "runtime_context": {
            "summary": "existing project",
            "commands": ["pytest"],
            "tags": {"contract"},
        },
    }
    profile = _make_profile(safe_defaults=source_defaults)

    with pytest.raises(TypeError):
        profile.safe_defaults["acceptance_criteria"] = "specific"  # type: ignore[index]

    runtime_context = profile.safe_defaults["runtime_context"]
    assert isinstance(runtime_context, Mapping)
    with pytest.raises(TypeError):
        runtime_context["summary"] = "mutated"  # type: ignore[index]

    commands = runtime_context["commands"]
    assert commands == ("pytest",)
    with pytest.raises(TypeError):
        commands[0] = "ruff"  # type: ignore[index]

    assert runtime_context["tags"] == frozenset({"contract"})

    source_defaults["runtime_context"]["summary"] = "mutated outside profile"
    source_defaults["runtime_context"]["commands"].append("ruff")
    assert runtime_context["summary"] == "existing project"
    assert runtime_context["commands"] == ("pytest",)


def test_domain_profile_coerces_predicate_inputs_to_immutable_tuple() -> None:
    registry = DomainProfileRegistry()
    exit_pred = _ExitCodePredicate()
    contrast_pred = _ContrastPredicate()
    source_predicates: list[VerifiablePredicate] = [exit_pred]
    profile = _make_profile(predicates=source_predicates)
    registry.register(profile)

    source_predicates.clear()
    source_predicates.append(contrast_pred)

    registered = registry.get("coding")
    assert registered is profile
    assert registered.verifiable_predicates == (exit_pred,)
    assert registered.find_verifiable_predicate("command exits 0") is exit_pred
    assert registered.find_verifiable_predicate("color contrast check") is None


def test_domain_profile_coerces_vague_terms_to_immutable_frozenset() -> None:
    registry = DomainProfileRegistry()
    source_vague_terms = {"easy", "clean"}
    profile = DomainProfile(
        name="coding",
        repo_context_extractor=_SimpleExtractor(),
        verifiable_predicates=(),
        intent_classifier=_SimpleClassifier(),
        vague_terms=source_vague_terms,
        safe_defaults={},
        detector=lambda _cwd: 0.8,
    )
    registry.register(profile)

    source_vague_terms.clear()
    source_vague_terms.add("later")

    registered = registry.get("coding")
    assert registered is profile
    assert registered.vague_terms == frozenset({"easy", "clean"})
    assert "later" not in registered.vague_terms


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


def test_lazy_registry_register_loads_defaults_before_custom_profile() -> None:
    calls: list[str] = []

    def _loader(registry: DomainProfileRegistry) -> None:
        calls.append("loaded")
        registry.register(_make_profile(name="built-in"))

    registry = DomainProfileRegistry(loader=_loader)
    custom = _make_profile(name="custom")

    registry.register(custom)

    assert calls == ["loaded"]
    assert [profile.name for profile in registry.all()] == ["built-in", "custom"]


def test_lazy_registry_respects_replaced_profile_storage_temporarily() -> None:
    calls: list[str] = []

    def _loader(registry: DomainProfileRegistry) -> None:
        calls.append("loaded")
        registry.register(_make_profile(name="built-in"))

    registry = DomainProfileRegistry(loader=_loader)
    original_profiles = registry._profiles  # type: ignore[attr-defined]
    registry._profiles = []  # type: ignore[attr-defined]  # test-only singleton isolation hook

    assert registry.all() == ()
    assert calls == []

    registry._profiles = original_profiles  # type: ignore[attr-defined]
    assert [profile.name for profile in registry.all()] == ["built-in"]
    assert calls == ["loaded"]


def test_lazy_registry_retries_after_loader_failure() -> None:
    calls: list[str] = []

    def _loader(registry: DomainProfileRegistry) -> None:
        calls.append("called")
        if len(calls) == 1:
            raise RuntimeError("transient profile import failure")
        registry.register(_make_profile(name="built-in"))

    registry = DomainProfileRegistry(loader=_loader)

    with pytest.raises(RuntimeError, match="transient"):
        registry.all()

    assert [profile.name for profile in registry.all()] == ["built-in"]
    assert calls == ["called", "called"]


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


def test_registry_detect_best_treats_detector_exception_as_zero_confidence() -> None:
    def _raise_detector(_cwd: Path) -> float:
        raise OSError("unreadable")

    registry = DomainProfileRegistry()
    broken = _make_profile(name="broken", detector=_raise_detector)
    viable = _make_profile(name="viable", confidence=0.6)
    registry.register(broken)
    registry.register(viable)

    assert registry.detect_best(Path("/tmp")) is viable


def test_registry_detect_best_returns_none_when_all_detectors_fail() -> None:
    def _raise_detector(_cwd: Path) -> float:
        raise OSError("unreadable")

    registry = DomainProfileRegistry()
    registry.register(_make_profile(name="broken", detector=_raise_detector))

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


def test_registry_union_predicates_ignores_detector_exceptions() -> None:
    def _raise_detector(_cwd: Path) -> float:
        raise OSError("unreadable")

    registry = DomainProfileRegistry()
    exit_pred = _ExitCodePredicate()
    contrast_pred = _ContrastPredicate()
    registry.register(
        _make_profile(name="broken", predicates=(exit_pred,), detector=_raise_detector)
    )
    registry.register(_make_profile(name="viable", predicates=(contrast_pred,), confidence=0.8))

    predicates = registry.union_predicates(Path("/tmp"), threshold=0.5)

    assert predicates == (contrast_pred,)


def test_importing_contracts_module_does_not_import_builtin_profiles() -> None:
    code = (
        "import sys; "
        "import ouroboros.auto.domain_profile; "
        "assert 'ouroboros.auto.profiles.coding' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_default_registry_contains_coding_after_pr2(tmp_path: Path) -> None:
    # DEFAULT_REGISTRY is a module-level singleton. Querying it should lazily
    # expose built-in default profiles without importing them at contract-module
    # import time.
    from ouroboros.auto.profiles.coding import CODING_PROFILE

    assert isinstance(DEFAULT_REGISTRY, DomainProfileRegistry)
    assert DEFAULT_REGISTRY.get("coding") is CODING_PROFILE
    (tmp_path / "pyproject.toml").write_text("[project]\nname='t'\n")
    assert DEFAULT_REGISTRY.detect_best(tmp_path) is CODING_PROFILE
