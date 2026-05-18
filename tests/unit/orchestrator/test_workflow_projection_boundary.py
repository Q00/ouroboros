"""Boundary fixture for #956 Workflow IR and #946 projections.

The Workflow IR describes planned work; projection records describe observed
EventStore history. This fixture keeps the two surfaces interoperable through
IDs and source events without embedding one record vocabulary inside the other.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.events.base import BaseEvent
from ouroboros.harness.projection import ArtifactRecord, RunRecord, StageRecord, StepRecord
from ouroboros.harness.projection_builder import build_projection
from ouroboros.orchestrator.workflow_ir import (
    NodeKind,
    WorkflowNode,
    WorkflowSpec,
    validate_workflow,
)
from ouroboros.orchestrator.workflow_ir_adapter import workflow_spec_from_seed


def _seed() -> Seed:
    return Seed(
        goal="Validate the workflow/projection boundary",
        task_type="code",
        constraints=("Keep planning and observation separate",),
        acceptance_criteria=("Projected evidence retains source event IDs",),
        ontology_schema=OntologySchema(
            name="Boundary",
            description="Workflow/projection boundary ontology",
            fields=(
                OntologyField(
                    name="evidence",
                    field_type="object",
                    description="Projected evidence references",
                ),
            ),
        ),
        evaluation_principles=(
            EvaluationPrinciple(
                name="separation",
                description="IR records do not embed projection records",
            ),
        ),
        exit_conditions=(
            ExitCondition(
                name="source_ids_preserved",
                description="Projection records retain event source IDs",
                evaluation_criteria="Step, artifact, and verdict records are source-linked",
            ),
        ),
        metadata=SeedMetadata(seed_id="seed_boundary_001", ambiguity_score=0.1),
    )


def test_workflow_ir_and_projection_remain_separate_read_models() -> None:
    spec = workflow_spec_from_seed(_seed())
    validation = validate_workflow(spec)
    assert validation.ok is True

    task_node = next(node for node in spec.nodes if node.kind is NodeKind.TASK)
    t0 = datetime(2026, 5, 18, tzinfo=UTC)
    events = [
        BaseEvent(
            id="evt_boundary_tool_start",
            type="tool.call.started",
            timestamp=t0,
            aggregate_type="execution",
            aggregate_id="exec_boundary_001",
            data={
                "call_id": task_node.node_id,
                "tool_name": "Bash",
                "workflow_spec_id": spec.spec_id,
                "workflow_node_id": task_node.node_id,
            },
        ),
        BaseEvent(
            id="evt_boundary_tool_return",
            type="tool.call.returned",
            timestamp=t0 + timedelta(seconds=1),
            aggregate_type="execution",
            aggregate_id="exec_boundary_001",
            data={
                "call_id": task_node.node_id,
                "tool_name": "Bash",
                "is_error": False,
                "workflow_spec_id": spec.spec_id,
                "workflow_node_id": task_node.node_id,
            },
        ),
        BaseEvent(
            id="evt_boundary_artifact",
            type="harness.artifact.recorded",
            timestamp=t0 + timedelta(seconds=2),
            aggregate_type="execution",
            aggregate_id="exec_boundary_001",
            data={
                "call_id": task_node.node_id,
                "artifact_id": "artifact_boundary_evidence",
                "kind": "evidence",
            },
        ),
        BaseEvent(
            id="evt_boundary_verdict",
            type="harness.verdict.recorded",
            timestamp=t0 + timedelta(seconds=3),
            aggregate_type="execution",
            aggregate_id="exec_boundary_001",
            data={
                "verdict_id": "verdict_boundary_run",
                "scope": "run",
                "outcome": "pass",
                "evidence_event_ids": ["evt_boundary_artifact"],
                "evidence_artifact_ids": ["artifact_boundary_evidence"],
            },
        ),
    ]

    projection = build_projection(events, seed_id=spec.source_ref or "seed_boundary_001")

    assert projection.run.seed_id == "seed_boundary_001"
    assert projection.steps[0].source_event_ids == (
        "evt_boundary_tool_start",
        "evt_boundary_tool_return",
    )
    assert projection.artifacts[0].metadata["source_event_id"] == "evt_boundary_artifact"
    assert projection.verdicts[0].evidence_event_ids == (
        "evt_boundary_verdict",
        "evt_boundary_artifact",
    )

    assert isinstance(spec, WorkflowSpec)
    assert isinstance(task_node, WorkflowNode)
    assert not isinstance(task_node, StepRecord)
    assert all(
        not isinstance(node, (RunRecord, StageRecord, StepRecord, ArtifactRecord))
        for node in spec.nodes
    )
    assert all("RunRecord" not in node.model_dump_json() for node in spec.nodes)
    assert all("StepRecord" not in node.model_dump_json() for node in spec.nodes)
