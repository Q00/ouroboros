"""Unit tests for the read-only Seed -> Workflow IR adapter."""

from __future__ import annotations

import pytest

from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.orchestrator.workflow_ir import (
    EdgeKind,
    NodeKind,
    NodeOwner,
    SourceKind,
    validate_workflow,
)
from ouroboros.orchestrator.workflow_ir_adapter import (
    DEFAULT_SEED_AC_EVIDENCE_SCHEMA_REF,
    DEFAULT_SEED_AC_INPUT_SCHEMA_REF,
    workflow_spec_from_seed,
)


def _seed(*acceptance_criteria: str) -> Seed:
    return Seed(
        goal="Ship a typed workflow plan",
        task_type="code",
        constraints=("Keep runtime behavior unchanged",),
        acceptance_criteria=acceptance_criteria,
        ontology_schema=OntologySchema(
            name="WorkflowPlan",
            description="Plan ontology",
            fields=(
                OntologyField(
                    name="workflow",
                    field_type="object",
                    description="Workflow graph",
                ),
            ),
        ),
        evaluation_principles=(
            EvaluationPrinciple(
                name="correctness",
                description="All ACs are satisfied",
            ),
        ),
        exit_conditions=(
            ExitCondition(
                name="all_ac_met",
                description="All acceptance criteria pass",
                evaluation_criteria="Every AC reports evidence",
            ),
        ),
        metadata=SeedMetadata(
            seed_id="seed_test_001",
            version="1.0.0",
            ambiguity_score=0.1,
            interview_id="interview_123",
        ),
    )


class TestWorkflowSpecFromSeed:
    def test_projects_current_string_acceptance_criteria_to_valid_workflow_spec(self) -> None:
        spec = workflow_spec_from_seed(
            _seed("First criterion", "Second criterion"),
            profile_ref="profile://default",
        )

        result = validate_workflow(spec)

        assert result.ok is True
        assert spec.source is SourceKind.SEED
        assert spec.source_ref == "seed_test_001"
        assert spec.spec_id == "wfspec_seed_test_001"
        assert spec.metadata["seed_id"] == "seed_test_001"
        assert spec.metadata["interview_id"] == "interview_123"
        assert spec.metadata["profile_ref"] == "profile://default"
        assert len(spec.nodes) == 3
        assert len(spec.edges) == 2

    def test_each_acceptance_criterion_becomes_agent_task_with_schema_refs(self) -> None:
        spec = workflow_spec_from_seed(_seed("  Criterion with padding  "))

        task = spec.nodes[0]
        assert task.node_id == "seed_ac_001"
        assert task.kind is NodeKind.TASK
        assert task.owner is NodeOwner.AGENT
        assert task.input_schema_ref == DEFAULT_SEED_AC_INPUT_SCHEMA_REF
        assert task.evidence_schema_ref == DEFAULT_SEED_AC_EVIDENCE_SCHEMA_REF
        assert task.metadata["acceptance_criterion_index"] == 1
        assert task.metadata["acceptance_criterion"] == "Criterion with padding"
        assert task.metadata["task_type"] == "code"

    def test_terminal_edges_preserve_independent_ac_branches(self) -> None:
        spec = workflow_spec_from_seed(_seed("A", "B", "C"))

        terminal = spec.nodes[-1]
        assert terminal.node_id == "seed_terminal"
        assert terminal.kind is NodeKind.TERMINAL
        assert all(edge.target == "seed_terminal" for edge in spec.edges)
        assert {edge.kind for edge in spec.edges} == {EdgeKind.TERMINAL}
        assert [edge.source for edge in spec.edges] == ["seed_ac_001", "seed_ac_002", "seed_ac_003"]

    def test_custom_schema_refs_and_metadata_are_additive(self) -> None:
        spec = workflow_spec_from_seed(
            _seed("A"),
            input_schema_ref="input://custom",
            evidence_schema_ref="evidence://custom",
            metadata={"source_comment": "#956 boundary decision"},
        )

        assert spec.nodes[0].input_schema_ref == "input://custom"
        assert spec.nodes[0].evidence_schema_ref == "evidence://custom"
        assert spec.metadata["source_comment"] == "#956 boundary decision"
        assert spec.metadata["acceptance_criteria_count"] == 1

    def test_metadata_cannot_override_canonical_seed_anchors(self) -> None:
        spec = workflow_spec_from_seed(_seed("A"), metadata={"seed_id": "tampered"})

        assert spec.metadata["seed_id"] == "seed_test_001"

    def test_rejects_empty_acceptance_criteria(self) -> None:
        with pytest.raises(ValueError, match="at least one acceptance criterion"):
            workflow_spec_from_seed(_seed())

    def test_rejects_blank_acceptance_criteria(self) -> None:
        with pytest.raises(ValueError, match="criterion 2 must be non-blank"):
            workflow_spec_from_seed(_seed("A", "   "))

    def test_does_not_import_projection_records_or_runtime_paths(self) -> None:
        import ouroboros.orchestrator.workflow_ir_adapter as adapter

        imported_names = set(adapter.__dict__)
        assert "RunRecord" not in imported_names
        assert "StepRecord" not in imported_names
        assert "ArtifactRecord" not in imported_names
        assert "OrchestratorRunner" not in imported_names
        assert "ParallelACExecutor" not in imported_names
