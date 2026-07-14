from __future__ import annotations

import pytest

from ouroboros.core.execution_preferences import (
    EfficiencyMode,
    FrugalityAssurance,
    execution_preferences_from_contract,
    resolve_execution_preferences,
)


@pytest.mark.parametrize(
    ("efficiency", "expected_assurance"),
    [
        ("adaptive", FrugalityAssurance.OBSERVE),
        ("quality_first", FrugalityAssurance.OFF),
    ],
)
def test_default_assurance_mapping(
    efficiency: str,
    expected_assurance: FrugalityAssurance,
) -> None:
    resolved = resolve_execution_preferences(efficiency, None)

    assert resolved.efficiency_mode is EfficiencyMode(efficiency)
    assert resolved.frugality_assurance is expected_assurance
    assert resolved.frugality_assurance_explicit is False
    assert resolved.strict_baseline_authorized is False


def test_strict_requires_explicit_value() -> None:
    resolved = resolve_execution_preferences("adaptive", "strict")

    assert resolved.strict_baseline_authorized is True


def test_contract_round_trip_and_malformed_rejection() -> None:
    resolved = resolve_execution_preferences("quality_first", "observe")

    assert execution_preferences_from_contract(resolved.to_contract_data()) == resolved
    assert execution_preferences_from_contract({"efficiency_mode": "adaptive"}) is None
