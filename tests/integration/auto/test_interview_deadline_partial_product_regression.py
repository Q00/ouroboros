"""Integration regression for the #1257 interview-deadline closure ladder.

This file is **PR-D**: the canonical regression evidence layer for the
#1257 work. It binds every contract surface PR-A/B/C added to a single
end-to-end assertion so future refactors cannot silently revert any
piece of the closure ladder. The scenarios are deliberately deterministic
and hermetic — no live LLM, no `OUROBOROS_RUN_CANONICAL=1` opt-in — so
they run in plain CI alongside the existing unit suites.

Coverage:

1. **R4-equivalent timeout fixture.** A pre-armed interview-phase
   deadline against a sleeping driver must:
     - NOT terminate as ``interview_phase_deadline`` BLOCKED,
     - persist a degraded ``Seed`` artifact with the PR-A metadata,
     - append ``runtime.deadline.interview.fired`` (PR-B) and
       ``auto.product.partial_emitted`` (PR-C) to the EventStore,
     - surface the PR-C ``partial_product*`` fields on the result envelope.

2. **Partial product fixture.** A successful deadline run reaches
   ``AutoPhase.COMPLETE`` with ``partial_product=True``, the recovery
   reason mirrored from the Seed, and unresolved slots carried forward
   verbatim as next-step hints.

3. **Raw evidence file.** The integration test writes a JSON evidence
   blob to the pytest tmp path so reviewers can audit the full
   seed/product/event trail without re-running the test.

Per the #1257 spec, #1170 closure remains out of scope; this file only
binds the #1257 regression contract.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
from typing import Any

import pytest

from ouroboros.auto.pipeline import AutoPipeline
from ouroboros.auto.state import (
    AutoPhase,
    AutoPipelineState,
    AutoStore,
)
from ouroboros.events.base import BaseEvent


class _RecordingEventStore:
    """In-memory EventStore stub. Mirrors the production append surface."""

    def __init__(self) -> None:
        self.appended: list[BaseEvent] = []

    async def append(self, event: BaseEvent, **_: Any) -> None:
        self.appended.append(event)


class _DeadlineDriver:
    """Interview driver stub that sleeps past the per-phase deadline.

    Exposes the same observability surface the production driver does
    (``event_store``, ``_pending_emit_tasks``, ``wait_for_pending_emits``)
    so the pipeline's deadline handler can locate the EventStore via
    ``self.interview_driver.event_store``.
    """

    progress_callback = None

    def __init__(self, event_store: _RecordingEventStore | None = None) -> None:
        self.event_store = event_store
        self._pending_emit_tasks: set[asyncio.Task[None]] = set()

    async def wait_for_pending_emits(self) -> None:
        return None

    async def run(self, _state, _ledger):  # noqa: ARG002
        await asyncio.sleep(3600)
        raise AssertionError("must be cancelled by per-phase deadline")


async def _unused_seed_generator(_session_id: str):  # pragma: no cover - unused
    raise AssertionError("seed generator must not run on the deadline path")


def _arm_interview_deadline(state: AutoPipelineState, *, phase_seconds: int = 1) -> None:
    """Set up the interview-phase deadline and a far-future top-level deadline.

    The top-level (`pipeline_timeout_seconds`) deadline is kept far in the
    future so `_enforce_deadline` does NOT short-circuit and hijack the
    routing — we want the PER-PHASE deadline to be the trigger.
    """
    state.timeout_seconds_by_phase[AutoPhase.INTERVIEW.value] = phase_seconds
    state.deadline_at = time.monotonic() + 3600
    state.deadline_at_epoch = time.time() + 3600
    state.transition(AutoPhase.INTERVIEW, "starting interview")


def _write_evidence_blob(
    path: Path,
    *,
    state: AutoPipelineState,
    result: Any,
    event_store: _RecordingEventStore,
) -> Path:
    """Persist a JSON evidence blob mirroring the spec's PR-D contract.

    The blob captures everything a reviewer needs to verify the #1257
    regression without re-running the test: the persisted Seed artifact,
    the closure-ladder result envelope, and the full ordered event list.
    """
    blob = {
        "auto_session_id": state.auto_session_id,
        "phase": state.phase.value,
        "last_error_code": state.last_error_code,
        "seed_artifact": state.seed_artifact,
        "result": {
            "status": result.status,
            "phase": result.phase,
            "stop_reason_code": result.stop_reason_code,
            "blocker": result.blocker,
            "partial_product": result.partial_product,
            "partial_product_reason": result.partial_product_reason,
            "partial_unresolved_slots": list(result.partial_unresolved_slots),
        },
        "events": [
            {
                "type": event.type,
                "aggregate_type": event.aggregate_type,
                "aggregate_id": event.aggregate_id,
                "data": dict(event.data),
            }
            for event in event_store.appended
        ],
    }
    path.write_text(json.dumps(blob, indent=2, sort_keys=True), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_r4_equivalent_interview_deadline_regression(tmp_path: Path) -> None:
    """Full regression for #1257: interview deadline → partial product terminal.

    Binds PR-A's substrate, PR-B's routing, and PR-C's terminal into a
    single end-to-end contract assertion. Also writes a raw evidence file
    so future reviewers can audit the closure ladder without re-running
    the test.
    """
    event_store = _RecordingEventStore()
    driver = _DeadlineDriver(event_store=event_store)
    state = AutoPipelineState(goal="Build a tiny CLI tool.", cwd=str(tmp_path))
    _arm_interview_deadline(state)

    pipeline = AutoPipeline(driver, _unused_seed_generator, store=AutoStore(tmp_path))

    result = await pipeline.run(state)
    # Flush fire-and-forget event-append tasks.
    await asyncio.sleep(0)

    # ----- Terminal must NOT be the deleted ``interview_phase_deadline`` BLOCKED.
    assert result.stop_reason_code != "interview_phase_deadline"
    assert state.last_error_code != "interview_phase_deadline"

    # ----- PR-C terminal: the pipeline must reach COMPLETE with partial_product.
    assert state.phase == AutoPhase.COMPLETE
    assert result.status == "complete"
    assert result.blocker is None
    assert result.partial_product is True
    assert result.partial_product_reason == "interview_phase_deadline"
    assert result.partial_unresolved_slots, (
        "unresolved slots must be surfaced verbatim so MCP/CLI consumers "
        "can convert them into next-step hints"
    )

    # ----- PR-A substrate: persisted Seed carries the degraded metadata.
    assert state.seed_artifact is not None
    seed_meta = state.seed_artifact["metadata"]
    assert seed_meta["generation_mode"] == "partial_seed_from_evidence"
    assert seed_meta["degraded"] is True
    assert seed_meta["recovery_reason"] == "interview_phase_deadline"
    assert seed_meta["unresolved_slots"], "seed metadata must list unresolved slots"
    # ``constraints`` mirrors each unresolved slot so the run path cannot
    # silently overrun the deadline-driven recovery contract.
    constraint_blob = " ".join(state.seed_artifact["constraints"])
    for slot in seed_meta["unresolved_slots"]:
        assert slot in constraint_blob, (
            f"unresolved slot {slot} must appear in constraints as a next-step "
            f"requirement; got: {constraint_blob!r}"
        )

    # ----- PR-B routing: runtime.deadline.interview.fired event landed.
    deadline_events = [
        event for event in event_store.appended if event.type == "runtime.deadline.interview.fired"
    ]
    assert len(deadline_events) == 1
    deadline_payload = deadline_events[0].data
    assert deadline_payload["auto_session_id"] == state.auto_session_id
    assert deadline_payload["phase"] == AutoPhase.INTERVIEW.value
    assert deadline_payload["closure_route"] == "partial_seed_from_evidence"
    assert deadline_payload["ledger_ready"] is False
    assert deadline_payload["open_gaps"], "deadline event must list open gaps"

    # ----- PR-C terminal: auto.product.partial_emitted event landed.
    partial_events = [
        event for event in event_store.appended if event.type == "auto.product.partial_emitted"
    ]
    assert len(partial_events) == 1
    partial_payload = partial_events[0].data
    assert partial_payload["auto_session_id"] == state.auto_session_id
    assert partial_payload["seed_id"] == state.seed_id
    assert partial_payload["recovery_reason"] == "interview_phase_deadline"
    assert partial_payload["unresolved_slots"], (
        "partial-emitted payload must list unresolved slots verbatim"
    )

    # ----- Event ordering: deadline event MUST precede partial-emitted event.
    deadline_index = event_store.appended.index(deadline_events[0])
    partial_index = event_store.appended.index(partial_events[0])
    assert deadline_index < partial_index, (
        "runtime.deadline.interview.fired must be emitted before "
        "auto.product.partial_emitted so post-hoc audit can trace the "
        "closure ladder in order"
    )

    # ----- Raw evidence file for reviewer audit (PR-D contract).
    evidence_path = _write_evidence_blob(
        tmp_path / "issue_1257_pr_d_evidence.json",
        state=state,
        result=result,
        event_store=event_store,
    )
    assert evidence_path.exists()
    # Sanity-check the round-trip so the file isn't silently corrupted.
    loaded = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert loaded["result"]["partial_product"] is True
    assert any(event["type"] == "runtime.deadline.interview.fired" for event in loaded["events"])
    assert any(event["type"] == "auto.product.partial_emitted" for event in loaded["events"])


@pytest.mark.asyncio
async def test_interview_deadline_regression_without_event_store(tmp_path: Path) -> None:
    """Back-compat: the closure ladder must succeed even without observability.

    A driver wired without an EventStore is the legacy/test default. The
    #1257 closure ladder must not depend on observability for correctness —
    the partial product terminal still has to fire and the result envelope
    still has to carry the PR-C surface fields.
    """
    driver = _DeadlineDriver(event_store=None)
    state = AutoPipelineState(goal="Build a tiny CLI tool.", cwd=str(tmp_path))
    _arm_interview_deadline(state)
    pipeline = AutoPipeline(driver, _unused_seed_generator, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert state.phase == AutoPhase.COMPLETE
    assert result.partial_product is True
    assert result.partial_product_reason == "interview_phase_deadline"
    assert result.partial_unresolved_slots
    assert state.seed_artifact is not None
    assert state.seed_artifact["metadata"]["degraded"] is True
