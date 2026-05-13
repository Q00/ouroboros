"""Tests for the #978 P2 read-only deliver-gate manifest loader."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.harness.deliver_gate import load_ac_evidence_manifest
from ouroboros.harness.journal import EvidenceKind


def _tool_started(
    *,
    call_id: str,
    ac_id: str | None = "ac_1",
    aggregate_id: str = "exec_1",
    session_id: str | None = None,
    execution_id: str | None = None,
    when: datetime,
) -> BaseEvent:
    return BaseEvent(
        id=f"evt_started_{call_id}",
        type="tool.call.started",
        timestamp=when,
        aggregate_type="execution",
        aggregate_id=aggregate_id,
        data={
            key: value
            for key, value in {
                "call_id": call_id,
                "tool_name": "Bash",
                "ac_id": ac_id,
                "session_id": session_id,
                "execution_id": execution_id,
            }.items()
            if value is not None
        },
    )


def _tool_returned(
    *,
    call_id: str,
    ac_id: str | None = "ac_1",
    aggregate_id: str = "exec_1",
    session_id: str | None = None,
    execution_id: str | None = None,
    when: datetime,
) -> BaseEvent:
    return BaseEvent(
        id=f"evt_returned_{call_id}",
        type="tool.call.returned",
        timestamp=when,
        aggregate_type="execution",
        aggregate_id=aggregate_id,
        data={
            key: value
            for key, value in {
                "call_id": call_id,
                "tool_name": "Bash",
                "ac_id": ac_id,
                "is_error": False,
                "duration_ms": 7,
                "session_id": session_id,
                "execution_id": execution_id,
            }.items()
            if value is not None
        },
    )


class _FakeEventStore:
    def __init__(self, events: list[BaseEvent]) -> None:
        self.events = events
        self.execution_queries: list[dict[str, object]] = []
        self.session_queries: list[dict[str, object]] = []

    async def query_execution_related_events(
        self,
        execution_id: str,
        event_type: str | None = None,
        limit: int | None = 50,
        offset: int = 0,
    ) -> list[BaseEvent]:
        self.execution_queries.append(
            {
                "execution_id": execution_id,
                "event_type": event_type,
                "limit": limit,
                "offset": offset,
            }
        )
        return self.events

    async def query_session_related_events(
        self,
        session_id: str,
        execution_id: str | None = None,
        event_type: str | None = None,
        limit: int | None = 50,
        offset: int = 0,
    ) -> list[BaseEvent]:
        self.session_queries.append(
            {
                "session_id": session_id,
                "execution_id": execution_id,
                "event_type": event_type,
                "limit": limit,
                "offset": offset,
            }
        )
        return self.events


class TestLoadAcEvidenceManifest:
    @pytest.mark.asyncio
    async def test_execution_id_only_query_is_full_read_and_normalizes_chronologically(
        self,
    ) -> None:
        now = datetime.now(UTC)
        returned = _tool_returned(call_id="c1", when=now + timedelta(seconds=1))
        started = _tool_started(call_id="c1", when=now)
        store = _FakeEventStore([returned, started])

        manifest = await load_ac_evidence_manifest(store, ac_id="ac_1", execution_id="exec_1")

        assert store.execution_queries == [
            {"execution_id": "exec_1", "event_type": None, "limit": None, "offset": 0}
        ]
        assert store.session_queries == []
        assert len(manifest.entries) == 1
        entry = manifest.entries[0]
        assert entry.kind is EvidenceKind.COMMAND_EXECUTED
        assert entry.ok is True
        assert entry.source_event_ids == ("evt_started_c1", "evt_returned_c1")

    @pytest.mark.asyncio
    async def test_session_query_is_preferred_when_both_scope_anchors_exist(self) -> None:
        now = datetime.now(UTC)
        store = _FakeEventStore(
            [_tool_started(call_id="c1", session_id="sess_1", execution_id="exec_1", when=now)]
        )

        manifest = await load_ac_evidence_manifest(
            store,
            ac_id="ac_1",
            session_id="sess_1",
            execution_id="exec_1",
        )

        assert store.execution_queries == []
        assert store.session_queries == [
            {
                "session_id": "sess_1",
                "execution_id": "exec_1",
                "event_type": None,
                "limit": None,
                "offset": 0,
            }
        ]
        assert manifest.entries[0].source_event_ids == ("evt_started_c1",)

    @pytest.mark.asyncio
    async def test_identical_timestamps_keep_started_before_returned(self) -> None:
        when = datetime.now(UTC)
        started = _tool_started(call_id="same", when=when)
        returned = _tool_returned(call_id="same", when=when)
        # EventStore query APIs return newest-first, and UUID/string ids do not
        # encode causality. The loader must still feed start before return.
        store = _FakeEventStore([returned, started])

        manifest = await load_ac_evidence_manifest(store, ac_id="ac_1", execution_id="exec_1")

        assert len(manifest.entries) == 1
        assert manifest.entries[0].ok is True
        assert manifest.entries[0].source_event_ids == (
            "evt_started_same",
            "evt_returned_same",
        )

    @pytest.mark.asyncio
    async def test_mismatched_session_execution_events_are_post_filtered(self) -> None:
        now = datetime.now(UTC)
        store = _FakeEventStore(
            [
                _tool_started(
                    call_id="wrong_exec",
                    aggregate_id="other_exec",
                    session_id="sess_1",
                    execution_id="other_exec",
                    when=now,
                ),
                _tool_started(
                    call_id="wrong_session",
                    aggregate_id="exec_1",
                    session_id="other_sess",
                    execution_id="exec_1",
                    when=now + timedelta(seconds=1),
                ),
                _tool_started(
                    call_id="target",
                    session_id="sess_1",
                    execution_id="exec_1",
                    when=now + timedelta(seconds=2),
                ),
            ]
        )

        manifest = await load_ac_evidence_manifest(
            store,
            ac_id="ac_1",
            session_id="sess_1",
            execution_id="exec_1",
        )

        assert len(manifest.entries) == 1
        assert manifest.entries[0].source_event_ids == ("evt_started_target",)

    @pytest.mark.asyncio
    async def test_scope_id_filters_production_shaped_events_without_ac_payload(self) -> None:
        now = datetime.now(UTC)
        store = _FakeEventStore(
            [
                _tool_started(
                    call_id="target",
                    ac_id=None,
                    aggregate_id="ac_runtime_scope",
                    session_id="sess_1",
                    execution_id="exec_1",
                    when=now,
                ),
                _tool_returned(
                    call_id="target",
                    ac_id=None,
                    aggregate_id="ac_runtime_scope",
                    session_id="sess_1",
                    execution_id="exec_1",
                    when=now + timedelta(seconds=1),
                ),
                _tool_started(
                    call_id="other",
                    ac_id=None,
                    aggregate_id="other_runtime_scope",
                    session_id="sess_1",
                    execution_id="exec_1",
                    when=now + timedelta(seconds=2),
                ),
            ]
        )

        manifest = await load_ac_evidence_manifest(
            store,
            ac_id="AC-1",
            scope_id="ac_runtime_scope",
            execution_id="exec_1",
            session_id="sess_1",
        )

        assert manifest.ac_id == "AC-1"
        assert len(manifest.entries) == 1
        assert manifest.entries[0].source_event_ids == (
            "evt_started_target",
            "evt_returned_target",
        )

    @pytest.mark.asyncio
    async def test_session_fallback_query_is_supported_for_observe_only_wiring(self) -> None:
        now = datetime.now(UTC)
        store = _FakeEventStore([_tool_started(call_id="c1", aggregate_id="sess_1", when=now)])

        manifest = await load_ac_evidence_manifest(store, ac_id="ac_1", session_id="sess_1")

        assert store.execution_queries == []
        assert store.session_queries == [
            {
                "session_id": "sess_1",
                "execution_id": None,
                "event_type": None,
                "limit": None,
                "offset": 0,
            }
        ]
        assert manifest.ac_id == "ac_1"
        assert manifest.entries[0].ok is None

    @pytest.mark.asyncio
    async def test_events_from_other_ac_are_filtered_by_normalizer(self) -> None:
        now = datetime.now(UTC)
        store = _FakeEventStore(
            [
                _tool_started(call_id="other", ac_id="ac_2", when=now),
                _tool_started(call_id="target", ac_id="ac_1", when=now + timedelta(seconds=1)),
            ]
        )

        manifest = await load_ac_evidence_manifest(store, ac_id="ac_1", execution_id="exec_1")

        assert len(manifest.entries) == 1
        assert manifest.entries[0].source_event_ids == ("evt_started_target",)

    @pytest.mark.asyncio
    async def test_requires_scope_anchor(self) -> None:
        with pytest.raises(ValueError, match="execution_id or session_id"):
            await load_ac_evidence_manifest(_FakeEventStore([]), ac_id="ac_1")

    @pytest.mark.asyncio
    async def test_rejects_blank_execution_id_instead_of_session_fallback(self) -> None:
        store = _FakeEventStore([])

        with pytest.raises(ValueError, match="blank execution_id"):
            await load_ac_evidence_manifest(
                store,
                ac_id="ac_1",
                execution_id="  ",
                session_id="sess_1",
            )

        assert store.execution_queries == []
        assert store.session_queries == []

    @pytest.mark.asyncio
    async def test_rejects_blank_session_id(self) -> None:
        store = _FakeEventStore([])

        with pytest.raises(ValueError, match="blank session_id"):
            await load_ac_evidence_manifest(store, ac_id="ac_1", session_id="  ")

        assert store.execution_queries == []
        assert store.session_queries == []

    @pytest.mark.asyncio
    async def test_rejects_blank_ac_id_before_query(self) -> None:
        store = _FakeEventStore([])

        with pytest.raises(ValueError, match="non-blank ac_id"):
            await load_ac_evidence_manifest(store, ac_id="  ", execution_id="exec_1")

        assert store.execution_queries == []
