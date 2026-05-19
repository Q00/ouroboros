"""Deterministic offline fixture builders for the Workflow IR conformance harness.

Each function in this module returns either:

* a :class:`WorkflowSpec` (and optional lifecycle history) that, by construction,
  exercises **one** named conformance rule from #956, or
* a tuple of fixture metadata used by the plugin-firewall contract test.

The helpers are pure, deterministic, and offline:

* No network, model provider, plugin subprocess, or cloud credential is touched.
* Timestamps are fixed via :data:`FIXTURE_EPOCH` so replays are reproducible.
* No fixture mutates global state; each call returns fresh frozen models.

Negative fixtures intentionally emit **invalid** specs so the validator's
rejection contract can be asserted. Each negative builder returns a spec
built via ``WorkflowSpec.model_construct(...)`` when the invalid shape would
otherwise be caught at the per-model layer (e.g. duplicate node ids cause
no pydantic error on their own — they are caught by ``validate_workflow``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

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
)

FIXTURE_EPOCH: Final[datetime] = datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC)
"""All conformance fixtures anchor lifecycle timestamps to this UTC epoch."""

AGENT_INPUT_SCHEMA: Final[str] = "schema://conformance.agent.input.v1"
AGENT_EVIDENCE_SCHEMA: Final[str] = "schema://conformance.agent.evidence.v1"
VERIFIER_INPUT_SCHEMA: Final[str] = "schema://conformance.verifier.input.v1"
VERIFIER_EVIDENCE_SCHEMA: Final[str] = "schema://conformance.verifier.evidence.v1"


def _ts(offset_seconds: int) -> datetime:
    """Return a deterministic UTC timestamp offset from :data:`FIXTURE_EPOCH`."""
    return FIXTURE_EPOCH + timedelta(seconds=offset_seconds)


# ---------------------------------------------------------------------------
# Shared building blocks
# ---------------------------------------------------------------------------


def agent_task(node_id: str, *, name: str = "agent task") -> WorkflowNode:
    """Return a minimally-valid agent task node."""
    return WorkflowNode(
        node_id=node_id,
        kind=NodeKind.TASK,
        owner=NodeOwner.AGENT,
        name=name,
        input_schema_ref=AGENT_INPUT_SCHEMA,
        evidence_schema_ref=AGENT_EVIDENCE_SCHEMA,
    )


def verifier_task(node_id: str, *, name: str = "verifier task") -> WorkflowNode:
    """Return a minimally-valid verifier task node."""
    return WorkflowNode(
        node_id=node_id,
        kind=NodeKind.TASK,
        owner=NodeOwner.VERIFIER,
        name=name,
        input_schema_ref=VERIFIER_INPUT_SCHEMA,
        evidence_schema_ref=VERIFIER_EVIDENCE_SCHEMA,
    )


def harness_terminal(node_id: str = "terminal") -> WorkflowNode:
    """Return a harness-owned terminal node."""
    return WorkflowNode(
        node_id=node_id,
        kind=NodeKind.TERMINAL,
        owner=NodeOwner.HARNESS,
        name="run complete",
    )


def direct_edge(edge_id: str, source: str, target: str) -> WorkflowEdge:
    """Return a direct edge between two known node ids."""
    return WorkflowEdge(
        edge_id=edge_id,
        source=source,
        target=target,
        kind=EdgeKind.DIRECT,
    )


def terminal_edge(edge_id: str, source: str, target: str) -> WorkflowEdge:
    """Return a terminal edge into the terminal node."""
    return WorkflowEdge(
        edge_id=edge_id,
        source=source,
        target=target,
        kind=EdgeKind.TERMINAL,
    )


def linear_valid_spec() -> WorkflowSpec:
    """Return a small, fully-valid linear spec used as the positive baseline.

    Shape: ``agent_task -> verifier -> terminal``.
    """
    return WorkflowSpec(
        spec_id="wfspec_linear_valid",
        source=SourceKind.SYNTHETIC,
        nodes=(
            agent_task("task_agent"),
            verifier_task("task_verify"),
            harness_terminal("terminal"),
        ),
        edges=(
            direct_edge("edge_agent_verify", "task_agent", "task_verify"),
            terminal_edge("edge_verify_terminal", "task_verify", "terminal"),
        ),
        metadata={"fixture": "linear_valid"},
    )


# ---------------------------------------------------------------------------
# Negative fixtures — each MUST be rejected by validate_workflow with a
# specific error code.
# ---------------------------------------------------------------------------


def dangling_edge_spec() -> WorkflowSpec:
    """An edge whose target is not declared in ``spec.nodes``."""
    return WorkflowSpec(
        spec_id="wfspec_dangling_edge",
        source=SourceKind.SYNTHETIC,
        nodes=(
            agent_task("task_agent"),
            harness_terminal("terminal"),
        ),
        edges=(
            # Target node 'ghost' is intentionally never declared.
            direct_edge("edge_agent_ghost", "task_agent", "ghost"),
            terminal_edge("edge_agent_terminal", "task_agent", "terminal"),
        ),
        metadata={"fixture": "dangling_edge"},
    )


def duplicate_node_id_spec() -> WorkflowSpec:
    """Two nodes share the same ``node_id`` after canonicalization.

    Pydantic does not reject duplicate ids at the model level — they are
    caught by :func:`validate_workflow`. We construct the spec normally
    so the duplicate enters via the nodes tuple.
    """
    return WorkflowSpec(
        spec_id="wfspec_duplicate_node_id",
        source=SourceKind.SYNTHETIC,
        nodes=(
            agent_task("task_agent"),
            # Same id again — duplicate by canonical identifier.
            agent_task("task_agent", name="duplicate agent task"),
            harness_terminal("terminal"),
        ),
        edges=(terminal_edge("edge_agent_terminal", "task_agent", "terminal"),),
        metadata={"fixture": "duplicate_node_id"},
    )


def unreachable_terminal_spec() -> WorkflowSpec:
    """A terminal node that no non-terminal node can reach."""
    return WorkflowSpec(
        spec_id="wfspec_unreachable_terminal",
        source=SourceKind.SYNTHETIC,
        nodes=(
            agent_task("task_agent"),
            verifier_task("task_verify"),
            # Two terminals: one reachable, one isolated.
            harness_terminal("terminal_reachable"),
            harness_terminal("terminal_unreachable"),
        ),
        edges=(
            direct_edge("edge_agent_verify", "task_agent", "task_verify"),
            terminal_edge("edge_verify_reachable", "task_verify", "terminal_reachable"),
            # No edge points at terminal_unreachable — by construction.
        ),
        metadata={"fixture": "unreachable_terminal"},
    )


def missing_schema_ref_spec() -> WorkflowSpec:
    """An agent node missing its required schema refs.

    Per-model validation also rejects this, but the spec-level validator
    re-checks it idempotently so a tampered spec entering via
    ``model_construct`` is still caught. We use ``model_construct`` here
    to bypass the per-model gate and exercise the validator branch.
    """
    tampered_agent = WorkflowNode.model_construct(
        schema_version=1,
        node_id="task_agent",
        kind=NodeKind.TASK,
        owner=NodeOwner.AGENT,
        name="agent missing schemas",
        input_schema_ref=None,
        evidence_schema_ref=None,
        capability_envelope=(),
    )
    return WorkflowSpec.model_construct(
        schema_version=1,
        spec_id="wfspec_missing_schema_ref",
        source=SourceKind.SYNTHETIC,
        source_ref=None,
        nodes=(
            tampered_agent,
            harness_terminal("terminal"),
        ),
        edges=(terminal_edge("edge_agent_terminal", "task_agent", "terminal"),),
        metadata={"fixture": "missing_schema_ref"},
    )


def illegal_transition_spec() -> WorkflowSpec:
    """A self-loop edge — the v1 IR forbids ``source == target`` transitions.

    ``WorkflowEdge`` rejects self-loops at construction; ``model_construct``
    skips that gate so we can prove the spec-level validator catches it too.
    """
    self_loop = WorkflowEdge.model_construct(
        schema_version=1,
        edge_id="edge_self_loop",
        source="task_agent",
        target="task_agent",
        kind=EdgeKind.DIRECT,
        condition=None,
    )
    return WorkflowSpec.model_construct(
        schema_version=1,
        spec_id="wfspec_illegal_transition",
        source=SourceKind.SYNTHETIC,
        source_ref=None,
        nodes=(
            agent_task("task_agent"),
            harness_terminal("terminal"),
        ),
        edges=(
            self_loop,
            terminal_edge("edge_agent_terminal", "task_agent", "terminal"),
        ),
        metadata={"fixture": "illegal_transition"},
    )


# ---------------------------------------------------------------------------
# Positive fixtures — each MUST be accepted by the validator AND lifecycle
# conformance check.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LifecycleFixture:
    """One positive lifecycle fixture: a valid spec + a valid history.

    Attributes:
        name: Stable identifier for the fixture (used in pytest ids).
        spec: A validator-passing :class:`WorkflowSpec`.
        events: A lifecycle history that conforms to ``spec``.
        expected_run_terminal: The expected terminal run event type
            (``RUN_COMPLETED``, ``RUN_FAILED``, ``RUN_CANCELLED``).
        expected_reason_code: Expected ``reason_code`` for non-completed
            terminals (``None`` for ``RUN_COMPLETED``).
    """

    name: str
    spec: WorkflowSpec
    events: tuple[WorkflowLifecycleEvent, ...]
    expected_run_terminal: WorkflowLifecycleEventType
    expected_reason_code: str | None = None


def _evt(
    spec: WorkflowSpec,
    event_type: WorkflowLifecycleEventType,
    offset_seconds: int,
    *,
    node_id: str | None = None,
    edge_id: str | None = None,
    attempt: int | None = None,
    reason_code: str | None = None,
) -> WorkflowLifecycleEvent:
    return WorkflowLifecycleEvent(
        event_type=event_type,
        workflow_id=spec.spec_id,
        node_id=node_id,
        edge_id=edge_id,
        attempt=attempt,
        reason_code=reason_code,
        timestamp=_ts(offset_seconds),
    )


def positive_legal_transitions() -> LifecycleFixture:
    """Legal node-state transitions: scheduled -> started -> completed."""
    spec = linear_valid_spec()
    events = (
        _evt(spec, WorkflowLifecycleEventType.RUN_CREATED, 0),
        _evt(spec, WorkflowLifecycleEventType.NODE_SCHEDULED, 1, node_id="task_agent"),
        _evt(spec, WorkflowLifecycleEventType.NODE_STARTED, 2, node_id="task_agent", attempt=1),
        _evt(spec, WorkflowLifecycleEventType.NODE_COMPLETED, 3, node_id="task_agent", attempt=1),
        _evt(spec, WorkflowLifecycleEventType.EDGE_TRAVERSED, 4, edge_id="edge_agent_verify"),
        _evt(spec, WorkflowLifecycleEventType.NODE_SCHEDULED, 5, node_id="task_verify"),
        _evt(spec, WorkflowLifecycleEventType.NODE_STARTED, 6, node_id="task_verify", attempt=1),
        _evt(spec, WorkflowLifecycleEventType.NODE_COMPLETED, 7, node_id="task_verify", attempt=1),
        _evt(
            spec,
            WorkflowLifecycleEventType.EDGE_TRAVERSED,
            8,
            edge_id="edge_verify_terminal",
        ),
        _evt(spec, WorkflowLifecycleEventType.RUN_COMPLETED, 9),
    )
    return LifecycleFixture(
        name="legal_transitions",
        spec=spec,
        events=events,
        expected_run_terminal=WorkflowLifecycleEventType.RUN_COMPLETED,
    )


def positive_terminal_emitted_once() -> LifecycleFixture:
    """Terminal-state-emitted-once: exactly one terminal run event per run."""
    spec = linear_valid_spec()
    events = (
        _evt(spec, WorkflowLifecycleEventType.RUN_CREATED, 0),
        _evt(spec, WorkflowLifecycleEventType.NODE_STARTED, 1, node_id="task_agent", attempt=1),
        _evt(spec, WorkflowLifecycleEventType.NODE_COMPLETED, 2, node_id="task_agent", attempt=1),
        _evt(spec, WorkflowLifecycleEventType.EDGE_TRAVERSED, 3, edge_id="edge_agent_verify"),
        _evt(spec, WorkflowLifecycleEventType.NODE_STARTED, 4, node_id="task_verify", attempt=1),
        _evt(spec, WorkflowLifecycleEventType.NODE_COMPLETED, 5, node_id="task_verify", attempt=1),
        _evt(spec, WorkflowLifecycleEventType.EDGE_TRAVERSED, 6, edge_id="edge_verify_terminal"),
        _evt(spec, WorkflowLifecycleEventType.RUN_COMPLETED, 7),
    )
    return LifecycleFixture(
        name="terminal_emitted_once",
        spec=spec,
        events=events,
        expected_run_terminal=WorkflowLifecycleEventType.RUN_COMPLETED,
    )


def positive_blocked_distinction() -> LifecycleFixture:
    """Run blocked at the gate — no node ever started, terminates as FAILED.

    ``blocked`` in lifecycle vocabulary is represented as a ``RUN_FAILED``
    with ``reason_code='blocked'`` so #946 projection can distinguish it
    from generic failures without inventing a new event family (out of
    scope per #956 boundary).
    """
    spec = linear_valid_spec()
    events = (
        _evt(spec, WorkflowLifecycleEventType.RUN_CREATED, 0),
        _evt(
            spec,
            WorkflowLifecycleEventType.RUN_FAILED,
            1,
            reason_code="blocked",
        ),
    )
    return LifecycleFixture(
        name="blocked_distinction",
        spec=spec,
        events=events,
        expected_run_terminal=WorkflowLifecycleEventType.RUN_FAILED,
        expected_reason_code="blocked",
    )


def positive_failed_distinction() -> LifecycleFixture:
    """Run failed mid-execution — node failure surfaces as RUN_FAILED."""
    spec = linear_valid_spec()
    events = (
        _evt(spec, WorkflowLifecycleEventType.RUN_CREATED, 0),
        _evt(spec, WorkflowLifecycleEventType.NODE_STARTED, 1, node_id="task_agent", attempt=1),
        _evt(
            spec,
            WorkflowLifecycleEventType.NODE_FAILED,
            2,
            node_id="task_agent",
            attempt=1,
            reason_code="execution_error",
        ),
        _evt(
            spec,
            WorkflowLifecycleEventType.RUN_FAILED,
            3,
            reason_code="node_failure",
        ),
    )
    return LifecycleFixture(
        name="failed_distinction",
        spec=spec,
        events=events,
        expected_run_terminal=WorkflowLifecycleEventType.RUN_FAILED,
        expected_reason_code="node_failure",
    )


def positive_cancelled_distinction() -> LifecycleFixture:
    """Run cancelled by user — terminates as RUN_CANCELLED."""
    spec = linear_valid_spec()
    events = (
        _evt(spec, WorkflowLifecycleEventType.RUN_CREATED, 0),
        _evt(spec, WorkflowLifecycleEventType.NODE_STARTED, 1, node_id="task_agent", attempt=1),
        _evt(
            spec,
            WorkflowLifecycleEventType.RUN_CANCELLED,
            2,
            reason_code="user_requested",
        ),
    )
    return LifecycleFixture(
        name="cancelled_distinction",
        spec=spec,
        events=events,
        expected_run_terminal=WorkflowLifecycleEventType.RUN_CANCELLED,
        expected_reason_code="user_requested",
    )


def positive_timed_out_distinction() -> LifecycleFixture:
    """Run timed out — terminates as RUN_FAILED with reason_code='timed_out'.

    The v1 IR lifecycle does not carry a separate RUN_TIMED_OUT event type;
    timeout is represented by a RUN_FAILED whose ``reason_code`` discriminates
    it from a generic execution failure. This locks the contract so a future
    #946 projection layer can split the read model without an event-family
    schema change.
    """
    spec = linear_valid_spec()
    events = (
        _evt(spec, WorkflowLifecycleEventType.RUN_CREATED, 0),
        _evt(spec, WorkflowLifecycleEventType.NODE_STARTED, 1, node_id="task_agent", attempt=1),
        _evt(
            spec,
            WorkflowLifecycleEventType.NODE_FAILED,
            2,
            node_id="task_agent",
            attempt=1,
            reason_code="timed_out",
        ),
        _evt(
            spec,
            WorkflowLifecycleEventType.RUN_FAILED,
            3,
            reason_code="timed_out",
        ),
    )
    return LifecycleFixture(
        name="timed_out_distinction",
        spec=spec,
        events=events,
        expected_run_terminal=WorkflowLifecycleEventType.RUN_FAILED,
        expected_reason_code="timed_out",
    )


def all_positive_fixtures() -> tuple[LifecycleFixture, ...]:
    """Return every positive lifecycle fixture defined in this module."""
    return (
        positive_legal_transitions(),
        positive_terminal_emitted_once(),
        positive_blocked_distinction(),
        positive_failed_distinction(),
        positive_cancelled_distinction(),
        positive_timed_out_distinction(),
    )


def terminal_run_event_types(
    events: Iterable[WorkflowLifecycleEvent],
) -> tuple[WorkflowLifecycleEvent, ...]:
    """Return events whose type is a terminal run event."""
    terminal_types = {
        WorkflowLifecycleEventType.RUN_COMPLETED,
        WorkflowLifecycleEventType.RUN_FAILED,
        WorkflowLifecycleEventType.RUN_CANCELLED,
    }
    return tuple(event for event in events if event.event_type in terminal_types)


__all__ = [
    "AGENT_EVIDENCE_SCHEMA",
    "AGENT_INPUT_SCHEMA",
    "FIXTURE_EPOCH",
    "LifecycleFixture",
    "VERIFIER_EVIDENCE_SCHEMA",
    "VERIFIER_INPUT_SCHEMA",
    "agent_task",
    "all_positive_fixtures",
    "dangling_edge_spec",
    "direct_edge",
    "duplicate_node_id_spec",
    "harness_terminal",
    "illegal_transition_spec",
    "linear_valid_spec",
    "missing_schema_ref_spec",
    "positive_blocked_distinction",
    "positive_cancelled_distinction",
    "positive_failed_distinction",
    "positive_legal_transitions",
    "positive_terminal_emitted_once",
    "positive_timed_out_distinction",
    "terminal_edge",
    "terminal_run_event_types",
    "unreachable_terminal_spec",
    "verifier_task",
]
