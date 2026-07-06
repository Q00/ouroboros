"""A2 interview-trace projection over EventStore + auto ledger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import types

import pytest

from ouroboros.auto.ledger import (
    DecisionProvenance,
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)
from ouroboros.auto.pipeline import AutoPipeline
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.auto.trace_export import (
    best_effort_export_trace,
    export_interview_trace,
    export_trace_from_state,
)
from ouroboros.events.base import BaseEvent

_BASE_TS = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)


class _FakeEventStore:
    """Minimal EventStore stand-in exposing ``query_events`` newest-first."""

    def __init__(self, events: list[BaseEvent], *, raise_on: str | None = None) -> None:
        self._events = events
        self._raise_on = raise_on

    async def query_events(
        self,
        aggregate_id: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[BaseEvent]:
        if self._raise_on is not None and aggregate_id == self._raise_on:
            raise RuntimeError("boom")
        rows = [e for e in self._events if aggregate_id is None or e.aggregate_id == aggregate_id]
        # Real store returns newest-first; the projection re-sorts ascending.
        return sorted(rows, key=lambda e: e.timestamp, reverse=True)


def _event(offset: int, event_type: str, aggregate_id: str, data: dict) -> BaseEvent:
    return BaseEvent(
        type=event_type,
        timestamp=_BASE_TS + timedelta(seconds=offset),
        aggregate_type="interview",
        aggregate_id=aggregate_id,
        data=data,
    )


def _populated_ledger() -> SeedDraftLedger:
    ledger = SeedDraftLedger.from_goal("build a widget")
    ledger.record_qa("What runtime?", "python 3.12")
    ledger.record_qa("What output format?", "json")
    # Promoted, evidence-backed decision.
    ledger.add_entry(
        "outputs",
        LedgerEntry(
            key="outputs.format",
            value="json lines",
            source=LedgerSource.USER_PREFERENCE,
            confidence=0.9,
            status=LedgerStatus.CONFIRMED,
        ),
    )
    # Promoted but gated (timeout-defaulted) decision.
    ledger.add_entry(
        "constraints",
        LedgerEntry(
            key="constraints.latency",
            value="best effort",
            source=LedgerSource.CONSERVATIVE_DEFAULT,
            confidence=0.5,
            status=LedgerStatus.DEFAULTED,
            provenance=DecisionProvenance.TIMEOUT_DEFAULT,
        ),
    )
    # Rejected (superseded) decision.
    ledger.add_entry(
        "constraints",
        LedgerEntry(
            key="constraints.old",
            value="stale",
            source=LedgerSource.INFERENCE,
            confidence=0.3,
            status=LedgerStatus.WEAK,
        ),
    )
    return ledger


def _terminal_state(tmp_path: Path) -> AutoPipelineState:
    state = AutoPipelineState(goal="build a widget", cwd=str(tmp_path))
    state.interview_session_id = "int_abc"
    state.phase = AutoPhase.COMPLETE
    state.last_grade = "A"
    state.seed_id = "seed_xyz"
    state.interview_closure_mode = "ledger_only"
    state.last_qa_verdict = "pass"
    state.last_qa_score = 0.95
    state.last_qa_passed = True
    return state


def _events_for(state: AutoPipelineState) -> list[BaseEvent]:
    aid = state.auto_session_id
    iid = state.interview_session_id or ""
    return [
        _event(
            0,
            "interview.response.recorded",
            iid,
            {
                "round_number": 1,
                "question_preview": "What runtime?",
                "response_preview": "python 3.12",
            },
        ),
        _event(
            1,
            "interview.lateral_review.recommended",
            iid,
            {"ambiguity_score": 0.6, "round_number": 1, "from_milestone": "a", "to_milestone": "b"},
        ),
        _event(
            2,
            "interview.lateral_review.recommended",
            iid,
            {"ambiguity_score": 0.3, "round_number": 2},
        ),
        _event(
            3,
            "auto.interview.stagnation.lateral_invoked",
            aid,
            {"persona": "contrarian", "directive": "decide the CSV dialect"},
        ),
        _event(
            4,
            "auto.interview.backend_start_failed_ledger_fallback",
            aid,
            {"reason": "provider down"},
        ),
    ]


@pytest.mark.asyncio
async def test_export_produces_all_streams(tmp_path: Path) -> None:
    state = _terminal_state(tmp_path)
    ledger = _populated_ledger()
    store = _FakeEventStore(_events_for(state))

    out_dir = await export_trace_from_state(state, ledger, event_store=store)

    assert out_dir == tmp_path / ".ouroboros" / "traces" / state.auto_session_id
    files = {p.name for p in out_dir.iterdir()}
    assert {
        "questions.jsonl",
        "ambiguity.jsonl",
        "lateral.jsonl",
        "decisions.jsonl",
        "flags.jsonl",
        "outcome.json",
        "summary.md",
    } <= files

    # questions: two ledger Q/A lines + one response_event line.
    q_lines = [json.loads(x) for x in (out_dir / "questions.jsonl").read_text().splitlines()]
    assert [ln["type"] for ln in q_lines].count("question") == 2
    assert any(ln["type"] == "response_event" and ln["round"] == 1 for ln in q_lines)

    # ambiguity trajectory ascending in time.
    amb = [json.loads(x) for x in (out_dir / "ambiguity.jsonl").read_text().splitlines()]
    assert [ln["ambiguity_score"] for ln in amb] == [0.6, 0.3]

    # lateral: the stagnation event + the persona directive survive.
    lat = (out_dir / "lateral.jsonl").read_text()
    assert "contrarian" in lat and "decide the CSV dialect" in lat


@pytest.mark.asyncio
async def test_decisions_promoted_rejected_and_gated(tmp_path: Path) -> None:
    state = _terminal_state(tmp_path)
    ledger = _populated_ledger()

    out_dir = await export_trace_from_state(state, ledger, event_store=None)

    decisions = [json.loads(x) for x in (out_dir / "decisions.jsonl").read_text().splitlines()]
    by_key = {d["key"]: d for d in decisions}
    assert by_key["outputs.format"]["promoted"] is True
    assert by_key["outputs.format"]["gated"] is False
    assert by_key["constraints.latency"]["promoted"] is True
    assert by_key["constraints.latency"]["provenance"] == "timeout_default"
    assert by_key["constraints.latency"]["gated"] is True
    assert by_key["constraints.old"]["promoted"] is False


@pytest.mark.asyncio
async def test_outcome_surfaces_histogram_and_gate_findings(tmp_path: Path) -> None:
    state = _terminal_state(tmp_path)
    state.findings = [
        {
            "code": "unverified_provenance",
            "severity": "medium",
            "message": "gated decision not verified",
            "target": "constraints.latency",
            "repair_instruction": "confirm",
        },
        {
            "code": "other",
            "severity": "low",
            "message": "x",
            "target": "",
            "repair_instruction": "",
        },
    ]
    ledger = _populated_ledger()

    out_dir = await export_trace_from_state(state, ledger, event_store=None)
    outcome = json.loads((out_dir / "outcome.json").read_text())

    assert outcome["run_id"] == state.auto_session_id
    assert outcome["grade"] == "A"
    assert outcome["qa"]["verdict"] == "pass"
    assert outcome["provenance_histogram"].get("timeout_default") == 1
    assert len(outcome["unverified_provenance_findings"]) == 1
    assert outcome["counts"]["decisions"] == len(
        (out_dir / "decisions.jsonl").read_text().splitlines()
    )
    # summary.md mentions the histogram + gate finding.
    summary = (out_dir / "summary.md").read_text()
    assert "timeout_default" in summary
    assert "constraints.latency" in summary


@pytest.mark.asyncio
async def test_empty_streams_are_omitted_and_cleared(tmp_path: Path) -> None:
    state = _terminal_state(tmp_path)
    ledger = SeedDraftLedger.from_goal("build a widget")  # no decisions beyond goal echo

    out_dir = await export_trace_from_state(state, ledger, event_store=None)
    # No events → no ambiguity/lateral files.
    assert not (out_dir / "ambiguity.jsonl").exists()
    assert not (out_dir / "lateral.jsonl").exists()

    # Re-export with a stale ambiguity file present → it is cleared.
    (out_dir / "ambiguity.jsonl").write_text("stale\n")
    await export_trace_from_state(state, ledger, event_store=None)
    assert not (out_dir / "ambiguity.jsonl").exists()


@pytest.mark.asyncio
async def test_reexport_is_byte_idempotent(tmp_path: Path) -> None:
    state = _terminal_state(tmp_path)
    ledger = _populated_ledger()
    store = _FakeEventStore(_events_for(state))

    out_dir = await export_trace_from_state(state, ledger, event_store=store)
    first = {p.name: p.read_bytes() for p in out_dir.iterdir()}
    await export_trace_from_state(state, ledger, event_store=store)
    second = {p.name: p.read_bytes() for p in out_dir.iterdir()}

    assert first == second


@pytest.mark.asyncio
async def test_export_interview_trace_from_store(tmp_path: Path) -> None:
    store_dir = tmp_path / "data"
    auto_store = AutoStore(store_dir)
    state = _terminal_state(tmp_path)
    state.ledger = _populated_ledger().to_dict()
    auto_store.save(state)

    event_store = _FakeEventStore(_events_for(state))
    out_dir = await export_interview_trace(
        state.auto_session_id, auto_store=auto_store, event_store=event_store
    )

    assert out_dir is not None
    decisions = (out_dir / "decisions.jsonl").read_text()
    assert "outputs.format" in decisions


@pytest.mark.asyncio
async def test_gather_swallows_per_aggregate_query_error(tmp_path: Path) -> None:
    state = _terminal_state(tmp_path)
    ledger = _populated_ledger()
    # Raise for the interview aggregate; auto aggregate still projects.
    store = _FakeEventStore(_events_for(state), raise_on=state.interview_session_id)

    out_dir = await export_trace_from_state(state, ledger, event_store=store)
    # Still wrote files despite the interview-aggregate query failure.
    assert (out_dir / "decisions.jsonl").exists()
    lat = (out_dir / "lateral.jsonl").read_text()
    assert "contrarian" in lat  # auto-aggregate lateral event survived


@pytest.mark.asyncio
async def test_best_effort_swallows_failure(tmp_path: Path) -> None:
    # cwd points at an existing *file*, so mkdir(parents=True) raises.
    blocker_file = tmp_path / "not_a_dir"
    blocker_file.write_text("x")
    state = AutoPipelineState(goal="g", cwd=str(blocker_file))
    state.phase = AutoPhase.COMPLETE
    ledger = SeedDraftLedger.from_goal("g")

    result = await best_effort_export_trace(state, ledger, event_store=None)
    assert result is None  # swallowed, no raise


@pytest.mark.asyncio
async def test_pipeline_finalize_writes_trace_once(tmp_path: Path) -> None:
    state = _terminal_state(tmp_path)
    state.ledger = _populated_ledger().to_dict()
    event_store = _FakeEventStore(_events_for(state))
    driver = types.SimpleNamespace(event_store=event_store, progress_callback=None)
    pipeline = AutoPipeline(
        interview_driver=driver,  # type: ignore[arg-type]
        seed_generator=lambda _sid: None,  # type: ignore[arg-type]
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    out_dir = tmp_path / ".ouroboros" / "traces" / state.auto_session_id
    assert (out_dir / "outcome.json").exists()
    outcome = json.loads((out_dir / "outcome.json").read_text())
    assert outcome["status"] == "complete"
