"""Positive Workflow IR conformance fixtures.

These fixtures pair a valid :class:`WorkflowSpec` with a legal lifecycle
history and assert two contracts simultaneously:

  1. ``validate_workflow(spec)`` returns ``ok=True`` with no errors.
  2. ``validate_workflow_lifecycle_conformance(spec, events)`` returns
     ``ok=True`` with no errors — the lifecycle rows conform to the graph.

Coverage:

  * legal node-state transitions (scheduled -> started -> completed)
  * terminal-state-emitted-once (exactly one terminal run event per run)
  * blocked vs. failed vs. cancelled vs. timed_out distinction (4 fixtures)

Together with the negative suite, this gives the 5+5 fixture count required
by issue #1131. The tests are offline-deterministic: no network, no model
providers, no subprocess, no plugin dispatch. Refs #1131, #956.
"""

from __future__ import annotations

from collections import Counter

import pytest

from ouroboros.orchestrator.workflow_ir import validate_workflow
from ouroboros.orchestrator.workflow_lifecycle import (
    WorkflowLifecycleEventType,
    validate_workflow_lifecycle_conformance,
)
from tests.conformance.workflow_ir.fixtures import (
    LifecycleFixture,
    all_positive_fixtures,
    positive_blocked_distinction,
    positive_cancelled_distinction,
    positive_failed_distinction,
    positive_legal_transitions,
    positive_terminal_emitted_once,
    positive_timed_out_distinction,
    terminal_run_event_types,
)


def _fixture_id(fixture: LifecycleFixture) -> str:
    return fixture.name


# ---------------------------------------------------------------------------
# Spec-level acceptance: every positive fixture must validate cleanly.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture",
    all_positive_fixtures(),
    ids=lambda f: _fixture_id(f),
)
def test_positive_spec_validates(fixture: LifecycleFixture) -> None:
    result = validate_workflow(fixture.spec)
    assert result.ok, (
        f"{fixture.name}: spec must validate cleanly; got errors="
        f"{tuple(e.code for e in result.errors)!r}"
    )
    assert result.errors == ()


# ---------------------------------------------------------------------------
# Lifecycle-level acceptance: histories must conform to the spec.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture",
    all_positive_fixtures(),
    ids=lambda f: _fixture_id(f),
)
def test_positive_lifecycle_conforms(fixture: LifecycleFixture) -> None:
    report = validate_workflow_lifecycle_conformance(fixture.spec, fixture.events)
    error_codes = tuple(issue.code for issue in report.errors)
    assert report.ok, f"{fixture.name}: lifecycle history must conform; got errors={error_codes!r}"
    assert report.errors == ()


# ---------------------------------------------------------------------------
# Terminal-emitted-once: every positive run history terminates with exactly
# one terminal run event, and that event is the expected discriminator.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture",
    all_positive_fixtures(),
    ids=lambda f: _fixture_id(f),
)
def test_terminal_run_event_emitted_once(fixture: LifecycleFixture) -> None:
    terminals = terminal_run_event_types(fixture.events)
    assert len(terminals) == 1, (
        f"{fixture.name}: expected exactly one terminal run event; got "
        f"{[t.event_type.value for t in terminals]!r}"
    )
    terminal = terminals[0]
    assert terminal.event_type is fixture.expected_run_terminal, (
        f"{fixture.name}: expected terminal type {fixture.expected_run_terminal.value!r}; "
        f"got {terminal.event_type.value!r}"
    )
    if fixture.expected_reason_code is not None:
        assert terminal.reason_code == fixture.expected_reason_code, (
            f"{fixture.name}: expected reason_code "
            f"{fixture.expected_reason_code!r}; got {terminal.reason_code!r}"
        )


# ---------------------------------------------------------------------------
# Legal-transition fixture: scheduled -> started -> completed sequencing.
# ---------------------------------------------------------------------------


def test_legal_transitions_sequence_per_node() -> None:
    """The legal-transitions fixture exercises the full node lifecycle path.

    For each task node we expect the canonical scheduled -> started ->
    completed order, with no failed/retried in between.
    """
    fixture = positive_legal_transitions()
    by_node: dict[str, list[WorkflowLifecycleEventType]] = {}
    for event in fixture.events:
        if event.node_id is None:
            continue
        by_node.setdefault(event.node_id, []).append(event.event_type)
    assert set(by_node) == {"task_agent", "task_verify"}
    expected_sequence = [
        WorkflowLifecycleEventType.NODE_SCHEDULED,
        WorkflowLifecycleEventType.NODE_STARTED,
        WorkflowLifecycleEventType.NODE_COMPLETED,
    ]
    for node_id, sequence in by_node.items():
        assert sequence == expected_sequence, (
            f"node {node_id!r} expected lifecycle {[e.value for e in expected_sequence]!r}; "
            f"got {[e.value for e in sequence]!r}"
        )


