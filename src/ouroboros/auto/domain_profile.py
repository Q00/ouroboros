"""DomainProfile contracts for the Pillar A refactor (#809 P3).

ooo auto today bakes Python/coding assumptions into ``answerer.py`` and
``safe_defaults.py``: vague-term lists, verifiable predicates,
intent classifiers, and safe defaults all assume the user is shipping
code. Pillar A of #809 inverts that: core only knows *that AC must
be verifiable*; *what counts as verifiable* moves into a per-domain
``DomainProfile``.

This module ships the contracts only — no caller is wired here. PR-2
populates the first ``coding`` profile, PR-3 adds CLI activation,
PR-4 routes ``AutoAnswerer`` through ``intent_classifier``, PR-5
migrates ``safe_defaults``, and PR-6 demonstrates plurality with a
second built-in profile.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import importlib
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "DEFAULT_REGISTRY",
    "DomainProfile",
    "DomainProfileRegistry",
    "IntentClassifier",
    "RepoContextExtractor",
    "VerifiablePredicate",
]


def _freeze_safe_default_value(value: Any) -> Any:
    """Recursively freeze common mutable containers used by safe defaults."""
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_safe_default_value(nested) for key, nested in value.items()}
        )
    if isinstance(value, tuple):
        return tuple(_freeze_safe_default_value(item) for item in value)
    if isinstance(value, list):
        return tuple(_freeze_safe_default_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_safe_default_value(item) for item in value)
    return value


def _freeze_safe_defaults(safe_defaults: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(
        {key: _freeze_safe_default_value(value) for key, value in safe_defaults.items()}
    )


@runtime_checkable
class VerifiablePredicate(Protocol):
    """A single verifiable predicate that can match and repair AC text.

    Each predicate has a stable ``code`` identifier (e.g. ``"exit_code"``,
    ``"wcag_contrast"``) that downstream tooling can reference without
    coupling to predicate internals.
    """

    code: str
    """Stable identifier — e.g. ``"exit_code"``, ``"stdout_snapshot"``."""

    def matches(self, criterion: str) -> bool:
        """Return True if *criterion* (an AC string) satisfies this predicate."""
        ...

    def repair_template(self, criterion: str) -> str:
        """Return a repaired AC string for a criterion that failed ``matches``."""
        ...


@runtime_checkable
class IntentClassifier(Protocol):
    """Classifies a raw interview question into a canonical intent label.

    ``classify`` returns a canonical label string (e.g. ``"runtime_context"``,
    ``"acceptance_criteria"``) or ``None`` when no intent matches.
    ``supported_intents`` advertises the full label set so callers can validate
    routing tables without probing every possible question.
    """

    def classify(self, question: str) -> str | None:
        """Return the canonical intent label for *question*, or None."""
        ...

    def supported_intents(self) -> frozenset[str]:
        """Return the full set of intent labels this classifier can emit."""
        ...


@runtime_checkable
class RepoContextExtractor(Protocol):
    """Extracts minimal repository context from a working directory.

    The shape of the returned dict is deliberately open (``dict[str, Any]``)
    so PR-2 can pass through the existing ``AutoAnswerContext``-style dict
    without forcing a schema change here.
    """

    def extract(self, cwd: Path) -> dict[str, Any]:
        """Return a flat dict of repository facts keyed by fact name."""
        ...


@dataclass(frozen=True, slots=True, init=False)
class DomainProfile:
    """An immutable profile that describes how a domain verifies AC.

    ``DomainProfile`` is the central unit of the Pillar A refactor: each
    domain (``coding``, ``design``, ``data-science``, …) ships its own
    instance.  Core logic references only this interface; domain knowledge
    stays out of ``answerer.py`` and ``safe_defaults.py``.

    Attributes
    ----------
    name:
        Unique profile identifier, e.g. ``"coding"``.
    repo_context_extractor:
        Extracts repo facts from a working directory.
    verifiable_predicates:
        Ordered tuple of predicates.  ``find_verifiable_predicate`` scans
        left-to-right and returns the first match.
    intent_classifier:
        Maps interview questions to canonical intent labels.
    vague_terms:
        Frozenset of phrases that are insufficiently precise for this domain
        (e.g. ``"clean"``, ``"easy"`` for coding).
    safe_defaults:
        Domain-specific defaults keyed by ledger section.  PR-5 will
        introduce a richer ``_DefaultSpec`` shape; ``Any`` is intentional here.
        Common mutable containers are recursively frozen at construction time.
    detector:
        A callable ``(cwd: Path) -> float`` returning a confidence in
        [0.0, 1.0] that *cwd* belongs to this domain.
    """

    name: str
    repo_context_extractor: RepoContextExtractor
    verifiable_predicates: tuple[VerifiablePredicate, ...]
    intent_classifier: IntentClassifier
    vague_terms: frozenset[str]
    safe_defaults: Mapping[str, Any]
    detector: Callable[[Path], float]

    def __init__(
        self,
        *,
        name: str,
        repo_context_extractor: RepoContextExtractor,
        verifiable_predicates: Iterable[VerifiablePredicate],
        intent_classifier: IntentClassifier,
        vague_terms: Iterable[str],
        safe_defaults: Mapping[str, Any],
        detector: Callable[[Path], float],
    ) -> None:
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "repo_context_extractor", repo_context_extractor)
        object.__setattr__(self, "verifiable_predicates", tuple(verifiable_predicates))
        object.__setattr__(self, "intent_classifier", intent_classifier)
        object.__setattr__(self, "vague_terms", frozenset(vague_terms))
        object.__setattr__(self, "safe_defaults", _freeze_safe_defaults(safe_defaults))
        object.__setattr__(self, "detector", detector)

    def find_verifiable_predicate(self, criterion: str) -> VerifiablePredicate | None:
        """Return the first predicate whose ``matches`` returns True, or None."""
        for predicate in self.verifiable_predicates:
            if predicate.matches(criterion):
                return predicate
        return None


class DomainProfileRegistry:
    """A mutable registry of ``DomainProfile`` instances.

    Profiles are stored in registration order, which acts as a tie-breaker
    when ``detect_best`` finds equal confidence scores.

    Usage::

        registry = DomainProfileRegistry()
        registry.register(my_coding_profile)
        best = registry.detect_best(Path.cwd())
    """

    def __init__(self, loader: Callable[[DomainProfileRegistry], None] | None = None) -> None:
        self._profiles: list[DomainProfile] = []
        self._profile_storage = self._profiles
        self._loader = loader
        self._loaded = loader is None
        self._loading = False

    def _ensure_loaded(self) -> None:
        """Load built-in profiles on first read without coupling module imports."""
        if self._loaded or self._loading:
            return
        if self._profiles is not self._profile_storage:
            # Tests and callers may intentionally replace the private backing
            # list to isolate the singleton registry.  Treat that replacement
            # as a temporary opt-out; do not mark the loader complete because
            # monkeypatch-style replacement may later restore the original list.
            return
        if self._loader is None:
            self._loaded = True
            return

        self._loading = True
        try:
            self._loader(self)
            self._loaded = True
        finally:
            self._loading = False

    def register(self, profile: DomainProfile) -> None:
        """Register *profile*.

        Raises
        ------
        ValueError
            If a profile with the same ``name`` is already registered.
        """
        self._ensure_loaded()
        if any(p.name == profile.name for p in self._profiles):
            raise ValueError(f"A DomainProfile named {profile.name!r} is already registered.")
        self._profiles.append(profile)

    def get(self, name: str) -> DomainProfile | None:
        """Return the profile registered under *name*, or None."""
        self._ensure_loaded()
        for profile in self._profiles:
            if profile.name == name:
                return profile
        return None

    def all(self) -> tuple[DomainProfile, ...]:
        """Return all registered profiles in registration order."""
        self._ensure_loaded()
        return tuple(self._profiles)

    def detect_best(self, cwd: Path) -> DomainProfile | None:
        """Return the highest-confidence profile for *cwd*.

        Confidence is measured by ``profile.detector(cwd)``.  Ties are broken
        by registration order (earlier registration wins).  Returns ``None``
        when no profiles are registered or all detectors return 0.0.
        """
        self._ensure_loaded()
        best_profile: DomainProfile | None = None
        best_confidence: float = 0.0
        for profile in self._profiles:
            confidence = _detector_confidence(profile, cwd)
            if confidence > best_confidence:
                best_confidence = confidence
                best_profile = profile
        return best_profile

    def union_predicates(
        self, cwd: Path, threshold: float = 0.5
    ) -> tuple[VerifiablePredicate, ...]:
        """Return the union of predicates from all profiles above *threshold*.

        Useful for monorepo projects where multiple domain profiles apply.
        Profiles are evaluated in registration order; predicates from earlier
        profiles appear first in the result.

        Parameters
        ----------
        cwd:
            Working directory passed to each profile's ``detector``.
        threshold:
            Minimum detector confidence required for a profile's predicates
            to be included.  Default is ``0.5``.
        """
        self._ensure_loaded()
        seen_codes: set[str] = set()
        result: list[VerifiablePredicate] = []
        for profile in self._profiles:
            if _detector_confidence(profile, cwd) >= threshold:
                for predicate in profile.verifiable_predicates:
                    if predicate.code not in seen_codes:
                        seen_codes.add(predicate.code)
                        result.append(predicate)
        return tuple(result)


def _detector_confidence(profile: DomainProfile, cwd: Path) -> float:
    try:
        return profile.detector(cwd)
    except Exception:
        return 0.0


def _load_default_profiles(registry: DomainProfileRegistry) -> None:
    """Register built-in profiles lazily.

    ``domain_profile`` is the lightweight contracts module.  Importing it must
    not drag in domain-specific adapters such as ``profiles.coding`` and their
    transitive dependencies.  The default registry still exposes built-ins to
    callers, but only when the registry is actually queried.
    """
    profiles = importlib.import_module("ouroboros.auto.profiles")
    profiles.register_default_profiles(registry)


#: Module-level singleton registry.  Built-in profiles are loaded lazily on
#: first access so importing this contracts module remains dependency-light.
DEFAULT_REGISTRY: DomainProfileRegistry = DomainProfileRegistry(loader=_load_default_profiles)
