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

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "DEFAULT_REGISTRY",
    "DomainProfile",
    "DomainProfileRegistry",
    "IntentClassifier",
    "RepoContextExtractor",
    "VerifiablePredicate",
]


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


@dataclass(frozen=True, slots=True)
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
        Immutable domain-specific defaults keyed by ledger section.  PR-5 will
        introduce a richer ``_DefaultSpec`` shape; ``Any`` is intentional here.
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

    def __init__(self) -> None:
        self._profiles: list[DomainProfile] = []

    def register(self, profile: DomainProfile) -> None:
        """Register *profile*.

        Raises
        ------
        ValueError
            If a profile with the same ``name`` is already registered.
        """
        if any(p.name == profile.name for p in self._profiles):
            raise ValueError(f"A DomainProfile named {profile.name!r} is already registered.")
        self._profiles.append(profile)

    def get(self, name: str) -> DomainProfile | None:
        """Return the profile registered under *name*, or None."""
        for profile in self._profiles:
            if profile.name == name:
                return profile
        return None

    def all(self) -> tuple[DomainProfile, ...]:
        """Return all registered profiles in registration order."""
        return tuple(self._profiles)

    def detect_best(self, cwd: Path) -> DomainProfile | None:
        """Return the highest-confidence profile for *cwd*.

        Confidence is measured by ``profile.detector(cwd)``.  Ties are broken
        by registration order (earlier registration wins).  Returns ``None``
        when no profiles are registered or all detectors return 0.0.
        """
        best_profile: DomainProfile | None = None
        best_confidence: float = 0.0
        for profile in self._profiles:
            confidence = profile.detector(cwd)
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
        seen_codes: set[str] = set()
        result: list[VerifiablePredicate] = []
        for profile in self._profiles:
            if profile.detector(cwd) >= threshold:
                for predicate in profile.verifiable_predicates:
                    if predicate.code not in seen_codes:
                        seen_codes.add(predicate.code)
                        result.append(predicate)
        return tuple(result)


#: Module-level singleton registry.  Built-in profiles are imported below so
#: callers that use ``DEFAULT_REGISTRY`` directly see the default registrations
#: without needing an additional ``ouroboros.auto.profiles`` side-effect import.
DEFAULT_REGISTRY: DomainProfileRegistry = DomainProfileRegistry()

# Import built-ins after ``DEFAULT_REGISTRY`` exists.  The profile modules import
# this module to access the registry and contracts, so keeping this at the end
# avoids an incomplete-initialization cycle while still satisfying the public
# default-registry contract.
import ouroboros.auto.profiles  # noqa: E402,F401
