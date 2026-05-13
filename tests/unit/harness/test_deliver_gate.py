"""Tests for the #978 P2 read-only deliver-gate manifest loader."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.harness.deliver_gate import (
    DeliverEvidenceClaim,
    DeliverEvidenceFact,
    TraceGuardEvidenceInput,
    evaluate_deliver_claim,
    load_ac_evidence_manifest,
)
from ouroboros.harness.journal import EvidenceEntry, EvidenceKind, EvidenceManifest


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


class _TraceGuardClaim:
    def __init__(self, *, fact_id: str | None, chunk_id: str | None) -> None:
        self.fact_id = fact_id
        self.chunk_id = chunk_id


class _TraceGuardRejection:
    def __init__(self, *, reason: str, claim: _TraceGuardClaim, detail: str) -> None:
        self.reason = reason
        self.claim = claim
        self.detail = detail


class _TraceGuardResult:
    def __init__(
        self,
        *,
        accepted: bool,
        accepted_claims: tuple[_TraceGuardClaim, ...] = (),
        rejected_claims: tuple[_TraceGuardRejection, ...] = (),
        allowed_fact_ids: object | None = None,
        allowed_chunk_ids: object | None = None,
    ) -> None:
        self.accepted = accepted
        self.accepted_claims = accepted_claims
        self.rejected_claims = rejected_claims
        self.allowed_fact_ids = allowed_fact_ids or tuple(
            claim.fact_id for claim in accepted_claims if claim.fact_id is not None
        )
        self.allowed_chunk_ids = allowed_chunk_ids or tuple(
            claim.chunk_id for claim in accepted_claims if claim.chunk_id is not None
        )

    @property
    def unsupported_claim_rate(self) -> float:
        total = len(self.accepted_claims) + len(self.rejected_claims)
        if total == 0:
            return 0.0
        return len(self.rejected_claims) / total


class _RecordingTraceGuardValidator:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        *,
        evidence_manifest: tuple[TraceGuardEvidenceInput, ...],
        parent_synthesis: dict[str, Any],
    ) -> _TraceGuardResult:
        self.calls.append(
            {
                "evidence_manifest": evidence_manifest,
                "parent_synthesis": parent_synthesis,
            }
        )
        allowed = {entry.fact_id: entry for entry in evidence_manifest}
        accepted: list[_TraceGuardClaim] = []
        rejected: list[_TraceGuardRejection] = []
        for item in parent_synthesis["result"]["observed_facts"]:
            fact_id = item.get("fact_id")
            chunk_id = item.get("chunk_id")
            expected = allowed.get(fact_id)
            if expected is None:
                rejected.append(
                    _TraceGuardRejection(
                        reason="unsupported_fact_id",
                        claim=_TraceGuardClaim(fact_id=fact_id, chunk_id=chunk_id),
                        detail="fact is not present in manifest",
                    )
                )
                continue
            if chunk_id != expected.chunk_id:
                rejected.append(
                    _TraceGuardRejection(
                        reason="evidence_handle_mismatch",
                        claim=_TraceGuardClaim(fact_id=fact_id, chunk_id=chunk_id),
                        detail="claim cited the wrong evidence handle",
                    )
                )
                continue
            accepted.append(_TraceGuardClaim(fact_id=fact_id, chunk_id=chunk_id))
        return _TraceGuardResult(
            accepted=not rejected,
            accepted_claims=tuple(accepted),
            rejected_claims=tuple(rejected),
        )


def _manifest_entry(
    *,
    handle: str,
    ok: bool | None,
    source_event_ids: tuple[str, ...],
) -> EvidenceEntry:
    return EvidenceEntry(
        handle=handle,
        kind=EvidenceKind.COMMAND_EXECUTED,
        ok=ok,
        started_at=datetime.now(UTC),
        payload={"tool_name": "Bash", "result_preview": f"result for {handle}"},
        source_event_ids=source_event_ids,
    )


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
    async def test_rejects_session_only_query_to_avoid_mixed_execution_manifests(self) -> None:
        store = _FakeEventStore([])

        with pytest.raises(ValueError, match="requires execution_id"):
            await load_ac_evidence_manifest(store, ac_id="ac_1", session_id="sess_1")

        assert store.execution_queries == []
        assert store.session_queries == []

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
    async def test_requires_execution_anchor(self) -> None:
        with pytest.raises(ValueError, match="requires execution_id"):
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
            await load_ac_evidence_manifest(
                store, ac_id="ac_1", execution_id="exec_1", session_id="  "
            )

        assert store.execution_queries == []
        assert store.session_queries == []

    @pytest.mark.asyncio
    async def test_rejects_blank_ac_id_before_query(self) -> None:
        store = _FakeEventStore([])

        with pytest.raises(ValueError, match="non-blank ac_id"):
            await load_ac_evidence_manifest(store, ac_id="  ", execution_id="exec_1")

        assert store.execution_queries == []


class TestEvaluateDeliverClaim:
    def test_builds_traceguard_envelope_and_returns_accepted_verdict(self) -> None:
        manifest = EvidenceManifest(
            ac_id="AC-1",
            entries=(
                _manifest_entry(handle="ev_pass", ok=True, source_event_ids=("evt_1", "evt_2")),
                _manifest_entry(handle="ev_failed", ok=False, source_event_ids=("evt_failed",)),
            ),
        )
        claim = DeliverEvidenceClaim(
            ac_id="AC-1",
            facts=(
                DeliverEvidenceFact(
                    fact_id="fact_admin_check",
                    evidence_handle="ev_pass",
                    statement="The AC passed because the command succeeded.",
                ),
            ),
        )
        validator = _RecordingTraceGuardValidator()

        verdict = evaluate_deliver_claim(
            manifest,
            claim,
            traceguard_validator=validator,
        )

        assert verdict.accepted is True
        assert verdict.accepted_fact_ids == ("fact_admin_check",)
        assert verdict.rejected_fact_ids == ()
        assert verdict.evidence_event_ids == ("evt_1", "evt_2")
        assert validator.calls == [
            {
                "evidence_manifest": (
                    TraceGuardEvidenceInput(
                        fact_id="fact_admin_check",
                        chunk_id="ev_pass",
                        text="result for ev_pass",
                        child_call_id="evt_1,evt_2",
                    ),
                ),
                "parent_synthesis": {
                    "result": {
                        "observed_facts": [
                            {
                                "fact_id": "fact_admin_check",
                                "chunk_id": "ev_pass",
                                "statement": ("The AC passed because the command succeeded."),
                            }
                        ]
                    }
                },
            }
        ]

    def test_rejected_traceguard_result_is_preserved_for_routing(self) -> None:
        manifest = EvidenceManifest(
            ac_id="AC-1",
            entries=(_manifest_entry(handle="ev_actual", ok=True, source_event_ids=("evt_1",)),),
        )
        claim = DeliverEvidenceClaim(
            ac_id="AC-1",
            facts=(
                DeliverEvidenceFact(
                    fact_id="ev_missing",
                    evidence_handle="ev_missing",
                    statement="Unsupported claim.",
                ),
            ),
        )
        validator = _RecordingTraceGuardValidator()

        verdict = evaluate_deliver_claim(
            manifest,
            claim,
            traceguard_validator=validator,
        )

        assert verdict.accepted is False
        assert verdict.unsupported_claim_rate == 1.0
        assert verdict.accepted_fact_ids == ()
        assert verdict.rejected_fact_ids == ("ev_missing",)
        assert verdict.rejected_reasons == ("unsupported_fact_id: fact is not present in manifest",)
        assert verdict.evidence_event_ids == ()

    def test_accepted_verdict_can_use_allowed_ids_without_claim_objects(self) -> None:
        manifest = EvidenceManifest(
            ac_id="AC-1",
            entries=(_manifest_entry(handle="ev_actual", ok=True, source_event_ids=("evt_1",)),),
        )
        claim = DeliverEvidenceClaim(
            ac_id="AC-1",
            facts=(
                DeliverEvidenceFact(
                    fact_id="fact_actual",
                    evidence_handle="ev_actual",
                    statement="Supported claim.",
                ),
            ),
        )

        verdict = evaluate_deliver_claim(
            manifest,
            claim,
            traceguard_validator=lambda **_: _TraceGuardResult(
                accepted=True,
                allowed_fact_ids=("fact_actual",),
                allowed_chunk_ids=("ev_actual",),
            ),
        )

        assert verdict.accepted is True
        assert verdict.accepted_fact_ids == ("fact_actual",)
        assert verdict.evidence_event_ids == ("evt_1",)

    def test_rejected_verdict_preserves_allowed_id_provenance_without_claim_objects(self) -> None:
        manifest = EvidenceManifest(
            ac_id="AC-1",
            entries=(_manifest_entry(handle="ev_actual", ok=True, source_event_ids=("evt_1",)),),
        )
        claim = DeliverEvidenceClaim(
            ac_id="AC-1",
            facts=(
                DeliverEvidenceFact(
                    fact_id="fact_actual",
                    evidence_handle="ev_actual",
                    statement="Supported claim.",
                ),
                DeliverEvidenceFact(
                    fact_id="fact_missing",
                    evidence_handle="ev_missing",
                    statement="Unsupported claim.",
                ),
            ),
        )

        verdict = evaluate_deliver_claim(
            manifest,
            claim,
            traceguard_validator=lambda **_: _TraceGuardResult(
                accepted=False,
                allowed_fact_ids=frozenset({"fact_actual"}),
                allowed_chunk_ids=frozenset({"ev_actual"}),
                rejected_claims=(
                    _TraceGuardRejection(
                        reason="unsupported_fact_id",
                        claim=_TraceGuardClaim(fact_id="fact_missing", chunk_id="ev_missing"),
                        detail="fact is not present in manifest",
                    ),
                ),
            ),
        )

        assert verdict.accepted is False
        assert verdict.accepted_fact_ids == ("fact_actual",)
        assert verdict.rejected_fact_ids == ("fact_missing",)
        assert verdict.evidence_event_ids == ("evt_1",)

    def test_claim_ac_id_must_match_manifest_scope(self) -> None:
        manifest = EvidenceManifest(
            ac_id="AC-1",
            entries=(_manifest_entry(handle="ev_pass", ok=True, source_event_ids=("evt_1",)),),
        )
        claim = DeliverEvidenceClaim(
            ac_id="AC-2",
            facts=(DeliverEvidenceFact(fact_id="ev_pass", evidence_handle="ev_pass"),),
        )

        with pytest.raises(ValueError, match="must match EvidenceManifest.ac_id"):
            evaluate_deliver_claim(
                manifest,
                claim,
                traceguard_validator=_RecordingTraceGuardValidator(),
            )

    def test_claim_requires_at_least_one_fact(self) -> None:
        with pytest.raises(ValueError, match="requires at least one fact"):
            DeliverEvidenceClaim(ac_id="AC-1", facts=())
