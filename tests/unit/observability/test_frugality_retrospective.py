"""Execution-finalized frugality evidence reporting."""

from __future__ import annotations

from typing import Any

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.observability.frugality_retrospective import (
    RETRY_ASSOCIATED_SPEND,
    UNACCEPTED_SPEND,
    build_frugality_retrospective,
    project_frugality_retrospective,
    report_frugality_retrospective,
)
from ouroboros.orchestrator.events import (
    FRUGALITY_RETROSPECTIVE_EVENT_TYPE,
    create_frugality_retrospective_event,
)
from ouroboros.orchestrator.frugality_proof import (
    EVENT_AC_OUTCOME_FINALIZED,
    EVENT_MODEL_ROUTED,
    EVENT_TOKEN_ATTRIBUTION,
)
from ouroboros.persistence.event_store import EventStore, acceptance_generation_id_for_session


def _event(event_type: str, event_id: str, **data: Any) -> BaseEvent:
    return BaseEvent(
        id=event_id,
        type=event_type,
        aggregate_type="execution",
        aggregate_id="exec-1",
        data=data,
    )


def _token(
    ac_id: str,
    attempt: int,
    spend: object = 10.0,
    *,
    root_ac_index: int = 0,
    event_id: str | None = None,
) -> BaseEvent:
    data: dict[str, Any] = {
        "execution_id": "exec-1",
        "ac_id": ac_id,
        "root_ac_index": root_ac_index,
        "retry_attempt": attempt,
        "token_spend": spend,
    }
    return _event(EVENT_TOKEN_ATTRIBUTION, event_id or f"tok-{ac_id}-{attempt}", **data)


def _outcome(
    attempt: int,
    success: object,
    *,
    root_ac_index: int = 0,
    outcome: object = "succeeded",
    event_id: str | None = None,
) -> BaseEvent:
    return _event(
        EVENT_AC_OUTCOME_FINALIZED,
        event_id or f"out-{root_ac_index}-{attempt}",
        execution_id="exec-1",
        root_ac_index=root_ac_index,
        retry_attempt=attempt,
        success=success,
        outcome=outcome,
    )


