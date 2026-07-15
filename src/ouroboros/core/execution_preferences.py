"""Stable execution-efficiency preferences shared by run and Auto.

The public vocabulary is deliberately small and language-neutral. Hosts may
explain the choice in the user's conversation language, while persisted state
and MCP contracts keep these enum values stable across resume and successor
handoffs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EfficiencyMode(StrEnum):
    """How aggressively a run may use lower-cost execution tiers."""

    ADAPTIVE = "adaptive"
    QUALITY_FIRST = "quality_first"


class FrugalityAssurance(StrEnum):
    """Strength of cost/grounding assurance requested for a run."""

    OFF = "off"
    OBSERVE = "observe"
    STRICT = "strict"


@dataclass(frozen=True, slots=True)
class ResolvedExecutionPreferences:
    """Canonical execution preferences after applying the public defaults."""

    efficiency_mode: EfficiencyMode
    frugality_assurance: FrugalityAssurance
    frugality_assurance_explicit: bool = False

    @property
    def child_model_lowering_enabled(self) -> bool:
        """Whether decomposition may start children below the parent tier."""
        return self.efficiency_mode is EfficiencyMode.ADAPTIVE

    @property
    def frugality_attention_enabled(self) -> bool:
        """Whether frugality verdicts may become user-facing attention."""
        return self.frugality_assurance is not FrugalityAssurance.OFF

    @property
    def strict_baseline_authorized(self) -> bool:
        """Whether the user explicitly authorized potentially costly proof."""
        return (
            self.frugality_assurance is FrugalityAssurance.STRICT
            and self.frugality_assurance_explicit
        )

    def to_contract_data(self) -> dict[str, object]:
        """Serialize the durable, language-neutral execution contract."""
        return {
            "efficiency_mode": self.efficiency_mode.value,
            "frugality_assurance": self.frugality_assurance.value,
            "frugality_assurance_explicit": self.frugality_assurance_explicit,
        }


def resolve_execution_preferences(
    efficiency_mode: str | EfficiencyMode | None,
    frugality_assurance: str | FrugalityAssurance | None,
) -> ResolvedExecutionPreferences:
    """Resolve public values and their safe default coupling.

    Adaptive execution defaults to lightweight observation. Quality-first
    execution disables frugality attention by default. Strict assurance never
    appears implicitly because it may authorize extra-cost baseline work.
    """
    resolved_efficiency = (
        efficiency_mode
        if isinstance(efficiency_mode, EfficiencyMode)
        else EfficiencyMode(efficiency_mode or EfficiencyMode.ADAPTIVE.value)
    )
    assurance_explicit = frugality_assurance is not None
    if isinstance(frugality_assurance, FrugalityAssurance):
        resolved_assurance = frugality_assurance
    elif isinstance(frugality_assurance, str):
        resolved_assurance = FrugalityAssurance(frugality_assurance)
    else:
        resolved_assurance = (
            FrugalityAssurance.OBSERVE
            if resolved_efficiency is EfficiencyMode.ADAPTIVE
            else FrugalityAssurance.OFF
        )
    return ResolvedExecutionPreferences(
        efficiency_mode=resolved_efficiency,
        frugality_assurance=resolved_assurance,
        frugality_assurance_explicit=assurance_explicit,
    )


def execution_preferences_from_contract(
    value: object,
) -> ResolvedExecutionPreferences | None:
    """Parse a persisted preference object, failing closed on malformed data."""
    if not isinstance(value, dict):
        return None
    if set(value) != {
        "efficiency_mode",
        "frugality_assurance",
        "frugality_assurance_explicit",
    }:
        return None
    explicit = value.get("frugality_assurance_explicit")
    if not isinstance(explicit, bool):
        return None
    try:
        resolved = resolve_execution_preferences(
            value.get("efficiency_mode"),
            value.get("frugality_assurance"),
        )
    except (TypeError, ValueError):
        return None
    return ResolvedExecutionPreferences(
        efficiency_mode=resolved.efficiency_mode,
        frugality_assurance=resolved.frugality_assurance,
        frugality_assurance_explicit=explicit,
    )


__all__ = [
    "EfficiencyMode",
    "FrugalityAssurance",
    "ResolvedExecutionPreferences",
    "execution_preferences_from_contract",
    "resolve_execution_preferences",
]
