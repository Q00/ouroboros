"""Tests for the interview-deadline closure ladder (#1257 PR-B).

The per-phase ``INTERVIEW`` deadline used to terminate as
``interview_phase_deadline`` BLOCKED. PR-B reroutes it through Seed
synthesis (complete ledger → :func:`synthesize_seed_from_ledger`,
incomplete ledger → :func:`partial_seed_from_evidence`) so a partial
product can still surface downstream. These tests pin:

1. ``runtime.deadline.interview.fired`` is appended to the wired
   EventStore with the contract payload.
2. An incomplete ledger reaches the degraded substrate path and the
   persisted Seed metadata advertises the recovery.
3. A complete ledger reaches the normal substrate path and the
   persisted Seed metadata stays at the ``"normal"`` default.
4. The pipeline does not re-mark ``interview_phase_deadline`` BLOCKED
   on the deadline path.

The downstream grade/run gates are deliberately out of scope for PR-B —
this test file asserts *routing*, not terminal outcome. PR-C tightens
the grade-gate side so degraded seeds can reach the partial-product
terminal in PR-D's canonical regression.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from ouroboros.auto.ledger import (
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)
from ouroboros.auto.pipeline import AutoPipeline
from ouroboros.auto.state import (
    AutoPhase,
    AutoPipelineState,
    AutoStore,
)
from ouroboros.events.base import BaseEvent


class _RecordingEventStore:
    """In-memory EventStore stub, modeled on ``test_interview_driver_event_store_wiring``."""

    def __init__(self) -> None:
        self.appended: list[BaseEvent] = []

    async def append(self, event: BaseEvent, **_: Any) -> None:
        self.appended.append(event)


class _DeadlineDriver:
    """Interview driver stub that sleeps past the per-phase deadline.

    Mirrors the ``_pending_emit_tasks`` / ``event_store`` / ``wait_for_pending_emits``
    surface the production driver exposes so the pipeline's deadline
    handler can locate the EventStore via ``self.interview_driver.event_store``.
    """

    progress_callback = None

    def __init__(self, event_store: _RecordingEventStore | None = None) -> None:
        self.event_store = event_store
        self._pending_emit_tasks: set[asyncio.Task[None]] = set()

    async def wait_for_pending_emits(self) -> None:
        return None

    async def run(self, _state, _ledger):  # noqa: ARG002
        await asyncio.sleep(3600)
        raise AssertionError("must be cancelled by phase timeout")


async def _no_seed_generator(_session_id: str):  # pragma: no cover - unused under deadline path
    raise AssertionError("seed generator must not be called on the deadline path")


def _build_deadline_state(tmp_path, *, goal: str = "Build a CLI") -> AutoPipelineState:
    state = AutoPipelineState(goal=goal, cwd=str(tmp_path))
    state.timeout_seconds_by_phase[AutoPhase.INTERVIEW.value] = 1
    # Keep the top-level deadline far in the future so ``_enforce_deadline``
    # does NOT short-circuit and hijack the routing.
    state.deadline_at = time.monotonic() + 3600
    state.deadline_at_epoch = time.time() + 3600
    state.transition(AutoPhase.INTERVIEW, "starting interview")
    return state


@pytest.mark.asyncio
async def test_deadline_emits_runtime_event_and_routes_through_partial_seed(tmp_path) -> None:
    """Incomplete ledger + interview deadline → degraded seed + runtime event."""
    event_store = _RecordingEventStore()
    driver = _DeadlineDriver(event_store=event_store)
    state = _build_deadline_state(tmp_path)
    store = AutoStore(tmp_path)
    pipeline = AutoPipeline(driver, _no_seed_generator, store=store)

    await pipeline.run(state)
    # Give the fire-and-forget event-append task a tick to flush.
    await asyncio.sleep(0)

    # 1. The runtime deadline event must have been appended.
    deadline_events = [
        event for event in event_store.appended if event.type == "runtime.deadline.interview.fired"
    ]
    assert len(deadline_events) == 1, (
        f"expected exactly one runtime.deadline.interview.fired event; "
        f"got {[e.type for e in event_store.appended]}"
    )
    event = deadline_events[0]
    assert event.aggregate_type == "auto_session"
    assert event.aggregate_id == state.auto_session_id
    payload = event.data
    assert payload["auto_session_id"] == state.auto_session_id
    assert payload["phase"] == AutoPhase.INTERVIEW.value
    assert payload["timeout_seconds"] == pytest.approx(1.0)
    assert payload["ledger_ready"] is False
    assert payload["closure_route"] == "partial_seed_from_evidence"
    assert "actors" in payload["open_gaps"], "incomplete ledger must list open gaps"

    # 2. The persisted Seed must come from the degraded substrate.
    assert state.seed_artifact is not None
    metadata = state.seed_artifact["metadata"]
    assert metadata["generation_mode"] == "partial_seed_from_evidence"
    assert metadata["degraded"] is True
    assert metadata["recovery_reason"] == "interview_phase_deadline"
    assert metadata["unresolved_slots"]

    # 3. The pipeline must NOT mark interview_phase_deadline BLOCKED.
    assert state.last_error_code != "interview_phase_deadline"


@pytest.mark.asyncio
async def test_deadline_with_complete_ledger_routes_through_normal_seed(tmp_path) -> None:
    """Complete ledger + interview deadline → normal seed via legacy synthesizer."""
    # Pre-populate every required section so ``ledger.is_seed_ready()`` is True
    # when the deadline fires. The driver still sleeps past the phase timeout
    # so the closure ladder is exercised; the difference is the *route*.
    event_store = _RecordingEventStore()
    state = _build_deadline_state(tmp_path, goal="Build a CLI")
    store = AutoStore(tmp_path)

    # The pipeline builds the ledger from ``state.goal`` on entry. Hydrate
    # every required section from inside the driver — that's the same in-
    # memory ledger the deadline handler will inspect when the per-phase
    # timeout fires.
    class _LedgerSeedingDeadlineDriver(_DeadlineDriver):
        async def run(self, _state, ledger: SeedDraftLedger):  # noqa: ARG002
            # Populate every required section before the deadline fires so the
            # closure ladder routes through the complete-ledger branch.
            for section, value in {
                "actors": "End user.",
                "inputs": "CLI argument.",
                "outputs": "stdout greeting.",
                "constraints": "Pure Python.",
                "non_goals": "Long-running daemon.",
                "acceptance_criteria": "Exit code 0 and prints greeting.",
                "verification_plan": "Run with sample arg; assert stdout/exit.",
                "failure_modes": "Missing argument raises typed error.",
                "runtime_context": "Local developer shell on POSIX.",
            }.items():
                ledger.add_entry(
                    section,
                    LedgerEntry(
                        key=f"{section}.deadline_test",
                        value=value,
                        source=LedgerSource.USER_GOAL,
                        confidence=0.9,
                        status=LedgerStatus.CONFIRMED,
                    ),
                )
            await asyncio.sleep(3600)
            raise AssertionError("must be cancelled by phase timeout")

    driver = _LedgerSeedingDeadlineDriver(event_store=event_store)
    pipeline = AutoPipeline(driver, _no_seed_generator, store=store)

    await pipeline.run(state)
    await asyncio.sleep(0)

    # The runtime event must report the ledger-seed route, not partial-seed.
    deadline_events = [
        event for event in event_store.appended if event.type == "runtime.deadline.interview.fired"
    ]
    assert len(deadline_events) == 1
    payload = deadline_events[0].data
    assert payload["ledger_ready"] is True
    assert payload["closure_route"] == "ledger_seed"
    assert payload["open_gaps"] == []

    # And the persisted Seed metadata stays at the ``"normal"`` default
    # because complete-ledger synthesis does not flip the recovery fields.
    assert state.seed_artifact is not None
    metadata = state.seed_artifact["metadata"]
    assert metadata["generation_mode"] == "normal"
    assert metadata["degraded"] is False
    assert metadata["recovery_reason"] is None


@pytest.mark.asyncio
async def test_deadline_path_marks_interview_completed(tmp_path) -> None:
    """The deadline closure ladder must mark the interview phase done so
    downstream consumers do not interpret the partial product as "interview
    still open"."""
    state = _build_deadline_state(tmp_path)
    driver = _DeadlineDriver()
    pipeline = AutoPipeline(driver, _no_seed_generator, store=AutoStore(tmp_path))

    await pipeline.run(state)

    assert state.interview_completed is True


@pytest.mark.asyncio
async def test_deadline_path_without_event_store_does_not_raise(tmp_path) -> None:
    """Back-compat: drivers wired without an EventStore must still survive the
    deadline path. The recovery routing must not depend on observability."""
    state = _build_deadline_state(tmp_path)
    driver = _DeadlineDriver(event_store=None)
    pipeline = AutoPipeline(driver, _no_seed_generator, store=AutoStore(tmp_path))

    # Must not raise; should still produce a degraded seed artifact.
    await pipeline.run(state)
    assert state.seed_artifact is not None
    assert state.seed_artifact["metadata"]["generation_mode"] == "partial_seed_from_evidence"
