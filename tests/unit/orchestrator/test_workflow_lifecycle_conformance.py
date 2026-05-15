from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ouroboros.orchestrator.workflow_ir import (
    EdgeKind,
    NodeKind,
    NodeOwner,
    SourceKind,
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
)
from ouroboros.orchestrator.workflow_lifecycle import (
    WorkflowLifecycleEvent,
    WorkflowLifecycleEventType,
    validate_workflow_lifecycle_conformance,
)


def _task(node_id: str) -> WorkflowNode:
    return WorkflowNode(
        node_id=node_id,
        kind=NodeKind.TASK,
        owner=NodeOwner.AGENT,
        input_schema_ref="schema://input.agent.v1",
        evidence_schema_ref="schema://evidence.agent.v1",
    )


def _spec() -> WorkflowSpec:
    return WorkflowSpec(
        spec_id="wfspec_conformance",
        source=SourceKind.SYNTHETIC,
        nodes=(
            _task("node_a"),
            WorkflowNode(node_id="end", kind=NodeKind.TERMINAL, owner=NodeOwner.HARNESS),
        ),
        edges=(
            WorkflowEdge(
                edge_id="edge_a_end",
                source="node_a",
                target="end",
                kind=EdgeKind.TERMINAL,
            ),
        ),
    )


def test_conformance_accepts_known_lifecycle_history() -> None:
    spec = _spec()
    start = datetime(2026, 5, 15, tzinfo=UTC)
    events = (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            timestamp=start,
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_COMPLETED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            timestamp=start + timedelta(seconds=1),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.EDGE_TRAVERSED,
            workflow_id=spec.spec_id,
            edge_id="edge_a_end",
            timestamp=start + timedelta(seconds=2),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_COMPLETED,
            workflow_id=spec.spec_id,
            timestamp=start + timedelta(seconds=3),
        ),
    )

    report = validate_workflow_lifecycle_conformance(spec, events)

    assert report.workflow_id == spec.spec_id
    assert report.ok is True
    assert report.event_count == 4
    assert report.errors == ()
    assert report.warnings == ()


def test_conformance_rejects_unknown_node_and_edge_ids() -> None:
    spec = _spec()
    events = (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_STARTED,
            workflow_id=spec.spec_id,
            node_id="missing_node",
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.EDGE_TRAVERSED,
            workflow_id=spec.spec_id,
            edge_id="missing_edge",
        ),
    )

    report = validate_workflow_lifecycle_conformance(spec, events)

    assert report.ok is False
    assert [issue.code for issue in report.errors] == ["unknown_node_id", "unknown_edge_id"]


def test_conformance_allows_new_run_created_after_terminal_boundary() -> None:
    spec = _spec()
    start = datetime(2026, 5, 15, tzinfo=UTC)
    events = (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            timestamp=start,
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_COMPLETED,
            workflow_id=spec.spec_id,
            timestamp=start + timedelta(seconds=1),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            timestamp=start + timedelta(seconds=2),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_STARTED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            timestamp=start + timedelta(seconds=3),
        ),
    )

    report = validate_workflow_lifecycle_conformance(spec, events)

    assert report.ok is True
    assert report.errors == ()


def test_conformance_accepts_new_run_created_at_terminal_timestamp() -> None:
    spec = _spec()
    start = datetime(2026, 5, 15, tzinfo=UTC)
    boundary = start + timedelta(seconds=1)
    events = (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            timestamp=start,
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_COMPLETED,
            workflow_id=spec.spec_id,
            timestamp=boundary,
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            timestamp=boundary,
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_STARTED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            timestamp=boundary + timedelta(seconds=1),
        ),
    )

    report = validate_workflow_lifecycle_conformance(spec, events)

    assert report.ok is True
    assert report.errors == ()


def test_conformance_rejects_run_created_before_terminal() -> None:
    spec = _spec()
    start = datetime(2026, 5, 15, tzinfo=UTC)
    events = (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            timestamp=start,
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            timestamp=start + timedelta(seconds=1),
        ),
    )

    report = validate_workflow_lifecycle_conformance(spec, events)

    assert report.ok is False
    assert "run_created_before_terminal" in {issue.code for issue in report.errors}


def test_conformance_rejects_ambiguous_same_timestamp_restart_node_state() -> None:
    spec = _spec()
    start = datetime(2026, 5, 15, tzinfo=UTC)
    boundary = start + timedelta(seconds=1)
    events = (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            timestamp=start,
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_COMPLETED,
            workflow_id=spec.spec_id,
            timestamp=boundary,
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            timestamp=boundary,
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_STARTED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            timestamp=boundary,
        ),
    )

    report = validate_workflow_lifecycle_conformance(spec, events)

    assert report.ok is False
    assert "ambiguous_run_boundary_timestamp" in {issue.code for issue in report.errors}


def test_conformance_flags_events_after_zero_duration_terminal_run() -> None:
    spec = _spec()
    timestamp = datetime(2026, 5, 15, tzinfo=UTC)
    events = (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            timestamp=timestamp,
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_COMPLETED,
            workflow_id=spec.spec_id,
            timestamp=timestamp,
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_STARTED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            timestamp=timestamp + timedelta(seconds=1),
        ),
    )

    report = validate_workflow_lifecycle_conformance(spec, events)

    assert report.ok is False
    assert "event_after_terminal_run" in {issue.code for issue in report.errors}


def test_conformance_still_flags_later_event_after_same_timestamp_terminal_group() -> None:
    spec = _spec()
    start = datetime(2026, 5, 15, tzinfo=UTC)
    boundary = start + timedelta(seconds=1)
    events = (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            timestamp=start,
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_COMPLETED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            timestamp=boundary,
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_COMPLETED,
            workflow_id=spec.spec_id,
            timestamp=boundary,
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.EDGE_TRAVERSED,
            workflow_id=spec.spec_id,
            edge_id="edge_a_end",
            timestamp=boundary + timedelta(seconds=1),
        ),
    )

    report = validate_workflow_lifecycle_conformance(spec, events)

    assert report.ok is False
    assert "event_after_terminal_run" in {issue.code for issue in report.errors}


def test_conformance_flags_events_after_terminal_run() -> None:
    spec = _spec()
    start = datetime(2026, 5, 15, tzinfo=UTC)
    events = (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            timestamp=start,
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_COMPLETED,
            workflow_id=spec.spec_id,
            timestamp=start + timedelta(seconds=1),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_STARTED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            timestamp=start + timedelta(seconds=2),
        ),
    )

    report = validate_workflow_lifecycle_conformance(spec, events)

    assert report.ok is False
    assert [issue.code for issue in report.errors] == ["event_after_terminal_run"]


def test_conformance_warns_when_node_events_precede_run_created() -> None:
    spec = _spec()
    event = WorkflowLifecycleEvent(
        event_type=WorkflowLifecycleEventType.NODE_STARTED,
        workflow_id=spec.spec_id,
        node_id="node_a",
    )

    report = validate_workflow_lifecycle_conformance(spec, (event,))

    assert report.ok is True
    assert [issue.code for issue in report.warnings] == ["lifecycle_before_run_created"]


def test_conformance_ignores_foreign_workflow_events() -> None:
    spec = _spec()
    event = WorkflowLifecycleEvent(
        event_type=WorkflowLifecycleEventType.NODE_STARTED,
        workflow_id="other_workflow",
        node_id="missing_node",
    )

    report = validate_workflow_lifecycle_conformance(spec, (event,))

    assert report.ok is True
    assert report.event_count == 0
    assert report.issues == ()
