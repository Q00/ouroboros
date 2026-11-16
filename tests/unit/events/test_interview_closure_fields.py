"""Interview event factories: closure fields are additive and optional."""

from __future__ import annotations

from ouroboros.events.base import BaseEvent
from ouroboros.events.interview import (
    interview_completed,
    interview_response_emitted,
    interview_response_recorded,
    interview_seed_forced,
    interview_started,
)

SNAPSHOT = {"answered_rounds": 3, "verdict": "above_threshold", "ambiguity_score": 0.43}


def test_existing_call_shapes_produce_unchanged_payloads() -> None:
    assert interview_started("i1", "ctx").data == {"initial_context": "ctx"}
    assert interview_response_recorded("i1", 2, "Q?", "A").data == {
        "round_number": 2,
        "question_preview": "Q?",
        "response_preview": "A",
    }
    assert set(interview_completed("i1", 4).data) == {"total_rounds", "timings_ms"}
    emitted = interview_response_emitted(
        "i1",
        response_kind="answer",
        round_number=3,
        payload_chars=10,
        transcript_chars=20,
        ambiguity_prefix_present=False,
        is_length_guard=False,
    )
    assert "closure" not in emitted.data


def test_closure_fields_are_written_when_given() -> None:
    started = interview_started("i1", "ctx", surface="mcp", host="claude_code")
    recorded = interview_response_recorded(
        "i1", 2, "Q?", "A", answer_source="from_code", closure=SNAPSHOT
    )
    completed = interview_completed("i1", 4, closure=SNAPSHOT)
    emitted = interview_response_emitted(
        "i1",
        response_kind="start",
        round_number=1,
        payload_chars=10,
        transcript_chars=0,
        ambiguity_prefix_present=False,
        is_length_guard=False,
        closure=SNAPSHOT,
    )

    assert (started.data["surface"], started.data["host"]) == ("mcp", "claude_code")
    assert recorded.data["answer_source"] == "from_code"
    assert recorded.data["closure"] == SNAPSHOT
    assert completed.data["closure"] == SNAPSHOT
    assert emitted.data["closure"] == SNAPSHOT


def test_host_is_omitted_outside_an_mcp_request() -> None:
    assert interview_started("i1", "ctx", surface="console").data == {
        "initial_context": "ctx",
        "surface": "console",
    }


def test_seed_forced_event_shape() -> None:
    event = interview_seed_forced(
        "interview_x", answered_rounds=5, interview_completed=False, closure=SNAPSHOT
    )

    assert event.type == "interview.seed_forced"
    assert event.aggregate_type == "interview"
    assert event.aggregate_id == "interview_x"
    assert event.data == {
        "answered_rounds": 5,
        "interview_completed": False,
        "closure": SNAPSHOT,
    }


def test_new_fields_survive_a_database_round_trip() -> None:
    event = interview_started("i1", "ctx", surface="auto", host="codex")
    row = event.to_db_dict()
    restored = BaseEvent.from_db_row(
        {
            "id": row["id"],
            "event_type": row["event_type"],
            "timestamp": row["timestamp"],
            "aggregate_type": row["aggregate_type"],
            "aggregate_id": row["aggregate_id"],
            "payload": row["payload"],
        }
    )

    assert restored.data["surface"] == "auto"
    assert restored.data["host"] == "codex"
