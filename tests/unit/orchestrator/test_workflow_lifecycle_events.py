"""Durable lifecycle-event tests for the #956 Workflow IR family.

These tests pin the contract introduced by the #1131 / #956 wave-1 PR:
durable workflow lifecycle events flow through the existing EventStore,
replay roundtrips deterministically, and the event family stays
bounded/redacted at the persistence boundary.

Scope guardrails:

* No live ``parallel_executor`` dispatch is exercised.
* No other event family is created or mutated.
* No raw prompt / stdout / stderr / credential is ever persisted; the
  fixture asserts those are rejected at the model boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

from pydantic import ValidationError
import pytest

from ouroboros.events.base import BaseEvent
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
    MAX_WORKFLOW_LIFECYCLE_DATA_BYTES,
    MAX_WORKFLOW_LIFECYCLE_REASON_CODE_LEN,
    MAX_WORKFLOW_LIFECYCLE_REF_BYTES,
    MAX_WORKFLOW_LIFECYCLE_REF_COUNT,
    MAX_WORKFLOW_LIFECYCLE_REF_LEN_CHARS,
    MAX_WORKFLOW_LIFECYCLE_WORKFLOW_ID_LEN,
    WORKFLOW_LIFECYCLE_AGGREGATE_TYPE,
    WORKFLOW_LIFECYCLE_EVENT_TYPES,
    WorkflowLifecycleEvent,
    WorkflowLifecycleEventType,
    WorkflowLifecycleProjection,
    WorkflowNodeLifecycleState,
    WorkflowRunLifecycleState,
    project_workflow_lifecycle,
)
from ouroboros.persistence.event_store import EventStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
        spec_id="wfspec_durable",
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


def _t(offset_seconds: int) -> datetime:
    return datetime(2026, 5, 19, tzinfo=UTC) + timedelta(seconds=offset_seconds)


def _completed_run_events(spec: WorkflowSpec) -> tuple[WorkflowLifecycleEvent, ...]:
    return (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            timestamp=_t(0),
            refs=("control://contract/run/completed", "io://journal/run/completed"),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_SCHEDULED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            attempt=1,
            timestamp=_t(1),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_STARTED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            attempt=1,
            timestamp=_t(2),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_COMPLETED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            attempt=1,
            timestamp=_t(3),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.EDGE_TRAVERSED,
            workflow_id=spec.spec_id,
            edge_id="edge_a_b",
            attempt=1,
            timestamp=_t(4),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_STARTED,
            workflow_id=spec.spec_id,
            node_id="node_b",
            attempt=1,
            timestamp=_t(5),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.CHECKPOINT_SAVED,
            workflow_id=spec.spec_id,
            refs=("checkpoint://store/run/1",),
            timestamp=_t(6),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_COMPLETED,
            workflow_id=spec.spec_id,
            node_id="node_b",
            attempt=1,
            timestamp=_t(7),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_COMPLETED,
            workflow_id=spec.spec_id,
            timestamp=_t(8),
        ),
    )


def _failed_run_events(spec: WorkflowSpec) -> tuple[WorkflowLifecycleEvent, ...]:
    return (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            timestamp=_t(0),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_STARTED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            attempt=1,
            timestamp=_t(1),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_FAILED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            attempt=1,
            reason_code="tool_timeout",
            timestamp=_t(2),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_FAILED,
            workflow_id=spec.spec_id,
            reason_code="node_failure",
            timestamp=_t(3),
        ),
    )


def _retried_run_events(spec: WorkflowSpec) -> tuple[WorkflowLifecycleEvent, ...]:
    return (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            timestamp=_t(0),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_STARTED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            attempt=1,
            timestamp=_t(1),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_FAILED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            attempt=1,
            reason_code="tool_timeout",
            timestamp=_t(2),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_RETRIED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            attempt=2,
            reason_code="bounded_retry",
            timestamp=_t(3),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_COMPLETED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            attempt=2,
            timestamp=_t(4),
        ),
    )


def _cancelled_run_events(spec: WorkflowSpec) -> tuple[WorkflowLifecycleEvent, ...]:
    return (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            timestamp=_t(0),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_SCHEDULED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            attempt=1,
            timestamp=_t(1),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CANCELLED,
            workflow_id=spec.spec_id,
            reason_code="user_cancelled",
            timestamp=_t(2),
        ),
    )


# ---------------------------------------------------------------------------
# Family registration / EventStore wiring
# ---------------------------------------------------------------------------


def test_workflow_lifecycle_family_registration_constants() -> None:
    assert WORKFLOW_LIFECYCLE_AGGREGATE_TYPE == "workflow_ir"
    expected = {
        "workflow.run.created",
        "workflow.run.completed",
        "workflow.run.failed",
        "workflow.run.cancelled",
        "workflow.node.scheduled",
        "workflow.node.started",
        "workflow.node.completed",
        "workflow.node.failed",
        "workflow.node.retried",
        "workflow.edge.traversed",
        "workflow.checkpoint.saved",
    }
    assert expected == WORKFLOW_LIFECYCLE_EVENT_TYPES


def test_lifecycle_event_round_trips_through_base_event() -> None:
    spec = _spec()
    event = WorkflowLifecycleEvent(
        event_type=WorkflowLifecycleEventType.NODE_RETRIED,
        workflow_id=spec.spec_id,
        node_id="node_a",
        attempt=2,
        reason_code="bounded_retry",
        refs=("control://contract/run/1",),
        timestamp=_t(3),
        data={"hint": "retry"},
    )
    base = event.to_base_event()
    rehydrated = WorkflowLifecycleEvent.from_base_event(base)
    assert rehydrated == event
    base_payload = base.to_db_dict()
    rehydrated_payload = rehydrated.to_base_event().to_db_dict()
    # Identity ids are random per BaseEvent; compare the durable fields.
    base_payload.pop("id", None)
    rehydrated_payload.pop("id", None)
    assert rehydrated_payload == base_payload


def test_from_base_event_rejects_foreign_family() -> None:
    foreign = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id="sess-1",
        data={"execution_id": "exec-1"},
    )
    with pytest.raises(ValueError, match="not from the workflow lifecycle family"):
        WorkflowLifecycleEvent.from_base_event(foreign)


def test_from_base_event_rejects_unregistered_event_type() -> None:
    rogue = BaseEvent(
        type="workflow.node.unknown",
        aggregate_type=WORKFLOW_LIFECYCLE_AGGREGATE_TYPE,
        aggregate_id="wfspec_durable",
        data={"workflow_id": "wfspec_durable"},
    )
    with pytest.raises(ValueError, match="not a workflow lifecycle event type"):
        WorkflowLifecycleEvent.from_base_event(rogue)


@pytest.mark.asyncio
async def test_event_store_records_and_replays_lifecycle_family() -> None:
    spec = _spec()
    events = _completed_run_events(spec)

    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    try:
        for event in events:
            await store.append_workflow_lifecycle_event(event)

        replayed = await store.replay_workflow_lifecycle(spec.spec_id)
        assert replayed == list(events)

        # Other event families must remain untouched: an unrelated session
        # event coexists without being returned by the workflow lifecycle
        # replay.
        session_event = BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id="sess-x",
            data={"execution_id": "exec-x", "seed_id": "seed-x"},
        )
        await store.append(session_event)
        again = await store.replay_workflow_lifecycle(spec.spec_id)
        assert again == list(events)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_event_store_helper_rejects_non_lifecycle_event() -> None:
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    try:
        with pytest.raises(Exception, match="requires a WorkflowLifecycleEvent"):
            await store.append_workflow_lifecycle_event(object())
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Replay fixture: distinguishes completed / failed / retried / cancelled
# ---------------------------------------------------------------------------


def test_replay_distinguishes_completed_failed_retried_cancelled() -> None:
    spec = _spec()

    completed = project_workflow_lifecycle(spec.spec_id, _completed_run_events(spec))
    failed = project_workflow_lifecycle(spec.spec_id, _failed_run_events(spec))
    retried = project_workflow_lifecycle(spec.spec_id, _retried_run_events(spec))
    cancelled = project_workflow_lifecycle(spec.spec_id, _cancelled_run_events(spec))

    assert completed.run_state is WorkflowRunLifecycleState.COMPLETED
    assert completed.is_terminal()
    assert completed.node_states["node_a"] is WorkflowNodeLifecycleState.COMPLETED
    assert completed.node_states["node_b"] is WorkflowNodeLifecycleState.COMPLETED
    assert tuple(record.edge_id for record in completed.traversed_edges) == ("edge_a_b",)
    assert tuple(record.refs for record in completed.checkpoints) == (
        ("checkpoint://store/run/1",),
    )

    assert failed.run_state is WorkflowRunLifecycleState.FAILED
    assert failed.terminal_reason_code == "node_failure"
    assert failed.node_states["node_a"] is WorkflowNodeLifecycleState.FAILED
    assert failed.is_terminal()

    # Retried run is not terminal yet — the most recent retry attempt
    # completed but the run itself never received a terminal event.
    assert retried.run_state is WorkflowRunLifecycleState.CREATED
    assert not retried.is_terminal()
    assert retried.node_states["node_a"] is WorkflowNodeLifecycleState.COMPLETED
    assert retried.node_attempts["node_a"] == 2

    assert cancelled.run_state is WorkflowRunLifecycleState.CANCELLED
    assert cancelled.terminal_reason_code == "user_cancelled"
    assert cancelled.is_terminal()


# ---------------------------------------------------------------------------
# Deterministic rebuild from the same event slice
# ---------------------------------------------------------------------------


def test_projection_is_deterministic_across_input_orderings() -> None:
    spec = _spec()
    events = _completed_run_events(spec)
    canonical = project_workflow_lifecycle(spec.spec_id, events)

    # Shuffle a few permutations: reversed and event-type-sorted. Both
    # must converge to the same projection because the function sorts
    # by the canonical key. Duplicate inputs are intentionally NOT
    # exercised here — they are a different event slice and would
    # legitimately change ``event_count``.
    permutations: tuple[tuple[WorkflowLifecycleEvent, ...], ...] = (
        tuple(reversed(events)),
        tuple(sorted(events, key=lambda event: event.event_type.value)),
    )
    for permutation in permutations:
        assert project_workflow_lifecycle(spec.spec_id, permutation) == canonical

    # Foreign workflow ids in the same slice are ignored.
    foreign = WorkflowLifecycleEvent(
        event_type=WorkflowLifecycleEventType.RUN_CREATED,
        workflow_id="wfspec_other",
        timestamp=_t(100),
    )
    mixed = events + (foreign,)
    assert project_workflow_lifecycle(spec.spec_id, mixed) == canonical


def test_projection_tie_breaks_same_timestamp_node_attempts() -> None:
    spec = _spec()
    timestamp = _t(1)
    events = (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            timestamp=_t(0),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_STARTED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            attempt=2,
            refs=("control://contract/node-a/attempt-2",),
            timestamp=timestamp,
            data={"hint": "b"},
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_STARTED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            attempt=1,
            refs=("control://contract/node-a/attempt-1",),
            timestamp=timestamp,
            data={"hint": "a"},
        ),
    )

    canonical = project_workflow_lifecycle(spec.spec_id, events)

    assert project_workflow_lifecycle(spec.spec_id, tuple(reversed(events))) == canonical
    assert canonical.node_attempts["node_a"] == 2


def test_projection_tie_breaks_same_timestamp_edge_traversals() -> None:
    spec = _spec()
    timestamp = _t(1)
    events = (
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            timestamp=_t(0),
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.EDGE_TRAVERSED,
            workflow_id=spec.spec_id,
            edge_id="edge_b_end",
            attempt=1,
            refs=("io://journal/edge-b-end",),
            timestamp=timestamp,
        ),
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.EDGE_TRAVERSED,
            workflow_id=spec.spec_id,
            edge_id="edge_a_b",
            attempt=1,
            refs=("io://journal/edge-a-b",),
            timestamp=timestamp,
        ),
    )

    canonical = project_workflow_lifecycle(spec.spec_id, events)

    assert project_workflow_lifecycle(spec.spec_id, tuple(reversed(events))) == canonical
    assert tuple(record.edge_id for record in canonical.traversed_edges) == (
        "edge_a_b",
        "edge_b_end",
    )


@pytest.mark.asyncio
async def test_round_trip_through_event_store_rebuilds_identical_projection() -> None:
    spec = _spec()
    events = _completed_run_events(spec)
    expected = project_workflow_lifecycle(spec.spec_id, events)

    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    try:
        for event in events:
            await store.append_workflow_lifecycle_event(event)
        replayed = await store.replay_workflow_lifecycle(spec.spec_id)
    finally:
        await store.close()

    rebuilt = project_workflow_lifecycle(spec.spec_id, replayed)
    assert rebuilt == expected
    assert isinstance(rebuilt, WorkflowLifecycleProjection)


# ---------------------------------------------------------------------------
# Bounded / redacted payload contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        {"stdout": "hello"},
        {"stderr": "boom"},
        {"prompt": "leak"},
        {"raw_prompt": "leak"},
        {"raw_stdout": "leak"},
        {"api_key": "secret"},
        {"credentials": "secret"},
        {"nested": {"password": "secret"}},
        {"nested": [{"refresh_token": "secret"}]},
    ],
)
def test_lifecycle_event_rejects_raw_prompt_stdout_stderr_and_credentials(
    data: dict[str, object],
) -> None:
    spec = _spec()
    with pytest.raises(ValidationError, match="replay-unsafe key"):
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_COMPLETED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            data=data,
        )


def test_lifecycle_event_rejects_oversized_payload() -> None:
    spec = _spec()
    payload = {"hint": "x" * (MAX_WORKFLOW_LIFECYCLE_DATA_BYTES + 1)}
    with pytest.raises(ValidationError, match="exceeds"):
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_COMPLETED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            data=payload,
        )


@pytest.mark.parametrize(
    "refs",
    [
        tuple(
            f"control://contract/ref-{index}"
            for index in range(MAX_WORKFLOW_LIFECYCLE_REF_COUNT + 1)
        ),
        ("control://contract/" + ("x" * MAX_WORKFLOW_LIFECYCLE_REF_BYTES),),
        tuple(
            "control://contract/" + ("x" * (MAX_WORKFLOW_LIFECYCLE_REF_BYTES - 32)) + str(index)
            for index in range(MAX_WORKFLOW_LIFECYCLE_REF_COUNT)
        ),
    ],
)
def test_lifecycle_event_rejects_oversized_refs(refs: tuple[str, ...]) -> None:
    spec = _spec()
    with pytest.raises(ValidationError, match="refs? exceed"):
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            refs=refs,
        )


@pytest.mark.parametrize(
    "refs",
    [
        ("control://contract/raw-stdout/run-1",),
        ("io://journal/raw_prompt/run-1",),
        ("checkpoint://store/api_key/run-1",),
        ("control://contract/password/value",),
        ("io://journal/bearer-token/run-1",),
    ],
)
def test_lifecycle_event_rejects_unsafe_refs(refs: tuple[str, ...]) -> None:
    spec = _spec()
    with pytest.raises(ValidationError, match="replay-unsafe ref"):
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            refs=refs,
        )


def test_persisted_payload_carries_only_bounded_identifiers() -> None:
    spec = _spec()
    events = _completed_run_events(spec)
    forbidden_substrings = (
        "stdout",
        "stderr",
        "raw_prompt",
        "api_key",
        "credential",
        "password",
        "secret",
        "bearer_token",
    )

    for event in events:
        base = event.to_base_event()
        payload = base.to_db_dict()["payload"]
        serialized = json.dumps(payload, sort_keys=True)
        # The payload must not name or carry any raw stdio/credential
        # leak channel. The redaction is asserted on the persisted
        # representation so a regression in event serialization is caught
        # before it reaches the event store.
        for needle in forbidden_substrings:
            assert needle not in serialized.lower(), (
                f"persisted payload contains forbidden substring {needle!r}: {serialized!r}"
            )
        # The bounded refs payload only carries opaque, replay-safe
        # identifiers (ControlContract / CheckpointStore / IOJournal).
        for ref in payload.get("refs", ()):
            assert "://" in ref
            assert "secret" not in ref.lower()
            assert "password" not in ref.lower()


@pytest.mark.asyncio
async def test_persisted_db_row_payload_contains_no_raw_streams() -> None:
    spec = _spec()
    events = _completed_run_events(spec)
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    try:
        for event in events:
            await store.append_workflow_lifecycle_event(event)
        rows = await store.replay(WORKFLOW_LIFECYCLE_AGGREGATE_TYPE, spec.spec_id)
    finally:
        await store.close()

    for row in rows:
        serialized = json.dumps(row.data, sort_keys=True).lower()
        for needle in ("stdout", "stderr", "raw_prompt", "credential", "password", "api_key"):
            assert needle not in serialized, (
                f"persisted DB row leaks forbidden substring {needle!r}: {serialized!r}"
            )


# ---------------------------------------------------------------------------
# Wave 2 #1142 / #956 — hardened storage-DoS / recursion bounds
# ---------------------------------------------------------------------------


def test_lifecycle_event_rejects_oversized_reason_code() -> None:
    """Reason code over the bound is rejected to prevent storage DoS."""
    spec = _spec()
    oversized = "x" * (MAX_WORKFLOW_LIFECYCLE_REASON_CODE_LEN + 1)
    with pytest.raises(ValidationError, match="reason_code exceeds"):
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_FAILED,
            workflow_id=spec.spec_id,
            reason_code=oversized,
        )


def test_lifecycle_event_rejects_oversized_workflow_id() -> None:
    """Workflow id over the bound is rejected to prevent storage DoS."""
    oversized = "wf_" + ("x" * MAX_WORKFLOW_LIFECYCLE_WORKFLOW_ID_LEN)
    with pytest.raises(ValidationError, match="workflow_id exceeds"):
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=oversized,
        )


def test_lifecycle_event_rejects_too_many_refs_entries() -> None:
    """Ref count above ``MAX_WORKFLOW_LIFECYCLE_REF_COUNT`` is rejected.

    Exercises the pre-existing per-event ref-count ceiling (the operative
    bound is ``MAX_WORKFLOW_LIFECYCLE_REF_COUNT``). The input size is the
    smallest value that trips the limit so a regression that loosens the
    bound by even one slot is caught.
    """
    spec = _spec()
    refs = tuple(
        f"control://contract/ref-{index}"
        for index in range(MAX_WORKFLOW_LIFECYCLE_REF_COUNT + 1)
    )
    with pytest.raises(
        ValidationError,
        match=f"refs exceed {MAX_WORKFLOW_LIFECYCLE_REF_COUNT} entries",
    ):
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            refs=refs,
        )


def test_lifecycle_event_rejects_individual_ref_over_char_bound() -> None:
    """A single oversized ref string is rejected at the validator entry point."""
    spec = _spec()
    refs = ("x" * (MAX_WORKFLOW_LIFECYCLE_REF_LEN_CHARS + 1),)
    with pytest.raises(ValidationError, match="refs exceed"):
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.RUN_CREATED,
            workflow_id=spec.spec_id,
            refs=refs,
        )


def test_lifecycle_event_rejects_deeply_nested_data_no_recursion_error() -> None:
    """A 33-level nested ``data`` mapping is rejected cleanly without RecursionError."""
    spec = _spec()
    # Build a dict nested deeper than _MAX_NESTING_DEPTH (32).
    payload: dict[str, object] = {"leaf": "value"}
    for _ in range(33):
        payload = {"nested": payload}
    with pytest.raises(ValidationError, match="nesting depth"):
        WorkflowLifecycleEvent(
            event_type=WorkflowLifecycleEventType.NODE_COMPLETED,
            workflow_id=spec.spec_id,
            node_id="node_a",
            data=payload,
        )