# ---------------------------------------------------------------------------
# Blocked / failed / cancelled / timed_out distinction.
# ---------------------------------------------------------------------------


def test_blocked_distinction_has_no_started_node() -> None:
    """Blocked runs never start a node — only RUN_CREATED + RUN_FAILED(blocked)."""
    fixture = positive_blocked_distinction()
    started = [
        event
        for event in fixture.events
        if event.event_type is WorkflowLifecycleEventType.NODE_STARTED
    ]
    assert started == [], (
        "blocked-distinction fixture must not contain NODE_STARTED events; "
        f"got {[e.node_id for e in started]!r}"
    )
    terminals = terminal_run_event_types(fixture.events)
    assert len(terminals) == 1
    assert terminals[0].event_type is WorkflowLifecycleEventType.RUN_FAILED
    assert terminals[0].reason_code == "blocked"


def test_failed_distinction_carries_node_failure() -> None:
    """A failed-mid-execution run includes at least one NODE_FAILED event."""
    fixture = positive_failed_distinction()
    failed_node_events = [
        event
        for event in fixture.events
        if event.event_type is WorkflowLifecycleEventType.NODE_FAILED
    ]
    assert failed_node_events, "failed-distinction fixture must include NODE_FAILED"
    assert all(e.reason_code is not None for e in failed_node_events), (
        "NODE_FAILED events must carry a reason_code per lifecycle schema"
    )


def test_cancelled_distinction_terminates_as_cancelled() -> None:
    """Cancelled runs MUST terminate with RUN_CANCELLED, not RUN_FAILED."""
    fixture = positive_cancelled_distinction()
    terminals = terminal_run_event_types(fixture.events)
    assert len(terminals) == 1
    assert terminals[0].event_type is WorkflowLifecycleEventType.RUN_CANCELLED
    assert terminals[0].reason_code == "user_requested"


def test_timed_out_distinction_disambiguates_via_reason_code() -> None:
    """Timed-out is represented as RUN_FAILED + reason_code='timed_out'.

    The v1 IR intentionally does NOT introduce a new RUN_TIMED_OUT event
    family (out of scope per #956 / #1131). This test pins the encoding so
    a future #946 projection can split the read model without an event-
    family schema change.
    """
    fixture = positive_timed_out_distinction()
    terminals = terminal_run_event_types(fixture.events)
    assert len(terminals) == 1
    assert terminals[0].event_type is WorkflowLifecycleEventType.RUN_FAILED
    assert terminals[0].reason_code == "timed_out"


def test_distinct_terminal_outcomes_cover_four_classes() -> None:
    """The four distinction fixtures must use four distinct (event,reason) pairs.

    Locks the contract that blocked / failed / cancelled / timed_out are
    encoded distinguishably in lifecycle history — there is no collision
    that would prevent a downstream projection from telling them apart.
    """
    fixtures = (
        positive_blocked_distinction(),
        positive_failed_distinction(),
        positive_cancelled_distinction(),
        positive_timed_out_distinction(),
    )
    pairs = []
    for fixture in fixtures:
        terminals = terminal_run_event_types(fixture.events)
        assert len(terminals) == 1, f"{fixture.name}: expected exactly one terminal event"
        pairs.append((terminals[0].event_type, terminals[0].reason_code))
    counts = Counter(pairs)
    duplicates = {pair: count for pair, count in counts.items() if count > 1}
    assert not duplicates, (
        "blocked/failed/cancelled/timed_out fixtures must be distinguishable; "
        f"got duplicate terminal encodings: {duplicates!r}"
    )


def test_terminal_emitted_once_fixture_has_single_terminal() -> None:
    """The dedicated terminal-emitted-once fixture asserts the count rule literally."""
    fixture = positive_terminal_emitted_once()
    terminals = terminal_run_event_types(fixture.events)
    assert len(terminals) == 1
    assert terminals[0].event_type is WorkflowLifecycleEventType.RUN_COMPLETED
