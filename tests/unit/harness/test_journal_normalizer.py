"""Unit tests for the journal → evidence-manifest normalizer.

Covers the contract from issue #978 P1:

* ``normalize_events`` pairs ``tool.call.started`` / ``tool.call.returned``
  events by ``call_id`` and emits one entry per pair.
* Tool name maps to the appropriate :class:`EvidenceKind`
  (``Bash`` → ``command_executed``, ``Write`` / ``Edit`` →
  ``file_modified``, other → ``tool_invocation``).
* Each entry references its source event ids via ``source_event_ids``.
* Unpaired start events surface as ``ok=None`` entries with
  ``ended_at=None`` so dangling work is observable.
* Events explicitly attributed to a different ``ac_id`` are filtered
  out.
* Manifest mappings reject in-place mutation (``MappingProxyType``).
* ``filter_events_for_ac`` returns only events whose payload references
  the target AC.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from pydantic import ValidationError
import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.harness.journal import (
    JOURNAL_SCHEMA_VERSION,
    EvidenceEntry,
    EvidenceKind,
    EvidenceManifest,
    filter_events_for_ac,
    normalize_events,
)


def _tool_started(
    *,
    call_id: str,
    tool_name: str,
    ac_id: str = "ac_1",
    args_preview: str | None = None,
    when: datetime | None = None,
    event_id: str | None = None,
) -> BaseEvent:
    return BaseEvent(
        id=event_id or f"evt_started_{call_id}",
        type="tool.call.started",
        timestamp=when or datetime.now(UTC),
        aggregate_type="session",
        aggregate_id="session_test",
        data={
            "call_id": call_id,
            "tool_name": tool_name,
            "args_preview": args_preview,
            "ac_id": ac_id,
        },
    )


def _tool_returned(
    *,
    call_id: str,
    tool_name: str,
    is_error: bool = False,
    duration_ms: int = 5,
    ac_id: str = "ac_1",
    result_preview: str | None = None,
    error_kind: str | None = None,
    when: datetime | None = None,
    event_id: str | None = None,
) -> BaseEvent:
    return BaseEvent(
        id=event_id or f"evt_returned_{call_id}",
        type="tool.call.returned",
        timestamp=when or datetime.now(UTC),
        aggregate_type="session",
        aggregate_id="session_test",
        data={
            "call_id": call_id,
            "tool_name": tool_name,
            "is_error": is_error,
            "duration_ms": duration_ms,
            "result_preview": result_preview,
            "error_kind": error_kind,
            "ac_id": ac_id,
        },
    )


class TestSchemaVersion:
    def test_initial_version_is_one(self) -> None:
        assert JOURNAL_SCHEMA_VERSION == 1


class TestEvidenceEntry:
    def test_requires_non_empty_source_event_ids(self) -> None:
        with pytest.raises(ValidationError):
            EvidenceEntry(
                kind=EvidenceKind.TOOL_INVOCATION,
                started_at=datetime.now(UTC),
                source_event_ids=(),
            )

    def test_rejects_blank_source_event_id(self) -> None:
        with pytest.raises(ValidationError):
            EvidenceEntry(
                kind=EvidenceKind.TOOL_INVOCATION,
                started_at=datetime.now(UTC),
                source_event_ids=("   ",),
            )

    def test_rejects_ended_before_started(self) -> None:
        now = datetime.now(UTC)
        with pytest.raises(ValidationError):
            EvidenceEntry(
                kind=EvidenceKind.TOOL_INVOCATION,
                started_at=now,
                ended_at=now - timedelta(seconds=1),
                source_event_ids=("evt_1",),
            )

    def test_payload_blocks_setitem(self) -> None:
        entry = EvidenceEntry(
            kind=EvidenceKind.TOOL_INVOCATION,
            started_at=datetime.now(UTC),
            payload={"tool_name": "Bash"},
            source_event_ids=("evt_1",),
        )
        assert isinstance(entry.payload, MappingProxyType)
        with pytest.raises(TypeError):
            entry.payload["tool_name"] = "Edit"  # type: ignore[index]

    def test_generates_prefixed_handle(self) -> None:
        entry = EvidenceEntry(
            kind=EvidenceKind.TOOL_INVOCATION,
            started_at=datetime.now(UTC),
            source_event_ids=("evt_1",),
        )
        assert entry.handle.startswith("ev_")


class TestEvidenceManifest:
    def test_ac_id_required(self) -> None:
        with pytest.raises(ValidationError):
            EvidenceManifest(ac_id="   ")

    def test_metadata_blocks_setitem(self) -> None:
        manifest = EvidenceManifest(ac_id="ac_1")
        with pytest.raises(TypeError):
            manifest.metadata["k"] = "v"  # type: ignore[index]

    def test_is_frozen(self) -> None:
        manifest = EvidenceManifest(ac_id="ac_1")
        with pytest.raises(ValidationError):
            manifest.ac_id = "ac_2"  # type: ignore[misc]


class TestNormalizeEventsToolPairs:
    def test_pairs_started_and_returned_by_call_id(self) -> None:
        start_time = datetime.now(UTC)
        events = [
            _tool_started(
                call_id="c1",
                tool_name="Edit",
                event_id="evt_start_c1",
                when=start_time,
                args_preview="path=src/foo.py",
            ),
            _tool_returned(
                call_id="c1",
                tool_name="Edit",
                event_id="evt_return_c1",
                when=start_time + timedelta(milliseconds=5),
                duration_ms=5,
                is_error=False,
                result_preview="ok",
            ),
        ]
        manifest = normalize_events(events, ac_id="ac_1")
        assert len(manifest.entries) == 1
        entry = manifest.entries[0]
        assert entry.kind is EvidenceKind.FILE_MODIFIED
        assert entry.ok is True
        assert entry.source_event_ids == ("evt_start_c1", "evt_return_c1")
        assert entry.payload["tool_name"] == "Edit"
        assert entry.payload["args_preview"] == "path=src/foo.py"
        assert entry.payload["result_preview"] == "ok"
        assert entry.payload["duration_ms"] == 5

    def test_bash_tool_emits_command_executed(self) -> None:
        events = [
            _tool_started(call_id="c2", tool_name="Bash"),
            _tool_returned(call_id="c2", tool_name="Bash"),
        ]
        manifest = normalize_events(events, ac_id="ac_1")
        assert manifest.entries[0].kind is EvidenceKind.COMMAND_EXECUTED

    def test_unknown_tool_falls_back_to_tool_invocation(self) -> None:
        events = [
            _tool_started(call_id="c3", tool_name="WebFetch"),
            _tool_returned(call_id="c3", tool_name="WebFetch"),
        ]
        manifest = normalize_events(events, ac_id="ac_1")
        assert manifest.entries[0].kind is EvidenceKind.TOOL_INVOCATION

    def test_returned_with_error_sets_ok_false(self) -> None:
        events = [
            _tool_started(call_id="c4", tool_name="Bash"),
            _tool_returned(
                call_id="c4",
                tool_name="Bash",
                is_error=True,
                error_kind="non_zero_exit",
            ),
        ]
        manifest = normalize_events(events, ac_id="ac_1")
        entry = manifest.entries[0]
        assert entry.ok is False
        assert entry.payload["is_error"] is True
        assert entry.payload["error_kind"] == "non_zero_exit"

    def test_unpaired_start_surfaces_as_running(self) -> None:
        events = [_tool_started(call_id="c5", tool_name="Bash")]
        manifest = normalize_events(events, ac_id="ac_1")
        assert len(manifest.entries) == 1
        entry = manifest.entries[0]
        assert entry.ok is None
        assert entry.ended_at is None
        assert entry.source_event_ids == ("evt_started_c5",)

    def test_completion_only_pair_still_emitted(self) -> None:
        # A `returned` event without a matching `started` is still
        # emitted so legacy traces remain observable.
        events = [_tool_returned(call_id="orphan", tool_name="Bash")]
        manifest = normalize_events(events, ac_id="ac_1")
        assert len(manifest.entries) == 1
        entry = manifest.entries[0]
        assert entry.kind is EvidenceKind.COMMAND_EXECUTED
        assert entry.source_event_ids == ("evt_returned_orphan",)


class TestNormalizeEventsACScope:
    def test_drops_events_belonging_to_other_ac(self) -> None:
        events = [
            _tool_started(call_id="c1", tool_name="Bash", ac_id="ac_other"),
            _tool_returned(call_id="c1", tool_name="Bash", ac_id="ac_other"),
            _tool_started(call_id="c2", tool_name="Bash", ac_id="ac_target"),
            _tool_returned(call_id="c2", tool_name="Bash", ac_id="ac_target"),
        ]
        manifest = normalize_events(events, ac_id="ac_target")
        assert len(manifest.entries) == 1
        entry = manifest.entries[0]
        assert "ac_other" not in str(entry.source_event_ids)

    def test_rejects_blank_ac_id(self) -> None:
        with pytest.raises(ValueError):
            normalize_events([], ac_id="   ")

    def test_trims_ac_id_whitespace(self) -> None:
        manifest = normalize_events([], ac_id="  ac_padded  ")
        assert manifest.ac_id == "ac_padded"


class TestFilterEventsForAC:
    def test_returns_only_matching_ac(self) -> None:
        events = [
            _tool_started(call_id="c1", tool_name="Bash", ac_id="ac_a"),
            _tool_started(call_id="c2", tool_name="Bash", ac_id="ac_b"),
        ]
        filtered = filter_events_for_ac(events, ac_id="ac_a")
        assert len(filtered) == 1
        assert filtered[0].data["call_id"] == "c1"

    def test_excludes_events_without_ac_id(self) -> None:
        bare = BaseEvent(
            id="evt_bare",
            type="tool.call.started",
            timestamp=datetime.now(UTC),
            aggregate_type="session",
            aggregate_id="session_test",
            data={"call_id": "c1", "tool_name": "Bash"},
        )
        filtered = filter_events_for_ac([bare], ac_id="ac_a")
        assert filtered == ()

    def test_rejects_blank_ac_id(self) -> None:
        with pytest.raises(ValueError):
            filter_events_for_ac([], ac_id="   ")


class TestLLMEntries:
    def test_llm_returned_emits_entry(self) -> None:
        event = BaseEvent(
            id="evt_llm_1",
            type="llm.call.returned",
            timestamp=datetime.now(UTC),
            aggregate_type="session",
            aggregate_id="session_test",
            data={
                "model": "claude-sonnet-4.6",
                "role": "deliver",
                "is_error": False,
                "ac_id": "ac_1",
            },
        )
        manifest = normalize_events([event], ac_id="ac_1")
        assert len(manifest.entries) == 1
        entry = manifest.entries[0]
        assert entry.kind is EvidenceKind.LLM_CALL
        assert entry.ok is True
        assert entry.payload["model"] == "claude-sonnet-4.6"
        assert entry.payload["role"] == "deliver"


class TestEnumerations:
    def test_evidence_kind_values(self) -> None:
        assert {kind.value for kind in EvidenceKind} == {
            "tool_invocation",
            "command_executed",
            "file_modified",
            "llm_call",
        }
