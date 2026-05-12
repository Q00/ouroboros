"""Tests for DomainProfile-aware safe-default finalization (#809 P3, PR 5/6).

Verifies that ``finalize_safe_defaultable_gaps`` consults ``active_profile``
first and falls back to the hardcoded ``_SAFE_DEFAULTS`` dict when the profile
does not supply an override.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ouroboros.auto.domain_profile import DomainProfile
from ouroboros.auto.ledger import LedgerSource, LedgerStatus, SeedDraftLedger
from ouroboros.auto.safe_defaults import (
    _DefaultSpec,
    build_safe_default_synthesis,
    finalize_safe_defaultable_gaps,
)
from ouroboros.auto.state import AutoPipelineState

# ---------------------------------------------------------------------------
# Minimal stubs
# ---------------------------------------------------------------------------


class _StubClassifier:
    def classify(self, question: str) -> str | None:
        return None

    def supported_intents(self) -> frozenset[str]:
        return frozenset()


class _StubExtractor:
    def extract(self, cwd: Path) -> dict[str, Any]:
        return {}


def _make_profile(
    name: str,
    safe_defaults: dict[str, Any],
    confidence: float = 0.9,
) -> DomainProfile:
    return DomainProfile(
        name=name,
        repo_context_extractor=_StubExtractor(),
        verifiable_predicates=(),
        intent_classifier=_StubClassifier(),
        vague_terms=frozenset(),
        safe_defaults=safe_defaults,
        detector=lambda _cwd: confidence,
    )


def _ledger_with_goal_only(goal: str = "Build a local CLI") -> SeedDraftLedger:
    """Ledger with only the goal filled — all other required sections are open gaps."""
    return SeedDraftLedger.from_goal(goal)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_profile_default_overrides_hardcoded() -> None:
    """active_profile.safe_defaults value takes precedence over _SAFE_DEFAULTS."""
    profile = _make_profile(
        name="test-override",
        safe_defaults={
            "actors": _DefaultSpec(
                value="Domain-specific actor override",
                rationale="Domain actor policy",
            )
        },
    )

    ledger = _ledger_with_goal_only()
    finalization = finalize_safe_defaultable_gaps(
        ledger,
        goal="Build a local CLI",
        provenance="test",
        active_profile=profile,
    )

    assert "actors" in finalization.defaulted_sections
    actors_entry = ledger.sections["actors"].entries[-1]
    assert actors_entry.value == "Domain-specific actor override"
    assert actors_entry.source == LedgerSource.ASSUMPTION
    assert actors_entry.status == LedgerStatus.DEFAULTED


def test_synthesis_uses_profile_resolved_default_not_hardcoded() -> None:
    """Transcript synthesis must match the profile-aware ledger default."""
    profile = _make_profile(
        name="story-profile",
        safe_defaults={
            "actors": _DefaultSpec(
                value="Assume the protagonist and narrator are the only story actors.",
                rationale="Narrative domain actor policy",
            )
        },
    )

    ledger = _ledger_with_goal_only("Draft a local short story outline")
    finalization = finalize_safe_defaultable_gaps(
        ledger,
        goal="Draft a local short story outline",
        provenance="test",
        active_profile=profile,
    )

    synthesis = build_safe_default_synthesis(finalization)

    assert "Assume the protagonist and narrator are the only story actors." in synthesis
    assert "Narrative domain actor policy" in synthesis
    assert "Assume the primary actor is the user or automation agent" not in synthesis


def test_missing_key_falls_back_to_hardcoded() -> None:
    """When active_profile.safe_defaults lacks a section, hardcoded dict is used."""
    # Profile only overrides 'actors'; 'inputs' falls through to _SAFE_DEFAULTS.
    profile = _make_profile(
        name="test-partial",
        safe_defaults={
            "actors": _DefaultSpec(
                value="Partial override — actors only",
                rationale="Partial domain policy",
            )
        },
    )

    ledger = _ledger_with_goal_only()
    finalization = finalize_safe_defaultable_gaps(
        ledger,
        goal="Build a local CLI",
        provenance="test",
        active_profile=profile,
    )

    assert "inputs" in finalization.defaulted_sections
    inputs_entry = ledger.sections["inputs"].entries[-1]
    # The hardcoded fallback value references "goal, repository state" — verify it came from
    # the hardcoded dict, not from the profile (which only covers "actors").
    assert "repository state" in inputs_entry.value.lower() or "goal" in inputs_entry.value.lower()


def test_no_profile_uses_hardcoded_dict_unchanged() -> None:
    """Without an active_profile the function behaves exactly as before."""
    ledger = _ledger_with_goal_only()
    finalization = finalize_safe_defaultable_gaps(
        ledger,
        goal="Build a local CLI",
        provenance="test",
        active_profile=None,
    )

    # All defaultable sections should be resolved using the hardcoded dict.
    assert len(finalization.defaulted_sections) > 0
    assert len(finalization.unsafe_gaps) == 0
    actors_entry = ledger.sections["actors"].entries[-1]
    # Hardcoded text references "primary actor"
    assert "primary actor" in actors_entry.value.lower() or "actor" in actors_entry.value.lower()


def test_profile_dict_value_is_coerced_to_defaultspec() -> None:
    """A plain dict in active_profile.safe_defaults is coerced into _DefaultSpec."""
    profile = _make_profile(
        name="test-dict-coerce",
        safe_defaults={
            "outputs": {
                "value": "Smallest observable artifact from dict",
                "rationale": "Dict-form domain policy",
            }
        },
    )

    ledger = _ledger_with_goal_only()
    finalize_safe_defaultable_gaps(
        ledger,
        goal="Build a local CLI",
        provenance="test",
        active_profile=profile,
    )

    outputs_entry = ledger.sections["outputs"].entries[-1]
    assert outputs_entry.value == "Smallest observable artifact from dict"


def test_malformed_profile_dict_default_never_writes_blank_default() -> None:
    """Malformed profile dict defaults are ignored instead of coercing blanks."""
    profile = _make_profile(
        name="test-malformed-dict",
        safe_defaults={
            "actors": {"rationale": "Missing value"},
            "outputs": {"value": "", "rationale": ""},
        },
    )

    ledger = _ledger_with_goal_only()
    finalization = finalize_safe_defaultable_gaps(
        ledger,
        goal="Build a local CLI",
        provenance="test",
        active_profile=profile,
    )

    assert "actors" in finalization.defaulted_sections
    assert "outputs" in finalization.defaulted_sections
    defaulted_entries = [
        entry
        for section in ledger.sections.values()
        for entry in section.entries
        if entry.status == LedgerStatus.DEFAULTED
    ]
    assert defaulted_entries
    assert all(entry.value.strip() for entry in defaulted_entries)
    assert all(entry.rationale.strip() for entry in defaulted_entries)
    actors_entry = ledger.sections["actors"].entries[-1]
    assert "primary actor" in actors_entry.value.lower()


def test_profile_str_value_is_coerced_to_defaultspec() -> None:
    """A plain string in active_profile.safe_defaults is coerced into _DefaultSpec."""
    profile = _make_profile(
        name="test-str-coerce",
        safe_defaults={"constraints": "String form domain constraint"},
    )

    ledger = _ledger_with_goal_only()
    finalize_safe_defaultable_gaps(
        ledger,
        goal="Build a local CLI",
        provenance="test",
        active_profile=profile,
    )

    constraints_entry = ledger.sections["constraints"].entries[-1]
    assert constraints_entry.value == "String form domain constraint"


def test_active_profile_threading_via_state() -> None:
    """AutoPipelineState.active_domain_profile_name persists and round-trips cleanly."""
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp")
    assert state.active_domain_profile_name is None  # default is None

    state.active_domain_profile_name = "coding"
    assert state.active_domain_profile_name == "coding"

    # Serialises to dict without error (JSON round-trip check).
    as_dict = state.to_dict()
    assert as_dict["active_domain_profile_name"] == "coding"
    assert AutoPipelineState.from_dict(as_dict).active_domain_profile_name == "coding"


def test_active_profile_legacy_state_defaults_to_none() -> None:
    """Legacy persisted state without active_domain_profile_name still loads."""
    payload = AutoPipelineState(goal="Build a CLI", cwd="/tmp").to_dict()
    payload.pop("active_domain_profile_name")

    restored = AutoPipelineState.from_dict(payload)

    assert restored.active_domain_profile_name is None
