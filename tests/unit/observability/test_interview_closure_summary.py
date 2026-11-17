"""Per-surface interview closure summary and best-effort closure recording."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ouroboros.bigbang.interview import InterviewRound, InterviewState, InterviewStatus
from ouroboros.dashboard.board import project_interview
from ouroboros.events.base import BaseEvent
from ouroboros.events.interview import (
    interview_completed,
    interview_response_recorded,
    interview_seed_forced,
    interview_started,
)
from ouroboros.observability import interview_closure_summary as summary_module
from ouroboros.observability.interview_closure_summary import (
    SurfaceClosureSummary,
    load_interview_events,
    record_forced_seed,
    record_interview_event,
    summarize_interview_closure,
)
from ouroboros.persistence.event_store import EventStore, sqlite_database_url

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def _at(event: BaseEvent, hours_ago: float) -> BaseEvent:
    return event.model_copy(update={"timestamp": NOW - timedelta(hours=hours_ago)})


def _synthetic_events() -> list[BaseEvent]:
    """Eight interviews across four surfaces, plus one legacy row set."""
    return [
        # console: 2 completed (6 and 8 questions), 1 forced early, 1 abandoned
        _at(interview_started("c1", "ctx", surface="console"), 50),
        _at(interview_completed("c1", 6), 49),
        _at(interview_started("c2", "ctx", surface="console"), 48),
        _at(interview_completed("c2", 8), 47),
        _at(interview_started("c3", "ctx", surface="console"), 30),
        _at(interview_response_recorded("c3", 1, "Q", "A", answer_source="from_user"), 29),
        _at(interview_seed_forced("c3", answered_rounds=4, interview_completed=False), 29),
        _at(interview_started("c4", "ctx", surface="console"), 40),
        _at(interview_response_recorded("c4", 1, "Q", "A"), 39),
        # mcp from Claude Code: 1 completed, 1 still open (recent)
        _at(interview_started("m1", "ctx", surface="mcp", host="claude_code"), 10),
        _at(interview_completed("m1", 5), 9),
        _at(interview_started("m2", "ctx", surface="mcp", host="claude_code"), 2),
        # cli init: completed, then a forced Seed -> forced early wins
        _at(interview_started("k1", "ctx", surface="cli_init"), 20),
        _at(interview_completed("k1", 3), 19),
        _at(interview_seed_forced("k1", answered_rounds=3, interview_completed=True), 19),
        # legacy row written before surfaces existed
        _at(interview_started("old", "ctx"), 100),
        _at(interview_completed("old", 12), 99),
        # an aggregate without a start event is skipped
        _at(interview_completed("orphan", 4), 5),
    ]


def _by_surface(rows: list[SurfaceClosureSummary]) -> dict[str, SurfaceClosureSummary]:
    return {row.surface: row for row in rows}


def test_summary_counts_each_interview_once_per_surface() -> None:
    rows = _by_surface(summarize_interview_closure(_synthetic_events(), now=NOW))

    assert sorted(rows) == ["cli_init", "console", "mcp:claude_code", "unrecorded"]
    console = rows["console"]
    assert (console.started, console.completed, console.forced_early) == (4, 2, 1)
    assert (console.abandoned, console.open) == (1, 0)
    assert console.median_questions_completed == 7.0
    assert console.mean_questions_completed == 7.0
    assert console.median_questions_forced == 4.0
    assert console.abandon_rate == pytest.approx(1 / 4)

    mcp = rows["mcp:claude_code"]
    assert (mcp.started, mcp.completed, mcp.open, mcp.abandoned) == (2, 1, 1, 0)
    assert mcp.abandon_rate == 0.0

    cli = rows["cli_init"]
    assert (cli.started, cli.completed, cli.forced_early) == (1, 0, 1)
    assert cli.median_questions_completed is None

    legacy = rows["unrecorded"]
    assert (legacy.started, legacy.completed) == (1, 1)
    assert legacy.median_questions_completed == 12.0


def test_summary_is_order_independent_and_tolerates_naive_timestamps() -> None:
    events = _synthetic_events()
    naive = [
        event.model_copy(update={"timestamp": event.timestamp.replace(tzinfo=None)})
        for event in events
    ]

    forward = summarize_interview_closure(events, now=NOW)
    backward = summarize_interview_closure(list(reversed(naive)), now=NOW)

    assert [row.to_dict() for row in forward] == [row.to_dict() for row in backward]


def test_idle_cutoff_separates_open_from_abandoned() -> None:
    events = [_at(interview_started("x", "ctx", surface="auto"), 5)]

    assert summarize_interview_closure(events, now=NOW)[0].open == 1
    shorter = summarize_interview_closure(events, now=NOW, idle_after=timedelta(hours=1))
    assert (shorter[0].open, shorter[0].abandoned) == (0, 1)


def test_reopened_interview_uses_the_last_completion() -> None:
    events = [
        _at(interview_started("r", "ctx", surface="cli_init"), 10),
        _at(interview_completed("r", 3), 9),
        _at(interview_completed("r", 5), 8),
    ]

    assert summarize_interview_closure(events, now=NOW)[0].median_questions_completed == 5.0


def test_malformed_counts_are_ignored_not_coerced() -> None:
    events = [
        _at(interview_started("b", "ctx", surface="console"), 10),
        _at(
            BaseEvent(
                type="interview.completed",
                aggregate_type="interview",
                aggregate_id="b",
                data={"total_rounds": "7"},
            ),
            9,
        ),
    ]

    row = summarize_interview_closure(events, now=NOW)[0]
    assert row.completed == 1
    assert row.median_questions_completed is None


def test_summary_of_no_interviews_is_empty() -> None:
    assert summarize_interview_closure([], now=NOW) == []


def test_summary_reads_labels_and_counts_only() -> None:
    row = summarize_interview_closure(_synthetic_events(), now=NOW)[0]

    assert set(row.to_dict()) == {
        "surface",
        "started",
        "completed",
        "forced_early",
        "abandoned",
        "open",
        "abandon_rate",
        "median_questions_completed",
        "mean_questions_completed",
        "median_questions_forced",
    }


async def _file_store(tmp_path: Path) -> EventStore:
    store = EventStore(sqlite_database_url(tmp_path / "events.db"))
    await store.initialize()
    return store


async def test_load_reads_every_interview_event_across_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(summary_module, "_PAGE_SIZE", 3)
    store = await _file_store(tmp_path)
    try:
        for event in _synthetic_events():
            await store.append(event)
        await store.append(
            BaseEvent(type="other.event", aggregate_type="session", aggregate_id="s", data={})
        )
        loaded = await load_interview_events(store)
    finally:
        await store.close()

    assert len(loaded) == len(_synthetic_events())
    rows = _by_surface(summarize_interview_closure(loaded, now=NOW))
    assert rows["console"].started == 4


async def test_record_interview_event_appends_to_the_given_store(tmp_path: Path) -> None:
    store = await _file_store(tmp_path)
    try:
        await record_interview_event(interview_started("i1", "ctx", surface="console"), store)
        loaded = await load_interview_events(store)
    finally:
        await store.close()

    assert [event.data["surface"] for event in loaded] == ["console"]


async def test_record_interview_event_opens_and_closes_a_default_store() -> None:
    await record_interview_event(interview_started("i2", "ctx", surface="cli_init"))

    store = EventStore()
    try:
        await store.initialize()
        loaded = await load_interview_events(store)
    finally:
        await store.close()
    assert [event.aggregate_id for event in loaded] == ["i2"]


async def test_record_interview_event_never_raises_and_logs_the_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[dict[str, Any]] = []
    monkeypatch.setattr(
        summary_module.log, "warning", lambda name, **kw: warnings.append({"name": name, **kw})
    )

    class _Broken:
        is_initialized = True

        async def initialize(self) -> None:
            return None

        async def append_durable(self, event: BaseEvent, *, timeout: float) -> None:
            raise RuntimeError("database is locked")

    await record_interview_event(interview_started("i3", "ctx"), _Broken())  # type: ignore[arg-type]

    assert warnings == [
        {
            "name": "interview.closure_event_failed",
            "event_type": "interview.started",
            "error": "database is locked",
        }
    ]


async def test_record_interview_event_gives_up_on_a_locked_database_within_the_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3
    import time

    monkeypatch.setattr(summary_module, "APPEND_TIMEOUT_SECONDS", 0.5)
    warnings: list[dict[str, Any]] = []
    monkeypatch.setattr(
        summary_module.log, "warning", lambda name, **kw: warnings.append({"name": name, **kw})
    )
    store = await _file_store(tmp_path)
    blocker = sqlite3.connect(tmp_path / "events.db", timeout=0)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        await record_interview_event(interview_started("locked", "ctx"), store)
        elapsed = time.monotonic() - started
    finally:
        blocker.rollback()
        blocker.close()
    try:
        loaded = await load_interview_events(store)
    finally:
        await store.close()

    assert elapsed < 3.0
    assert [w["name"] for w in warnings] == ["interview.closure_event_failed"]
    assert loaded == []


def _forced_state(*, completed: bool) -> InterviewState:
    state = InterviewState(
        interview_id="interview_forced",
        initial_context="ctx",
        status=InterviewStatus.COMPLETED if completed else InterviewStatus.IN_PROGRESS,
    )
    state.rounds = [
        InterviewRound(round_number=1, question="Q1", user_response="A1"),
        InterviewRound(round_number=2, question="Q2", user_response="A2"),
        InterviewRound(round_number=3, question="Q3", user_response=None),
    ]
    return state


class _Capture:
    def __init__(self) -> None:
        self.events: list[BaseEvent] = []

    is_initialized = True

    async def initialize(self) -> None:
        return None

    async def append(self, event: BaseEvent) -> None:
        self.events.append(event)

    async def append_durable(self, event: BaseEvent, *, timeout: float) -> None:
        assert 0 < timeout <= 5
        self.events.append(event)


async def test_record_forced_seed_is_a_no_op_without_force() -> None:
    capture = _Capture()

    await record_forced_seed(capture, _forced_state(completed=False), None, force=False)  # type: ignore[arg-type]

    assert capture.events == []


async def test_record_forced_seed_writes_counts_and_the_snapshot() -> None:
    capture = _Capture()

    await record_forced_seed(capture, _forced_state(completed=False), None, force=True)  # type: ignore[arg-type]

    (event,) = capture.events
    assert event.type == "interview.seed_forced"
    assert event.aggregate_id == "interview_forced"
    assert event.data["answered_rounds"] == 2
    assert event.data["interview_completed"] is False
    assert event.data["closure"]["verdict"] == "unscored"
    assert "Q1" not in str(event.data) and "A1" not in str(event.data)


@pytest.mark.parametrize(
    "event_type",
    [
        "interview.started",
        "interview.response.recorded",
        "interview.response.emitted",
        "interview.completed",
        "interview.seed_forced",
    ],
)
def test_interview_events_are_not_on_the_telemetry_event_table(event_type: str) -> None:
    from ouroboros import telemetry

    # Interview events stay in the local event store; only the closed
    # network event set ever leaves the machine.
    assert event_type not in telemetry._NETWORK_EVENTS


def test_dashboard_projection_reads_events_with_closure_fields_unchanged() -> None:
    def rows(events: list[BaseEvent]) -> list[dict[str, Any]]:
        return [
            {"event_type": e.type, "aggregate_id": e.aggregate_id, "payload": e.data}
            for e in events
        ]

    legacy = [
        interview_started("p", "ctx"),
        interview_response_recorded("p", 1, "Q", "A"),
        interview_completed("p", 1),
    ]
    labeled = [
        interview_started("p", "ctx", surface="mcp", host="codex"),
        interview_response_recorded("p", 1, "Q", "A", answer_source="from_user"),
        interview_completed("p", 1, closure={"verdict": "passed", "answered_rounds": 1}),
        interview_seed_forced("p", answered_rounds=1, interview_completed=True),
    ]

    assert project_interview(rows(labeled)) == project_interview(rows(legacy))
