"""Active Conductor bounded contract tests."""

from __future__ import annotations

import pytest

from ouroboros.core.conductor import (
    ConductorActorMode,
    ConductorDirective,
    validate_conductor_successor_authorization,
)
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.evolution.loop import _conductor_preservation_error


def test_non_relaxing_directive_is_bounded_and_digest_stable() -> None:
    directive = ConductorDirective(
        source_attention_event_id="attention_1",
        instruction="Support the rejected claim with repository evidence.",
        rejected_reasons=("The cited file was not observed.",),
        deterministic=True,
    )

    assert directive.is_non_relaxing is True
    assert directive.digest == ConductorDirective.from_mapping(directive.to_event_data()).digest
    directive.validate_actor_policy(ConductorActorMode.AUTO)


def test_auto_rejects_non_deterministic_or_relaxing_directive() -> None:
    non_deterministic = ConductorDirective(
        source_attention_event_id="attention_1",
        instruction="Try a different implementation approach.",
    )
    relaxing = ConductorDirective(
        source_attention_event_id="attention_1",
        instruction="Replace the acceptance criteria after approval.",
        preserve_acceptance_criteria=False,
        deterministic=True,
        user_approval_event_id="approval_1",
    )

    with pytest.raises(ValueError, match="deterministic non-relaxing"):
        non_deterministic.validate_actor_policy(ConductorActorMode.AUTO)
    with pytest.raises(ValueError, match="deterministic non-relaxing"):
        relaxing.validate_actor_policy(ConductorActorMode.RALPH)
    relaxing.validate_actor_policy(ConductorActorMode.RUN)


def test_specification_change_requires_user_approval() -> None:
    with pytest.raises(ValueError, match="requires user_approval_event_id"):
        ConductorDirective(
            source_attention_event_id="attention_1",
            instruction="Replace one acceptance criterion.",
            preserve_acceptance_criteria=False,
            deterministic=True,
        )


def test_directive_rejects_secret_shaped_content() -> None:
    with pytest.raises(ValueError, match="secret-shaped"):
        ConductorDirective(
            source_attention_event_id="attention_1",
            instruction="Use api_key=super-secret-token-value in the successor.",
            deterministic=True,
        )


def test_successor_authorization_rejects_mismatched_predecessor_or_directive() -> None:
    directive = ConductorDirective(
        source_attention_event_id="attention_1",
        instruction="Correct the rejected evidence without weakening the AC.",
        deterministic=True,
    )
    selected = {
        "engine_ownership_state": "closed",
        "selected_effect": "successor_only",
        "predecessor_execution_id": "exec_predecessor",
        "conductor_directive_digest": directive.digest,
        "actor_mode": "auto",
    }

    assert (
        validate_conductor_successor_authorization(
            selected,
            directive=directive,
            predecessor_execution_id="exec_predecessor",
        )
        is ConductorActorMode.AUTO
    )
    with pytest.raises(ValueError, match="predecessor_execution_id"):
        validate_conductor_successor_authorization(
            selected,
            directive=directive,
            predecessor_execution_id="exec_other",
        )
    changed = ConductorDirective(
        source_attention_event_id="attention_1",
        instruction="Use a different correction.",
        deterministic=True,
    )
    with pytest.raises(ValueError, match="conductor_directive"):
        validate_conductor_successor_authorization(
            selected,
            directive=changed,
            predecessor_execution_id="exec_predecessor",
        )


def test_evolution_preservation_gate_names_every_unauthorized_direction_change() -> None:
    approved = Seed(
        goal="Keep the approved goal",
        constraints=("Keep the compatibility contract",),
        acceptance_criteria=("The behavior remains verified",),
        ontology_schema=OntologySchema(name="Test", description="Test"),
        metadata=SeedMetadata(ambiguity_score=0.1),
    )
    successor = approved.model_copy(
        update={
            "goal": "Changed goal",
            "constraints": ("Changed constraint",),
            "acceptance_criteria": ("Changed criterion",),
        }
    )
    directive = ConductorDirective(
        source_attention_event_id="attention_1",
        instruction="Correct evidence only.",
        deterministic=True,
    )

    error = _conductor_preservation_error(approved, successor, directive)

    assert error is not None
    assert "goal" in error
    assert "acceptance_criteria" in error
    assert "constraints" in error
