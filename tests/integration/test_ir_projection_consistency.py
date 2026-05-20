"""Integration tests for the IR ↔ projection mapping contract.

These tests pin the read-only mapping documented in
``docs/agentos/workflow-ir-projection-mapping.md`` between the #956
Workflow IR and the #946 projection vocabulary. They are deterministic,
offline, and never dispatch work, persist state, or open the network.

Per the locked boundary paragraph in ``docs/agentos/workflow-ir-v1.md``:
the default boundary fixture pairs a validated ``WorkflowSpec`` with
synthetic ``EventStore`` rows to prove source-event linkage, and it must
not add dispatch, cache, persistence, or projection-record embedding to
the IR. No production source file under ``src/`` is modified to make
these tests pass.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ouroboros.events.base import BaseEvent
from ouroboros.harness.projection import StepKind, VerdictOutcome
from ouroboros.harness.projection_builder import (
    ProjectionBuilder,
    _stable_step_id,
)
from ouroboros.orchestrator.workflow_ir import (
    EdgeKind,
    NodeKind,
    NodeOwner,
    SourceKind,
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
    validate_workflow,
)
from ouroboros.orchestrator.workflow_lifecycle import (
    WorkflowLifecycleEvent,
    WorkflowLifecycleEventType,
    validate_workflow_lifecycle_conformance,
)

# ---------------------------------------------------------------------------
# Deterministic fixture helpers (local, offline, no persistence).
# ---------------------------------------------------------------------------


def _fixture_spec() -> WorkflowSpec:
    """Build a small validated WorkflowSpec: fan-out -> two tasks -> terminal.

    The graph deliberately exercises the three identifier roles the
    mapping doc covers:

    * ``plan_node``      — harness fan-out (no projection record by itself,
                            but its lifecycle events anchor the run).
    * ``run_tool_node``  — agent task that projects into ``StepRecord``.
    * ``judge_ac_node``  — verifier task that projects into
                            ``VerdictRecord`` with ``ac_id``.
    * ``done_node``      — terminal that closes the run.
    """

    plan_node = WorkflowNode(
        node_id="plan_node",
        kind=NodeKind.FAN_OUT,
        owner=NodeOwner.HARNESS,
        name="plan",
    )
    run_tool_node = WorkflowNode(
        node_id="run_tool_node",
        kind=NodeKind.TASK,
        owner=NodeOwner.AGENT,
        name="run_tool",
        input_schema_ref="agent.input.v1",
        evidence_schema_ref="agent.evidence.v1",
    )
    judge_ac_node = WorkflowNode(
        node_id="judge_ac_node",
        kind=NodeKind.TASK,
        owner=NodeOwner.VERIFIER,
        name="judge_ac",
        evidence_schema_ref="verifier.verdict.v1",
    )
    done_node = WorkflowNode(
        node_id="done_node",
        kind=NodeKind.TERMINAL,
        owner=NodeOwner.HARNESS,
        name="done",
    )

    edges = (
        WorkflowEdge(
            edge_id="edge_plan_to_tool",
            source="plan_node",
            target="run_tool_node",
            kind=EdgeKind.FAN_OUT,
        ),
        WorkflowEdge(
            edge_id="edge_plan_to_judge",
            source="plan_node",
            target="judge_ac_node",
            kind=EdgeKind.FAN_OUT,
        ),
        WorkflowEdge(
            edge_id="edge_tool_to_done",
            source="run_tool_node",
            target="done_node",
        ),
        WorkflowEdge(
            edge_id="edge_judge_to_done",
            source="judge_ac_node",
            target="done_node",
        ),
    )

    spec = WorkflowSpec(
        spec_id="wfspec_ir_proj_fixture",
        source=SourceKind.SYNTHETIC,
        nodes=(plan_node, run_tool_node, judge_ac_node, done_node),
        edges=edges,
    )

    # Sanity: the fixture must be a valid spec so the mapping is exercised
    # against a graph the IR side would actually accept.
    validation = validate_workflow(spec)
    assert validation.ok, validation.errors
    return spec


def _at(seconds: int) -> datetime:
    """Deterministic UTC timestamp anchored to a fixed instant."""
    return datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=seconds)


def _tool_started(*, call_id: str, tool_name: str, when: datetime) -> BaseEvent:
    return BaseEvent(
        id=f"evt_{call_id}_started",
        type="tool.call.started",
        timestamp=when,
        aggregate_type="execution",
        aggregate_id="exec_ir_proj",
        data={"call_id": call_id, "tool_name": tool_name, "ac_id": call_id},
    )


def _tool_returned(
    *, call_id: str, tool_name: str, when: datetime, is_error: bool = False
) -> BaseEvent:
    return BaseEvent(
        id=f"evt_{call_id}_returned",
        type="tool.call.returned",
        timestamp=when,
        aggregate_type="execution",
        aggregate_id="exec_ir_proj",
        data={
            "call_id": call_id,
            "tool_name": tool_name,
            "is_error": is_error,
            "duration_ms": 7,
            "ac_id": call_id,
        },
    )


def _verdict_event(*, ac_id: str, when: datetime) -> BaseEvent:
    return BaseEvent(
        id=f"evt_{ac_id}_verdict",
        type="harness.verdict.recorded",
        timestamp=when,
        aggregate_type="execution",
        aggregate_id="exec_ir_proj",
        data={
            "scope": "ac",
            "ac_id": ac_id,
            "outcome": "pass",
            "rationale": "fixture verdict",
        },
    )


def _lifecycle(
    spec_id: str,
    event_type: WorkflowLifecycleEventType,
    *,
    when: datetime,
    node_id: str | None = None,
    edge_id: str | None = None,
    reason_code: str | None = None,
    refs: tuple[str, ...] = (),
    attempt: int | None = None,
) -> WorkflowLifecycleEvent:
    return WorkflowLifecycleEvent(
        event_type=event_type,
        workflow_id=spec_id,
        node_id=node_id,
        edge_id=edge_id,
        reason_code=reason_code,
        refs=refs,
        attempt=attempt,
        timestamp=when,
    )


# ---------------------------------------------------------------------------
# Test 1: identifier mapping holds for a fan-out + terminal fixture.
# ---------------------------------------------------------------------------


def test_projection_identifiers_match_workflow_ir_plan() -> None:
    """Synthetic events keyed by node_id project into matching records.

    Locks the rules in
    ``docs/agentos/workflow-ir-projection-mapping.md`` § "Identifier
    mapping":

    * ``WorkflowNode.node_id`` for a task/agent node maps to the projected
      ``StepRecord.step_id`` via the existing ``call_id``-keyed stable id
      derivation.
    * The same node id appears as ``StepRecord.ac_id`` when the event
      carries it.
    * ``WorkflowNode.node_id`` for a verifier node maps to
      ``VerdictRecord.ac_id``.
    * ``WorkflowSpec.spec_id`` is **not** a projection identifier; the
      projection's ``run_id`` is derived from the execution anchor.

    The fixture also confirms that lifecycle conformance (the IR's side
    of the same boundary) accepts the synthetic history.
    """

    spec = _fixture_spec()

    # 1. Synthetic projection events keyed on the IR node ids.
    events: list[BaseEvent] = [
        _tool_started(call_id="run_tool_node", tool_name="Bash", when=_at(10)),
        _tool_returned(call_id="run_tool_node", tool_name="Bash", when=_at(11)),
        _verdict_event(ac_id="judge_ac_node", when=_at(12)),
    ]

    builder = ProjectionBuilder(
        seed_id="seed_ir_proj",
        goal="Verify IR ↔ projection mapping contract",
    )
    result = builder.add_events(events).build()

    # 2. The projection produces exactly one run and one default stage.
    assert result.run.seed_id == "seed_ir_proj"
    assert len(result.stages) == 1
    stage = result.stages[0]
    assert stage.run_id == result.run.run_id
    assert result.run.stage_ids == (stage.stage_id,)

    # 3. One StepRecord whose step_id matches the IR-derived stable id.
    assert len(result.steps) == 1, [step.name for step in result.steps]
    step = result.steps[0]
    expected_step_id = _stable_step_id("execution:exec_ir_proj", "tool", "run_tool_node")
    assert step.step_id == expected_step_id, (
        "WorkflowNode.node_id must derive a deterministic StepRecord.step_id "
        "via the documented call_id mapping"
    )

    # 4. The step preserves the IR node id as ac_id (per the mapping doc
    #    row for AGENT/PLUGIN nodes producing acceptance evidence).
    assert step.ac_id == "run_tool_node"
    assert step.kind is StepKind.SHELL_COMMAND
    # Source-event linkage is the only read-model evidence of node
    # lifecycle; legacy_inferred must stay False on a properly linked
    # projection.
    assert step.legacy_inferred is False
    assert step.source_event_ids == (
        "evt_run_tool_node_started",
        "evt_run_tool_node_returned",
    )

    # 5. One verdict whose ac_id matches the verifier node id.
    assert len(result.verdicts) == 1
    verdict = result.verdicts[0]
    assert verdict.scope == "ac"
    assert verdict.ac_id == "judge_ac_node"
    assert verdict.outcome is VerdictOutcome.PASS
    assert verdict.run_id == result.run.run_id

    # 6. spec_id MUST NOT leak into the projection identity space.
    assert spec.spec_id not in {result.run.run_id, stage.stage_id, step.step_id}
    assert spec.spec_id not in {verdict.verdict_id}

    # 7. The IR side accepts a lifecycle history that mirrors these
    #    projection events, proving the boundary holds in both
    #    directions. The lifecycle records are not embedded into the
    #    projection.
    lifecycle = [
        _lifecycle(
            spec.spec_id,
            WorkflowLifecycleEventType.RUN_CREATED,
            when=_at(9),
        ),
        _lifecycle(
            spec.spec_id,
            WorkflowLifecycleEventType.NODE_STARTED,
            when=_at(10),
            node_id="run_tool_node",
            attempt=1,
        ),
        _lifecycle(
            spec.spec_id,
            WorkflowLifecycleEventType.NODE_COMPLETED,
            when=_at(11),
            node_id="run_tool_node",
            attempt=1,
        ),
        _lifecycle(
            spec.spec_id,
            WorkflowLifecycleEventType.NODE_STARTED,
            when=_at(11),
            node_id="judge_ac_node",
            attempt=1,
        ),
        _lifecycle(
            spec.spec_id,
            WorkflowLifecycleEventType.NODE_COMPLETED,
            when=_at(12),
            node_id="judge_ac_node",
            attempt=1,
        ),
        _lifecycle(
            spec.spec_id,
            WorkflowLifecycleEventType.RUN_COMPLETED,
            when=_at(13),
        ),
    ]
    report = validate_workflow_lifecycle_conformance(spec, lifecycle)
    assert report.ok, report.issues


# ---------------------------------------------------------------------------
# Test 2: negative case — events that reference an unknown node id still
# produce a valid projection, and the mismatch is surfaced by the IR's
# existing conformance helper without introducing a new flag.
# ---------------------------------------------------------------------------


def test_projection_builds_when_events_reference_unknown_node_id() -> None:
    """Mis-linked synthetic events do not crash the projection.

    Locks the negative-path behavior described in
    ``docs/agentos/workflow-ir-projection-mapping.md`` § "Verification":

    * The projection builder is spec-agnostic by design, so an event
      whose ``call_id`` is not a known ``WorkflowNode.node_id`` still
      produces a well-formed ``StepRecord``. No new flag is added to
      either side to represent the mismatch.
    * The mismatch is surfaced via the existing IR-side helper
      ``validate_workflow_lifecycle_conformance``, which emits an
      ``unknown_node_id`` conformance issue when a lifecycle event names
      a node that the spec does not declare.
    * The projected step's ``legacy_inferred`` flag is **not** flipped
      on by this builder for unknown ids — the mapping contract relies on
      the IR-side conformance helper, not on a new projection flag.
    """

    spec = _fixture_spec()

    # Event references a node that is NOT in the spec — the mis-link.
    events: list[BaseEvent] = [
        _tool_started(
            call_id="ghost_node",
            tool_name="Bash",
            when=_at(20),
        ),
        _tool_returned(
            call_id="ghost_node",
            tool_name="Bash",
            when=_at(21),
        ),
    ]

    result = ProjectionBuilder(seed_id="seed_ir_proj_negative").add_events(events).build()

    # The projection still builds.
    assert len(result.steps) == 1
    ghost_step = result.steps[0]
    assert ghost_step.ac_id == "ghost_node"
    assert ghost_step.legacy_inferred is False  # no new flag introduced
    assert ghost_step.source_event_ids == (
        "evt_ghost_node_started",
        "evt_ghost_node_returned",
    )
    # The projection's step_id is derivable, but the IR has no node it
    # corresponds to.
    expected_step_id = _stable_step_id("execution:exec_ir_proj", "tool", "ghost_node")
    assert ghost_step.step_id == expected_step_id
    assert "ghost_node" not in {node.node_id for node in spec.nodes}

    # The mismatch is detected by the IR's existing conformance helper
    # rather than by adding a new projection flag.
    lifecycle = [
        _lifecycle(
            spec.spec_id,
            WorkflowLifecycleEventType.RUN_CREATED,
            when=_at(19),
        ),
        _lifecycle(
            spec.spec_id,
            WorkflowLifecycleEventType.NODE_STARTED,
            when=_at(20),
            node_id="ghost_node",
            attempt=1,
        ),
    ]
    report = validate_workflow_lifecycle_conformance(spec, lifecycle)
    assert not report.ok
    unknown_codes = {issue.code for issue in report.errors}
    assert "unknown_node_id" in unknown_codes, report.issues
