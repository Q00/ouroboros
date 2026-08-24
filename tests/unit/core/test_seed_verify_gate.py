"""Seed verify-command gate (RFC verify-gate-windows-portability, Part B1).

Stage one is warn-only: the gate names criteria nothing can deterministically
judge, and lets the run proceed. ``block`` is the same finding set, refused.
"""

from __future__ import annotations

import pytest

from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.core.seed_verify_gate import (
    render_verify_command_gate_warning,
    unverifiable_criteria,
    verify_command_gate_mode,
)


def _seed(*criteria: AcceptanceCriterionSpec | str) -> Seed:
    return Seed(
        goal="ship it",
        acceptance_criteria=criteria,
        ontology_schema=OntologySchema(name="n", description="d"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )


def test_criterion_with_verify_command_is_not_flagged() -> None:
    seed = _seed(AcceptanceCriterionSpec(description="tests pass", verify_command="pytest -q"))

    assert unverifiable_criteria(seed) == ()


def test_explicit_exemption_reason_is_accepted() -> None:
    seed = _seed(
        AcceptanceCriterionSpec(
            description="the copy reads well to a native speaker",
            verify_exemption_reason="Judging prose quality has no deterministic command",
        )
    )

    assert unverifiable_criteria(seed) == ()


def test_bare_string_and_contractless_criteria_are_flagged() -> None:
    seed = _seed(
        "Make the dashboard nicer",
        AcceptanceCriterionSpec(description="tests pass", verify_command="pytest -q"),
        AcceptanceCriterionSpec(description="refactor the module"),
    )

    findings = unverifiable_criteria(seed)

    assert [finding.ac_index for finding in findings] == [0, 2]
    assert [finding.display_index for finding in findings] == [1, 3]


def test_expected_artifacts_alone_do_not_exempt_a_criterion() -> None:
    """A file existing is a different claim from the criterion being satisfied."""
    seed = _seed(
        AcceptanceCriterionSpec(description="write the report", expected_artifacts=("report.md",))
    )

    assert len(unverifiable_criteria(seed)) == 1


def test_warning_names_every_offending_criterion_and_both_escape_hatches() -> None:
    seed = _seed("Make the dashboard nicer", AcceptanceCriterionSpec(description="refactor"))

    warning = render_verify_command_gate_warning(unverifiable_criteria(seed))

    assert "AC 1: Make the dashboard nicer" in warning
    assert "AC 2: refactor" in warning
    assert "verify_command" in warning
    assert "verify_exemption_reason" in warning


def test_no_findings_render_nothing() -> None:
    assert render_verify_command_gate_warning(()) == ""


def test_gate_defaults_to_warn_when_config_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken config must not escalate into refusing to run at all."""
    import ouroboros.config.loader as loader

    def explode() -> None:
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(loader, "load_config", explode)

    assert verify_command_gate_mode() == "warn"


def test_exemption_reason_survives_seed_serialization() -> None:
    spec = AcceptanceCriterionSpec(
        description="the copy reads well",
        verify_exemption_reason="no deterministic command exists",
    )

    value = spec.to_seed_value()

    assert isinstance(value, dict)
    assert value["verify_exemption_reason"] == "no deterministic command exists"
    assert AcceptanceCriterionSpec.model_validate(value) == spec
