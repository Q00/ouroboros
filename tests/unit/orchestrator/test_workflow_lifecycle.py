"""Tests for the #956 Workflow IR lifecycle event contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
import pytest

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
    WORKFLOW_LIFECYCLE_SCHEMA_VERSION,
    WorkflowLifecycleEvent,
    WorkflowLifecycleEventType,
    WorkflowNodeLifecycleState,
    completed_node_ids,
    effective_node_states,
    lifecycle_event_for_spec,
    next_runnable_node_ids,
)


def _task(node_id: str) -> WorkflowNode:
    return WorkflowNode(
        node_id=node_id,
        kind=NodeKind.TASK,
        owner=NodeOwner.AGENT,
        input_schema_ref="schema://input.agent.v1",
        evidence_schema_ref="schema://evidence.agent.v1",
    )


def _terminal(node_id: str = "end") -> WorkflowNode:
    return WorkflowNode(node_id=node_id, kind=NodeKind.TERMINAL, owner=NodeOwner.HARNESS)


def _spec() -> WorkflowSpec:
    return WorkflowSpec(
        spec_id="wfspec_lifecycle",
        source=SourceKind.SYNTHETIC,
        nodes=(_task("node_a"), _task("node_b"), _terminal()),
        edges=(
            WorkflowEdge(edge_id="edge_a_b", source="node_a", target="node_b"),
            WorkflowEdge(
                edge_id="edge_b_end",
                source="node_b",
                target="end",
                kind=EdgeKind.TERMINAL,
            ),
        ),
    )


def _conditional_spec() -> WorkflowSpec:
    return WorkflowSpec(
        spec_id="wfspec_conditional",
        source=SourceKind.SYNTHETIC,
        nodes=(
            WorkflowNode(node_id="decide", kind=NodeKind.DECISION, owner=NodeOwner.HARNESS),
            _task("branch_yes"),
            _task("branch_no"),
        ),
        edges=(
            WorkflowEdge(
                edge_id="edge_yes",
                source="decide",
                target="branch_yes",
                kind=EdgeKind.CONDITIONAL,
                condition={"result": "yes"},
            ),
            WorkflowEdge(
                edge_id="edge_no",
                source="decide",
                target="branch_no",
                kind=EdgeKind.CONDITIONAL,
                condition={"result": "no"},
            ),
        ),
    )


def test_run_created_event_anchors_to_workflow_spec_id() -> None:
    timestamp = datetime(2026, 5, 15, tzinfo=UTC)
    event = lifecycle_event_for_spec(
        _spec(),
        WorkflowLifecycleEventType.RUN_CREATED,
        refs=("seed://example",),
        data={"source_ref": "seed_001"},
        timestamp=timestamp,
    )

    base = event.to_base_event()

    assert base.type == "workflow.run.created"
    assert base.timestamp == timestamp
    assert base.aggregate_type == "workflow_ir"
    assert base.aggregate_id == "wfspec_lifecycle"
    assert base.event_version == WORKFLOW_LIFECYCLE_SCHEMA_VERSION
    assert base.data["workflow_id"] == "wfspec_lifecycle"
    assert base.data["refs"] == ["seed://example"]
    assert base.to_db_dict()["payload"]["event_version"] == WORKFLOW_LIFECYCLE_SCHEMA_VERSION


def test_node_lifecycle_requires_node_id_and_preserves_attempt() -> None:
    event = WorkflowLifecycleEvent(
        event_type=WorkflowLifecycleEventType.NODE_STARTED,
        workflow_id="wfspec_lifecycle",
        node_id="node_a",
        attempt=2,
    )

    assert event.to_event_data()["node_id"] == "node_a"
    assert event.to_event_data()["attempt"] == 2

    with pytest.raises(ValidationError, match="requires node_id"):
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_STARTED,
            workflow_id="wfspec_lifecycle",
        )


@pytest.mark.parametrize(
    "event_type",
    [
        WorkflowLifecycleEventType.NODE_FAILED,
        WorkflowLifecycleEventType.NODE_RETRIED,
        WorkflowLifecycleEventType.RUN_FAILED,
        WorkflowLifecycleEventType.RUN_CANCELLED,
    ],
)
def test_failure_and_retry_events_require_reason_code(
    event_type: WorkflowLifecycleEventType,
) -> None:
    kwargs = {"node_id": "node_a"} if event_type.value.startswith("workflow.node") else {}
    with pytest.raises(ValidationError, match="requires reason_code"):
        WorkflowLifecycleEvent(
            event_type=event_type,
            workflow_id="wfspec_lifecycle",
            **kwargs,
        )


def test_edge_traversed_requires_edge_id_and_excludes_node_id() -> None:
    event = WorkflowLifecycleEvent(
        event_type=WorkflowLifecycleEventType.EDGE_TRAVERSED,
        workflow_id="wfspec_lifecycle",
        edge_id="edge_a_b",
    )
    assert event.to_event_data()["edge_id"] == "edge_a_b"

    with pytest.raises(ValidationError, match="requires edge_id"):
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.EDGE_TRAVERSED,
            workflow_id="wfspec_lifecycle",
        )

    with pytest.raises(ValidationError, match="must not carry node_id"):
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.EDGE_TRAVERSED,
            workflow_id="wfspec_lifecycle",
            edge_id="edge_a_b",
            node_id="node_a",
        )


def test_checkpoint_saved_requires_checkpoint_ref() -> None:
    event = WorkflowLifecycleEvent(
        event_type=WorkflowLifecycleEventType.CHECKPOINT_SAVED,
        workflow_id="wfspec_lifecycle",
        refs=("checkpoint://run/1",),
    )
    assert event.refs == ("checkpoint://run/1",)

    with pytest.raises(ValidationError, match="requires at least one checkpoint ref"):
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.CHECKPOINT_SAVED,
            workflow_id="wfspec_lifecycle",
        )


def test_replay_safe_payload_rejects_raw_output_and_secrets() -> None:
    with pytest.raises(ValidationError, match="replay-unsafe key"):
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_COMPLETED,
            workflow_id="wfspec_lifecycle",
            node_id="node_a",
            data={"stdout": "large raw output"},
        )

    with pytest.raises(ValidationError, match="replay-unsafe key"):
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_COMPLETED,
            workflow_id="wfspec_lifecycle",
            node_id="node_a",
            data={"nested": {"api_key": "secret"}},
        )


def test_effective_state_keeps_failed_history_but_allows_retry_success() -> None:
    start = datetime(2026, 5, 15, tzinfo=UTC)
    events = (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_STARTED,
            workflow_id="wfspec_lifecycle",
            node_id="node_a",
            attempt=1,
            timestamp=start,
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_FAILED,
            workflow_id="wfspec_lifecycle",
            node_id="node_a",
            attempt=1,
            reason_code="tool_timeout",
            timestamp=start + timedelta(seconds=1),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_RETRIED,
            workflow_id="wfspec_lifecycle",
            node_id="node_a",
            attempt=2,
            reason_code="bounded_retry",
            timestamp=start + timedelta(seconds=2),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_COMPLETED,
            workflow_id="wfspec_lifecycle",
            node_id="node_a",
            attempt=2,
            timestamp=start + timedelta(seconds=3),
        ),
    )

    states = effective_node_states(events)

    assert [event.event_type for event in events] == [
        WorkflowLifecycleEventType.NODE_STARTED,
        WorkflowLifecycleEventType.NODE_FAILED,
        WorkflowLifecycleEventType.NODE_RETRIED,
        WorkflowLifecycleEventType.NODE_COMPLETED,
    ]
    assert states["node_a"] is WorkflowNodeLifecycleState.COMPLETED
    assert completed_node_ids(events) == frozenset({"node_a"})


def test_next_runnable_nodes_are_pure_projection_from_completed_predecessors() -> None:
    spec = _spec()
    assert next_runnable_node_ids(spec, ()) == ("node_a",)

    events = (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_COMPLETED,
            workflow_id=spec.spec_id,
            node_id="node_a",
        ),
    )

    assert next_runnable_node_ids(spec, events) == ("node_b",)

    all_done = events + (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_COMPLETED,
            workflow_id=spec.spec_id,
            node_id="node_b",
        ),
    )
    assert next_runnable_node_ids(spec, all_done) == ("end",)


def test_retried_node_becomes_runnable_again_after_failure_history() -> None:
    spec = _spec()
    start = datetime(2026, 5, 15, tzinfo=UTC)
    events = (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_STARTED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            attempt=1,
            timestamp=start,
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_FAILED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            attempt=1,
            reason_code="tool_timeout",
            timestamp=start + timedelta(seconds=1),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_RETRIED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            attempt=2,
            reason_code="bounded_retry",
            timestamp=start + timedelta(seconds=2),
        ),
    )

    assert effective_node_states(events)["node_a"] is WorkflowNodeLifecycleState.RETRIED
    assert next_runnable_node_ids(spec, events) == ("node_a",)


def test_next_runnable_nodes_follow_traversed_conditional_edges_only() -> None:
    spec = _conditional_spec()
    completed_decision = (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_COMPLETED,
            workflow_id=spec.spec_id,
            node_id="decide",
        ),
    )

    assert next_runnable_node_ids(spec, completed_decision) == ()

    yes_selected = completed_decision + (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.EDGE_TRAVERSED,
            workflow_id=spec.spec_id,
            edge_id="edge_yes",
        ),
    )

    assert next_runnable_node_ids(spec, yes_selected) == ("branch_yes",)


def test_next_runnable_nodes_ignore_foreign_workflow_events() -> None:
    spec = _spec()
    events = (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_COMPLETED,
            workflow_id="other_workflow",
            node_id="node_a",
        ),
    )

    assert next_runnable_node_ids(spec, events) == ("node_a",)


def test_next_runnable_conditional_edges_ignore_foreign_traversal() -> None:
    spec = _conditional_spec()
    events = (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_COMPLETED,
            workflow_id=spec.spec_id,
            node_id="decide",
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.EDGE_TRAVERSED,
            workflow_id="other_workflow",
            edge_id="edge_yes",
        ),
    )

    assert next_runnable_node_ids(spec, events) == ()


def test_next_runnable_conditional_edges_ignore_prior_attempt_traversal() -> None:
    spec = _conditional_spec()
    start = datetime(2026, 5, 15, tzinfo=UTC)
    events = (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_COMPLETED,
            workflow_id=spec.spec_id,
            node_id="decide",
            attempt=1,
            timestamp=start,
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.EDGE_TRAVERSED,
            workflow_id=spec.spec_id,
            edge_id="edge_yes",
            attempt=1,
            timestamp=start + timedelta(seconds=1),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_RETRIED,
            workflow_id=spec.spec_id,
            node_id="decide",
            attempt=2,
            reason_code="retry_decision",
            timestamp=start + timedelta(seconds=2),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_COMPLETED,
            workflow_id=spec.spec_id,
            node_id="decide",
            attempt=2,
            timestamp=start + timedelta(seconds=3),
        ),
    )

    assert next_runnable_node_ids(spec, events) == ()

    retried_branch_selected = events + (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.EDGE_TRAVERSED,
            workflow_id=spec.spec_id,
            edge_id="edge_no",
            attempt=2,
            timestamp=start + timedelta(seconds=4),
        ),
    )

    assert next_runnable_node_ids(spec, retried_branch_selected) == ("branch_no",)


def test_next_runnable_nodes_stop_after_terminal_run_events() -> None:
    spec = _spec()
    start = datetime(2026, 5, 15, tzinfo=UTC)

    for terminal_event_type in (
        WorkflowLifecycleEventType.RUN_COMPLETED,
        WorkflowLifecycleEventType.RUN_FAILED,
        WorkflowLifecycleEventType.RUN_CANCELLED,
    ):
        events = (
            WorkflowLifecycleEvent(
                event_type=WorkflowLifecycleEventType.RUN_CREATED,
                workflow_id=spec.spec_id,
                timestamp=start,
            ),
            WorkflowLifecycleEvent(
                event_type=terminal_event_type,
                workflow_id=spec.spec_id,
                reason_code="terminal"
                if terminal_event_type is not WorkflowLifecycleEventType.RUN_COMPLETED
                else None,
                timestamp=start + timedelta(seconds=1),
            ),
        )

        assert next_runnable_node_ids(spec, events) == ()


def test_lifecycle_projection_uses_deterministic_tie_breakers() -> None:
    spec = _spec()
    timestamp = datetime(2026, 5, 15, tzinfo=UTC)
    tied_node_events = (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_COMPLETED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            timestamp=timestamp,
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_RETRIED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            attempt=2,
            reason_code="retry",
            timestamp=timestamp,
        ),
    )

    assert effective_node_states(tied_node_events)["node_a"] is WorkflowNodeLifecycleState.COMPLETED
    assert (
        effective_node_states(tuple(reversed(tied_node_events)))["node_a"]
        is WorkflowNodeLifecycleState.COMPLETED
    )

    tied_run_events = (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CANCELLED,
            workflow_id=spec.spec_id,
            reason_code="cancelled",
            timestamp=timestamp,
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            timestamp=timestamp,
        ),
    )

    assert next_runnable_node_ids(spec, tied_run_events) == ()
    assert next_runnable_node_ids(spec, tuple(reversed(tied_run_events))) == ()


def test_next_runnable_allows_new_run_at_terminal_timestamp_after_active_run() -> None:
    spec = _spec()
    timestamp = datetime(2026, 5, 15, tzinfo=UTC)
    events = (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            timestamp=timestamp - timedelta(seconds=1),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_COMPLETED,
            workflow_id=spec.spec_id,
            timestamp=timestamp,
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            timestamp=timestamp,
        ),
    )

    assert next_runnable_node_ids(spec, events) == ("node_a",)
    assert next_runnable_node_ids(spec, tuple(reversed(events))) == ("node_a",)


def test_next_runnable_keeps_same_timestamp_completion_in_closing_run() -> None:
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
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            timestamp=boundary,
        ),
    )

    assert next_runnable_node_ids(spec, events) == ("node_a",)


def test_next_runnable_scopes_node_state_to_latest_run_after_restart() -> None:
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
            event_type=WorkflowLifecycleEventType.RUN_COMPLETED,
            workflow_id=spec.spec_id,
            timestamp=start + timedelta(seconds=2),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            timestamp=start + timedelta(seconds=3),
        ),
    )

    assert next_runnable_node_ids(spec, events) == ("node_a",)


def test_lifecycle_module_does_not_import_runtime_dispatcher() -> None:
    import ouroboros.orchestrator.workflow_lifecycle as lifecycle

    assert "ParallelACExecutor" not in lifecycle.__dict__
    assert "OrchestratorRunner" not in lifecycle.__dict__