def _signal(report: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((item for item in report["evidence_signals"] if item["name"] == name), None)


class TestBuildFrugalityRetrospective:
    @pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
    def test_hard_final_statuses_emit_the_required_v1_envelope(self, status: str) -> None:
        report = build_frugality_retrospective(
            [],
            execution_id="exec-1",
            session_id="sess-1",
            terminal_status=status,
        )

        assert report == {
            "execution_id": "exec-1",
            "session_id": "sess-1",
            "retrospective_version": "v1",
            "trigger": "execution_finalized",
            "terminal_status": status,
            "evidence_only": True,
            "coverage": {
                "measured_attempts": 0,
                "unknown_attempts": 0,
                "invalid_attempts": 0,
                "total_measured_tokens": 0.0,
            },
            "evidence_signals": [],
        }

    def test_paused_is_not_a_reportable_terminal_status(self) -> None:
        assert (
            build_frugality_retrospective(
                [],
                execution_id="exec-1",
                session_id="sess-1",
                terminal_status="paused",
            )
            is None
        )

    def test_final_success_reports_retry_associated_spend_only(self) -> None:
        report = build_frugality_retrospective(
            [
                _token("ac-1", 0, 100.0),
                _outcome(0, False, outcome="failed"),
                _token("ac-1", 1, 50.0),
                _outcome(1, True, outcome="succeeded"),
            ],
            execution_id="exec-1",
            session_id="sess-1",
            terminal_status="completed",
        )

        assert report is not None
        assert report["coverage"] == {
            "measured_attempts": 2,
            "unknown_attempts": 0,
            "invalid_attempts": 0,
            "total_measured_tokens": 150.0,
        }
        assert _signal(report, RETRY_ASSOCIATED_SPEND) == {
            "name": RETRY_ASSOCIATED_SPEND,
            "token_spend": 100.0,
            "attempt_count": 1,
            "latest_attempt_index": 1,
            "evidence_event_ids": ["tok-ac-1-0"],
        }
        assert _signal(report, UNACCEPTED_SPEND) is None

    def test_final_failure_reports_unaccepted_spend_without_calling_it_waste(self) -> None:
        report = build_frugality_retrospective(
            [
                _token("ac-1", 0, 100.0),
                _outcome(0, False, outcome="failed"),
                _token("ac-1", 1, 50.0),
                _outcome(1, False, outcome="failed"),
            ],
            execution_id="exec-1",
            session_id="sess-1",
            terminal_status="failed",
        )

        assert report is not None
        unaccepted = _signal(report, UNACCEPTED_SPEND)
        assert unaccepted == {
            "name": UNACCEPTED_SPEND,
            "token_spend": 150.0,
            "attempt_count": 2,
            "latest_outcome": "failed",
            "evidence_event_ids": ["out-0-1", "tok-ac-1-0", "tok-ac-1-1"],
        }
        assert "waste" not in str(report).lower()
        assert "guardrail" not in str(report).lower()

    def test_successful_first_attempt_has_no_evidence_signal(self) -> None:
        report = build_frugality_retrospective(
            [_token("ac-1", 0, 25.0), _outcome(0, True)],
            execution_id="exec-1",
            session_id="sess-1",
            terminal_status="completed",
        )

        assert report is not None
        assert report["evidence_signals"] == []

    def test_coverage_separates_measured_unknown_and_invalid_attempts(self) -> None:
        missing_spend = _token("ac-missing", 0)
        missing_spend.data.pop("token_spend")
        report = build_frugality_retrospective(
            [
                _token("ac-measured", 0, 10.0),
                _event(
                    EVENT_MODEL_ROUTED,
                    "model-unknown",
                    ac_id="ac-unknown",
                    root_ac_index=1,
                    retry_attempt=0,
                ),
                missing_spend,
                _token("ac-invalid", 0, "not-a-number"),
                _event(
                    EVENT_TOKEN_ATTRIBUTION,
                    "tok-bad-attempt",
                    ac_id="ac-bad-attempt",
                    retry_attempt="0",
                    token_spend=5.0,
                ),
            ],
            execution_id="exec-1",
            session_id="sess-1",
            terminal_status="completed",
        )

        assert report is not None
        assert report["coverage"] == {
            "measured_attempts": 1,
            "unknown_attempts": 2,
            "invalid_attempts": 2,
            "total_measured_tokens": 10.0,
        }

    @pytest.mark.parametrize("spend", [-1.0, float("nan"), float("inf"), True, "10"])
    def test_malformed_token_spend_is_invalid_and_never_fabricates_spend(
        self, spend: object
    ) -> None:
        report = build_frugality_retrospective(
            [_token("ac-1", 0, spend)],
            execution_id="exec-1",
            session_id="sess-1",
            terminal_status="failed",
        )

        assert report is not None
        assert report["coverage"]["invalid_attempts"] == 1
        assert report["coverage"]["total_measured_tokens"] == 0
        assert report["evidence_signals"] == []

    def test_malformed_latest_outcome_fails_closed_without_falling_back(self) -> None:
        report = build_frugality_retrospective(
            [
                _token("ac-1", 0, 10.0),
                _outcome(0, False, outcome="failed"),
                _token("ac-1", 1, 20.0),
                _outcome(1, "false", outcome="failed"),
            ],
            execution_id="exec-1",
            session_id="sess-1",
            terminal_status="failed",
        )

        assert report is not None
        assert report["coverage"]["invalid_attempts"] == 1
        assert _signal(report, UNACCEPTED_SPEND) is None

    def test_proof_reference_is_optional_and_never_gates_emission(self) -> None:
        absent = build_frugality_retrospective(
            [],
            execution_id="exec-1",
            session_id="sess-1",
            terminal_status="completed",
        )
        insufficient = build_frugality_retrospective(
            [
                _event(
                    "execution.frugality_proof.evaluated",
                    "proof-insufficient",
                    status="insufficient_data",
                )
            ],
            execution_id="exec-1",
            session_id="sess-1",
            terminal_status="completed",
        )

        assert absent is not None and "proof_reference" not in absent
        assert insufficient is not None
        assert insufficient["proof_reference"] == "proof-insufficient"

    def test_shared_execution_filters_foreign_session_acceptance(self) -> None:
        target_token = _token("ac-1", 0, 25.0)
        target_token.data["session_id"] = "sess-a"
        target_outcome = _outcome(0, False, outcome="failed")
        target_outcome.data["session_id"] = "sess-a"
        foreign_acceptance = _event(
            "execution.ac.acceptance_finalized",
            "acceptance-b",
            execution_id="exec-1",
            session_id="sess-b",
            acceptance_generation_id=acceptance_generation_id_for_session("sess-b", "exec-1"),
            root_ac_index=0,
            final_retry_attempt=0,
            accepted=True,
            disposition="accepted",
            outcome="succeeded",
            terminal_status="completed",
        )

        report = build_frugality_retrospective(
            [target_token, target_outcome, foreign_acceptance],
            execution_id="exec-1",
            session_id="sess-a",
            terminal_status="failed",
        )

        assert report is not None
        assert report["coverage"]["measured_attempts"] == 1
        assert _signal(report, UNACCEPTED_SPEND) == {
            "name": UNACCEPTED_SPEND,
            "token_spend": 25.0,
            "attempt_count": 1,
            "latest_outcome": "failed",
            "evidence_event_ids": ["out-0-0", "tok-ac-1-0"],
        }


class _MemoryStore:
    def __init__(self, events: list[BaseEvent] | None = None) -> None:
        self.events = list(events or [])
        self.query_calls = 0

    async def query_execution_related_events(
        self,
        execution_id: str,
        event_type: str | None = None,
        limit: int | None = 50,
    ) -> list[BaseEvent]:
        del execution_id, limit
        self.query_calls += 1
        events = self.events
        if event_type is not None:
            events = [event for event in events if event.type == event_type]
        return list(reversed(events))

    async def append(self, event: BaseEvent) -> None:
        self.events.append(event)


class TestReportFrugalityRetrospective:
    @pytest.mark.asyncio
    async def test_pause_resume_same_execution_emits_once_at_hard_finalization(self) -> None:
        store = _MemoryStore([_token("ac-1", 0, 10.0), _outcome(0, True)])

        paused = await report_frugality_retrospective(
            store,
            execution_id="exec-1",
            session_id="sess-1",
            terminal_status="paused",
        )
        completed = await report_frugality_retrospective(
            store,
            execution_id="exec-1",
            session_id="sess-1",
            terminal_status="completed",
        )
        duplicate = await report_frugality_retrospective(
            store,
            execution_id="exec-1",
            session_id="sess-1",
            terminal_status="completed",
        )

        assert paused is False
        assert store.query_calls == 2
        assert completed is True
        assert duplicate is False
        emitted = [
            event for event in store.events if event.type == FRUGALITY_RETROSPECTIVE_EVENT_TYPE
        ]
        assert len(emitted) == 1

    def test_event_id_is_deterministic_per_execution_and_version(self) -> None:
        data = {
            "execution_id": "exec-1",
            "session_id": "sess-1",
            "retrospective_version": "v1",
        }
        first = create_frugality_retrospective_event("exec-1", data)
        second = create_frugality_retrospective_event("exec-1", data)

        assert first.id == second.id
        assert first.aggregate_type == "execution"
        assert first.aggregate_id == "exec-1"

    @pytest.mark.asyncio
    async def test_shared_execution_emits_one_retrospective_per_session(self) -> None:
        store = _MemoryStore()

        assert await report_frugality_retrospective(
            store,
            execution_id="exec-1",
            session_id="sess-a",
            terminal_status="completed",
        )
        assert await report_frugality_retrospective(
            store,
            execution_id="exec-1",
            session_id="sess-b",
            terminal_status="completed",
        )

        emitted = [
            event for event in store.events if event.type == FRUGALITY_RETROSPECTIVE_EVENT_TYPE
        ]
        assert len(emitted) == 2
        assert {event.data["session_id"] for event in emitted} == {"sess-a", "sess-b"}
        assert emitted[0].id != emitted[1].id

    @pytest.mark.asyncio
    async def test_event_store_replay_reconstructs_the_same_payload(self) -> None:
        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()
        try:
            token = _token("ac-1", 0, 25.0)
            outcome = _outcome(0, True)
            await store.append(token)
            await store.append(outcome)

            emitted = await report_frugality_retrospective(
                store,
                execution_id="exec-1",
                session_id="sess-1",
                terminal_status="completed",
            )
            replayed = await store.replay("execution", "exec-1")
        finally:
            await store.close()

        assert emitted is True
        report = next(
            event for event in replayed if event.type == FRUGALITY_RETROSPECTIVE_EVENT_TYPE
        )
        expected = build_frugality_retrospective(
            [outcome, token],
            execution_id="exec-1",
            session_id="sess-1",
            terminal_status="completed",
        )
        assert report.data == expected


class TestProjectFrugalityRetrospective:
    def test_projection_normalizes_the_shared_web_tui_shape(self) -> None:
        report = build_frugality_retrospective(
            [
                _token("ac-1", 0, 100.0),
                _outcome(0, False, outcome="failed"),
                _token("ac-1", 1, 50.0),
                _outcome(1, False, outcome="failed"),
            ],
            execution_id="exec-1",
            session_id="sess-1",
            terminal_status="failed",
        )

        assert report is not None
        assert project_frugality_retrospective(report) == {
            "terminal_status": "failed",
            "measured_attempts": 2,
            "unknown_attempts": 0,
            "invalid_attempts": 0,
            "total_measured_tokens": 150.0,
            "retry_associated_tokens": 100.0,
            "retry_associated_attempts": 1,
            "unaccepted_tokens": 150.0,
            "unaccepted_attempts": 2,
        }

    def test_projection_rejects_non_evidence_or_malformed_payloads(self) -> None:
        assert project_frugality_retrospective({"evidence_only": False}) is None
        assert (
            project_frugality_retrospective(
                {
                    "retrospective_version": "v1",
                    "trigger": "execution_finalized",
                    "terminal_status": "completed",
                    "evidence_only": True,
                    "coverage": {
                        "measured_attempts": 1,
                        "unknown_attempts": 0,
                        "invalid_attempts": 0,
                        "total_measured_tokens": float("nan"),
                    },
                    "evidence_signals": [],
                }
            )
            is None
        )
