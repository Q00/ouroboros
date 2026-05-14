"""Journal → evidence-manifest normalization.

The journal normalizer is the first slice of issue #978 (the AgentOS
Evidence Gate spine). It walks an ordered sequence of ``EventStore``
events for a single acceptance-criterion scope and produces a typed
:class:`EvidenceManifest` that downstream verifiers — including the
``TraceGuard`` deliver gate — can consult without re-mining raw logs.

Design constraints:

* The normalizer is a **pure read** function. It does not write to
  ``EventStore`` and does not mutate the events it walks.
* Each manifest entry carries a stable ``handle`` that the leaf agent
  can cite in its evidence claim, and the ``source_event_ids`` that
  produced it so verdicts remain replayable.
* Manifest mappings are immutable
  (:class:`types.MappingProxyType`) so cached projections cannot
  silently drift when consumers stash a reference.
* No EventStore wiring lives here yet. The downstream harness hook
  that pulls events out of ``EventStore`` and feeds them to this
  normalizer lands in the P2 deliver-gate PR.

The set of event types the normalizer recognizes in this PR is small
on purpose — it covers the canonical ``tool.call.started`` /
``tool.call.returned`` pair and the conventional ``Bash`` / ``Write``
/ ``Edit`` tool names that already drive the existing executor. Plugin
-defined evidence types (#939) and additional kinds arrive in later
slices.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)

from ouroboros.events.base import BaseEvent

JOURNAL_SCHEMA_VERSION = 1
"""Initial schema version for the evidence manifest."""

_MEMORY_FILENAMES = frozenset({"MEMORY.md", ".memory.md"})


# ---------------------------------------------------------------------------
# Internal helpers — immutable mappings + identifier hygiene
# ---------------------------------------------------------------------------


def _deep_freeze(value: Any) -> Any:
    """Recursively convert mappings/lists into immutable views.

    Bare :class:`types.MappingProxyType` only blocks top-level
    ``__setitem__``; nested ``dict``/``list`` values remain mutable and
    can be reached through the proxy. To honour the "cached projections
    cannot silently drift" contract we deep-copy + freeze every layer,
    converting dicts to ``MappingProxyType`` views, lists to tuples,
    sets to ``frozenset`` values, and mutable byte arrays to ``bytes``.
    """
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_deep_freeze(item) for item in value)
    if isinstance(value, bytearray):
        return bytes(value)
    return value


def _deep_thaw(value: Any) -> Any:
    """Convert frozen containers back to JSON-native containers for dumps."""
    if isinstance(value, Mapping):
        return {key: _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    if isinstance(value, set | frozenset):
        thawed = [_deep_thaw(item) for item in value]
        return sorted(thawed, key=_stable_json_sort_key)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _stable_json_sort_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _coerce_to_mapping(value: Any) -> Mapping[str, Any]:
    """Normalize mapping-shaped input into a freshly deep-frozen view."""
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        msg = f"mapping field must be a mapping, got {type(value).__name__}"
        raise ValueError(msg)
    return _deep_freeze(value)


def _ensure_frozen_after(value: Any) -> Mapping[str, Any]:
    """Final-stage deep-freeze guaranteeing nested immutability."""
    if not isinstance(value, Mapping):
        msg = f"mapping field must be a mapping, got {type(value).__name__}"
        raise ValueError(msg)
    return _deep_freeze(value)


def _empty_frozen_mapping() -> Mapping[str, Any]:
    return MappingProxyType({})


FrozenMapping = Annotated[
    Mapping[str, Any],
    BeforeValidator(_coerce_to_mapping),
    AfterValidator(_ensure_frozen_after),
    PlainSerializer(lambda value: _deep_thaw(value), return_type=dict, when_used="always"),
]


def _normalize_id_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str):
            msg = f"identifier at index {index} must be a string; got {type(value).__name__}"
            raise TypeError(msg)
        stripped = value.strip()
        if not stripped:
            msg = (
                f"identifier at index {index} is empty or whitespace-only; "
                "the journal normalizer requires usable provenance ids"
            )
            raise ValueError(msg)
        normalized.append(stripped)
    return tuple(normalized)


IdentifierTuple = Annotated[tuple[str, ...], AfterValidator(_normalize_id_tuple)]


# ---------------------------------------------------------------------------
# Manifest models
# ---------------------------------------------------------------------------


class EvidenceKind(StrEnum):
    """Manifest entry classification.

    The set is intentionally small in PR-1; plugin-defined kinds are
    added in #939's lifecycle work and are validated against the same
    open-vocabulary pattern :class:`ouroboros.harness.projection.ArtifactRecord`
    uses today.
    """

    TOOL_INVOCATION = "tool_invocation"
    COMMAND_EXECUTED = "command_executed"
    FILE_MODIFIED = "file_modified"
    LLM_CALL = "llm_call"


def _stable_entry_handle(
    *,
    kind: EvidenceKind,
    payload: Mapping[str, Any],
    source_event_ids: tuple[str, ...],
) -> str:
    serialized = json.dumps(
        {
            "kind": kind.value,
            "payload": _deep_thaw(payload),
            "source_event_ids": source_event_ids,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"ev_{sha256(serialized.encode('utf-8')).hexdigest()[:16]}"


def _stable_manifest_id(*, ac_id: str, entries: tuple[EvidenceEntry, ...]) -> str:
    serialized = json.dumps(
        {
            "ac_id": ac_id,
            "entries": [
                {
                    "handle": entry.handle,
                    "kind": entry.kind.value,
                    "source_event_ids": entry.source_event_ids,
                }
                for entry in entries
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"manifest_{sha256(serialized.encode('utf-8')).hexdigest()[:16]}"


class EvidenceEntry(BaseModel, frozen=True):
    """A single piece of evidence in an :class:`EvidenceManifest`.

    Attributes:
        handle: Stable, manifest-scoped identifier that the leaf agent
            can cite in its evidence claim (``ev_<sha256-prefix>`` by default).
        kind: Discriminator from :class:`EvidenceKind`.
        ok: Tri-valued success flag. ``True`` = succeeded, ``False`` =
            failed, ``None`` = undetermined (e.g. only the start event
            was observed).
        started_at: When the underlying work began. Falls back to the
            earliest source event's timestamp when no explicit start is
            available.
        ended_at: When the underlying work finished. ``None`` for
            entries observed only via a start event.
        payload: Structured details (tool name, command, file path,
            etc.). Immutable at runtime.
        source_event_ids: One or more event ids that produced this
            entry. Empty tuples are rejected — every entry must be
            traceable back to the journal.
    """

    schema_version: int = Field(default=JOURNAL_SCHEMA_VERSION, ge=1)
    handle: str = Field(default="__auto__", min_length=1)
    kind: EvidenceKind
    ok: bool | None = Field(default=None)
    started_at: datetime
    ended_at: datetime | None = Field(default=None)
    payload: FrozenMapping = Field(default_factory=_empty_frozen_mapping)
    source_event_ids: IdentifierTuple

    @field_validator("source_event_ids")
    @classmethod
    def _source_events_non_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            msg = "EvidenceEntry.source_event_ids must reference at least one journal event"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _validate_timestamps(self) -> EvidenceEntry:
        if self.ended_at is not None and self.ended_at < self.started_at:
            msg = "EvidenceEntry.ended_at cannot precede started_at"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _stabilize_generated_handle(self) -> EvidenceEntry:
        if self.handle != "__auto__":
            return self
        object.__setattr__(
            self,
            "handle",
            _stable_entry_handle(
                kind=self.kind,
                payload=self.payload,
                source_event_ids=self.source_event_ids,
            ),
        )
        return self


class EvidenceManifest(BaseModel, frozen=True):
    """Per-AC evidence manifest derived from the journal.

    Manifests are the ``evidence_manifest`` input to the TraceGuard
    deliver gate. They are scoped to a single acceptance criterion so a
    leaf agent's claim can be compared against the observable trace of
    its own work.

    Attributes:
        manifest_id: Stable identifier derived from AC id and entries.
        ac_id: Acceptance-criterion identifier this manifest covers.
        entries: Tuple of :class:`EvidenceEntry` records in observation
            order.
        normalized_at: When the manifest was produced.
        metadata: Free-form metadata bag (immutable at runtime).
    """

    schema_version: int = Field(default=JOURNAL_SCHEMA_VERSION, ge=1)
    manifest_id: str = Field(default="__auto__", min_length=1)
    ac_id: str = Field(..., min_length=1)
    entries: tuple[EvidenceEntry, ...] = Field(default_factory=tuple)
    normalized_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: FrozenMapping = Field(default_factory=_empty_frozen_mapping)

    @field_validator("ac_id")
    @classmethod
    def _ac_id_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            msg = "EvidenceManifest.ac_id must be a non-blank identifier"
            raise ValueError(msg)
        return stripped

    @model_validator(mode="after")
    def _stabilize_generated_manifest_id(self) -> EvidenceManifest:
        if self.manifest_id != "__auto__":
            return self
        object.__setattr__(
            self,
            "manifest_id",
            _stable_manifest_id(ac_id=self.ac_id, entries=self.entries),
        )
        return self


# ---------------------------------------------------------------------------
# Normalizer
# ---------------------------------------------------------------------------


_TOOL_STARTED = "tool.call.started"
_TOOL_RETURNED = "tool.call.returned"
_LLM_REQUESTED = "llm.call.requested"
_LLM_RETURNED = "llm.call.returned"

_FILE_MODIFY_TOOL_NAMES = frozenset({"Write", "Edit", "NotebookEdit"})
_COMMAND_TOOL_NAMES = frozenset({"Bash"})


def _event_scope_tokens(event: BaseEvent) -> tuple[str, ...]:
    """Return all scope tokens an event can be attributed to.

    The Ouroboros I/O recorders (``src/ouroboros/events/io.py``) emit
    tool / LLM events using ``aggregate_type`` / ``aggregate_id`` for
    the target plus correlation fields (``session_id``,
    ``execution_id``, ``phase``, ``lineage_id``). They do not currently
    emit a dedicated ``ac_id`` payload. This helper returns every
    channel an event could plausibly belong to so the normalizer can
    accept matches against any of them.

    Channels considered, in priority order:
    1. ``event.data["ac_id"]`` — explicit attribution if the producer
       chose to embed it.
    2. ``event.aggregate_id`` — the recorder's configured target id.
    3. ``event.data["execution_id"]`` — correlation field; an AC's
       execution typically owns a single execution id.
    4. ``event.data["phase"]`` — correlation field; used by some
       executors to carry the AC identifier.
    """
    tokens: list[str] = []
    if isinstance(event.data, dict):
        for key in ("ac_id", "execution_id", "phase"):
            value = event.data.get(key)
            if isinstance(value, str) and value.strip():
                tokens.append(value.strip())
    if isinstance(event.aggregate_id, str) and event.aggregate_id.strip():
        tokens.append(event.aggregate_id.strip())
    return tuple(tokens)


def _event_matches_ac(event: BaseEvent, ac_id: str) -> bool:
    """Return True when any scope channel on the event matches ``ac_id``.

    Used both for filtering inside :func:`normalize_events` and by the
    :func:`filter_events_for_ac` helper. Returns ``False`` when no
    channel on the event references the AC at all — pre-filtered event
    iterables should therefore be acceptable inputs to
    :func:`normalize_events`.
    """
    target = ac_id.strip()
    if not target:
        return False
    return target in _event_scope_tokens(event)


def _classify_tool_kind(tool_name: str) -> EvidenceKind:
    if tool_name in _COMMAND_TOOL_NAMES:
        return EvidenceKind.COMMAND_EXECUTED
    if tool_name in _FILE_MODIFY_TOOL_NAMES:
        return EvidenceKind.FILE_MODIFIED
    return EvidenceKind.TOOL_INVOCATION


def _tool_payload(
    *,
    tool_name: str,
    args_preview: str | None,
    result_preview: str | None,
    duration_ms: int | None,
    is_error: bool | None,
    error_kind: str | None,
    child_ac_id: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"tool_name": tool_name}
    if child_ac_id is not None:
        payload["child_ac_id"] = child_ac_id
    if args_preview is not None:
        payload["args_preview"] = args_preview
    if result_preview is not None:
        payload["result_preview"] = result_preview
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    if is_error is not None:
        payload["is_error"] = is_error
    if error_kind is not None:
        payload["error_kind"] = error_kind
    return payload


def normalize_events(
    events: Iterable[BaseEvent],
    ac_id: str,
) -> EvidenceManifest:
    """Walk an event sequence and produce an :class:`EvidenceManifest`.

    The normalizer pairs ``tool.call.started`` and ``tool.call.returned``
    events by ``call_id`` and emits a single entry per pair. Unpaired
    start events are emitted with ``ok=None`` and ``ended_at=None`` so
    long-running work that has not yet completed is still observable.
    Returned events with no matching start are emitted as completion-only
    entries; this matches the behaviour of legacy traces where the start
    record was lost or never persisted.

    Args:
        events: Ordered iterable of :class:`BaseEvent` records, typically
            already filtered to the AC scope by the caller.
        ac_id: Acceptance-criterion identifier the manifest belongs to.
            Used both to populate :attr:`EvidenceManifest.ac_id` and to
            filter events whose payload explicitly references a
            different ``ac_id``.

    Returns:
        An :class:`EvidenceManifest` containing the normalized entries
        in observation order.
    """
    normalized_ac_id = ac_id.strip()
    if not normalized_ac_id:
        msg = "normalize_events requires a non-blank ac_id"
        raise ValueError(msg)

    # ``slots`` preserves observation order: a started event reserves a
    # slot at its observed index, and the matching returned event fills
    # that slot in place. Returned-only events append a new slot. Slots
    # left as :class:`BaseEvent` at the end are finalized as pending /
    # dangling entries. This stops unmatched start events from being
    # shuffled to the tail of the manifest after later pairs complete
    # (the previous append-pending-at-end strategy reordered the trace).
    slots: list[BaseEvent | EvidenceEntry | None] = []
    tool_slot_index: dict[str, int] = {}
    llm_slot_index: dict[str, int] = {}
    memory_tool_call_ids: set[str] = set()
    memory_llm_call_ids: set[str] = set()

    for event in events:
        # Skip events that carry explicit scope tokens but none of them
        # references this AC. Events without any scope token are allowed
        # through so pre-filtered iterables remain valid inputs.
        scope_tokens = _event_scope_tokens(event)
        if scope_tokens and normalized_ac_id not in scope_tokens:
            continue

        if _is_memory_derived_event(event):
            call_id = _slot_call_id(event)
            if call_id:
                if event.type in {_TOOL_STARTED, _TOOL_RETURNED}:
                    memory_tool_call_ids.add(call_id)
                    slot = tool_slot_index.pop(call_id, None)
                    if slot is not None:
                        slots[slot] = None
                elif event.type in {_LLM_REQUESTED, _LLM_RETURNED}:
                    memory_llm_call_ids.add(call_id)
                    slot = llm_slot_index.pop(call_id, None)
                    if slot is not None:
                        slots[slot] = None
            continue

        if event.type == _TOOL_STARTED:
            call_id = event.data.get("call_id") if isinstance(event.data, dict) else None
            normalized_call_id = call_id.strip() if isinstance(call_id, str) else ""
            if normalized_call_id and normalized_call_id not in memory_tool_call_ids:
                tool_slot_index[normalized_call_id] = len(slots)
                slots.append(event)
            continue

        if event.type == _TOOL_RETURNED:
            call_id_raw = event.data.get("call_id") if isinstance(event.data, dict) else None
            call_id = call_id_raw.strip() if isinstance(call_id_raw, str) else ""
            if call_id in memory_tool_call_ids:
                continue
            slot = tool_slot_index.pop(call_id, None) if call_id else None
            start_event: BaseEvent | None = None
            if slot is not None:
                start_event = slots[slot]  # type: ignore[assignment]
            entry = _build_tool_entry_for_returned(start_event, event)
            if entry is None:
                continue
            if slot is not None:
                slots[slot] = entry
            else:
                slots.append(entry)
            continue

        if event.type == _LLM_REQUESTED:
            call_id = event.data.get("call_id") if isinstance(event.data, dict) else None
            normalized_call_id = call_id.strip() if isinstance(call_id, str) else ""
            if normalized_call_id and normalized_call_id not in memory_llm_call_ids:
                llm_slot_index[normalized_call_id] = len(slots)
                slots.append(event)
            continue

        if event.type == _LLM_RETURNED:
            call_id_raw = event.data.get("call_id") if isinstance(event.data, dict) else None
            call_id = call_id_raw.strip() if isinstance(call_id_raw, str) else ""
            if call_id in memory_llm_call_ids:
                continue
            slot = llm_slot_index.pop(call_id, None) if call_id else None
            requested_event: BaseEvent | None = None
            if slot is not None:
                requested_event = slots[slot]  # type: ignore[assignment]
            entry = _build_llm_entry_for_returned(requested_event, event)
            if entry is None:
                continue
            if slot is not None:
                slots[slot] = entry
            else:
                slots.append(entry)
            continue

    # Finalize: every remaining slot is either an already-built
    # EvidenceEntry or a dangling start/requested BaseEvent that we
    # surface as a still-running entry so the caller can detect
    # incomplete work.
    entries: list[EvidenceEntry] = []
    for slot in slots:
        if slot is None:
            continue
        if isinstance(slot, EvidenceEntry):
            entries.append(slot)
            continue
        if slot.type == _TOOL_STARTED:
            pending = _build_tool_entry_from_start_only(_slot_call_id(slot), slot)
            if pending is not None:
                entries.append(pending)
        elif slot.type == _LLM_REQUESTED:
            pending = _build_llm_entry_from_start_only(_slot_call_id(slot), slot)
            if pending is not None:
                entries.append(pending)

    return EvidenceManifest(
        ac_id=normalized_ac_id,
        entries=tuple(entries),
    )


def _slot_call_id(event: BaseEvent) -> str:
    if not isinstance(event.data, dict):
        return ""
    call_id = event.data.get("call_id")
    return call_id.strip() if isinstance(call_id, str) else ""


def _event_child_ac_id(*events: BaseEvent | None) -> str | None:
    for event in events:
        if event is None or not isinstance(event.data, dict):
            continue
        value = event.data.get("child_ac_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
        extra = event.data.get("extra")
        if isinstance(extra, Mapping):
            value = extra.get("child_ac_id")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _is_memory_derived_event(event: BaseEvent) -> bool:
    return (
        _is_memory_derived_value(event.aggregate_id)
        or _is_memory_derived_value(event.aggregate_type)
        or _is_memory_derived_value(event.data)
    )


def _is_memory_derived_value(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            _looks_like_memory_reference(str(key)) or _is_memory_derived_value(item)
            for key, item in value.items()
        )
    if isinstance(value, list | tuple | set | frozenset):
        return any(_is_memory_derived_value(item) for item in value)
    if isinstance(value, str):
        return _looks_like_memory_reference(value)
    return False


def _looks_like_memory_reference(value: str) -> bool:
    if not value:
        return False
    normalized = value.replace("\\", "/")
    for part in normalized.split():
        token = part.strip("`'\".,:;()[]{}")
        candidates = (token, token.rsplit("=", maxsplit=1)[-1], token.rsplit(":", maxsplit=1)[-1])
        for candidate in candidates:
            filename = PurePosixPath(candidate.strip("`'\".,:;()[]{}")).name
            if filename in _MEMORY_FILENAMES:
                return True
    return False


def _build_tool_entry_for_returned(
    start_event: BaseEvent | None,
    returned_event: BaseEvent,
) -> EvidenceEntry | None:
    if not isinstance(returned_event.data, dict):
        return None

    tool_name = returned_event.data.get("tool_name") or (
        start_event.data.get("tool_name") if start_event else None
    )
    if not isinstance(tool_name, str) or not tool_name.strip():
        return None

    started_at = start_event.timestamp if start_event else returned_event.timestamp
    is_error = returned_event.data.get("is_error")
    ok = (not bool(is_error)) if isinstance(is_error, bool) else None

    payload = _tool_payload(
        tool_name=tool_name.strip(),
        args_preview=(
            start_event.data.get("args_preview")
            if start_event and isinstance(start_event.data, dict)
            else None
        ),
        result_preview=returned_event.data.get("result_preview"),
        duration_ms=returned_event.data.get("duration_ms"),
        is_error=is_error if isinstance(is_error, bool) else None,
        error_kind=returned_event.data.get("error_kind"),
        child_ac_id=_event_child_ac_id(returned_event, start_event),
    )

    source_event_ids: list[str] = []
    if start_event is not None:
        source_event_ids.append(start_event.id)
    source_event_ids.append(returned_event.id)

    return EvidenceEntry(
        kind=_classify_tool_kind(tool_name.strip()),
        ok=ok,
        started_at=started_at,
        ended_at=returned_event.timestamp,
        payload=payload,
        source_event_ids=tuple(source_event_ids),
    )


def _build_tool_entry_from_start_only(
    call_id: str,
    start_event: BaseEvent,
) -> EvidenceEntry | None:
    del call_id  # reserved for future correlation logging
    if not isinstance(start_event.data, dict):
        return None
    tool_name = start_event.data.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name.strip():
        return None
    payload = _tool_payload(
        tool_name=tool_name.strip(),
        args_preview=start_event.data.get("args_preview"),
        result_preview=None,
        duration_ms=None,
        is_error=None,
        error_kind=None,
        child_ac_id=_event_child_ac_id(start_event),
    )
    return EvidenceEntry(
        kind=_classify_tool_kind(tool_name.strip()),
        ok=None,
        started_at=start_event.timestamp,
        ended_at=None,
        payload=payload,
        source_event_ids=(start_event.id,),
    )


def _llm_payload(
    *,
    model_id: str | None,
    caller: str | None,
    duration_ms: int | None,
    is_error: bool | None,
    error_kind: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if model_id is not None:
        payload["model_id"] = model_id
    if caller is not None:
        payload["caller"] = caller
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    if is_error is not None:
        payload["is_error"] = is_error
    if error_kind is not None:
        payload["error_kind"] = error_kind
    return payload


def _build_llm_entry_for_returned(
    start_event: BaseEvent | None,
    returned_event: BaseEvent,
) -> EvidenceEntry | None:
    if not isinstance(returned_event.data, dict):
        return None

    model_id = returned_event.data.get("model_id") or (
        start_event.data.get("model_id") if start_event else None
    )
    if isinstance(model_id, str):
        model_id = model_id.strip() or None

    caller = (
        start_event.data.get("caller")
        if start_event and isinstance(start_event.data, dict)
        else None
    )
    if isinstance(caller, str):
        caller = caller.strip() or None

    is_error = returned_event.data.get("is_error")
    ok = (not bool(is_error)) if isinstance(is_error, bool) else None
    duration_ms = returned_event.data.get("duration_ms")
    error_kind = returned_event.data.get("error_kind")
    if isinstance(error_kind, str):
        error_kind = error_kind.strip() or None

    payload = _llm_payload(
        model_id=model_id if isinstance(model_id, str) else None,
        caller=caller if isinstance(caller, str) else None,
        duration_ms=duration_ms if isinstance(duration_ms, int) else None,
        is_error=is_error if isinstance(is_error, bool) else None,
        error_kind=error_kind if isinstance(error_kind, str) else None,
    )

    source_event_ids: list[str] = []
    if start_event is not None:
        source_event_ids.append(start_event.id)
    source_event_ids.append(returned_event.id)

    started_at = start_event.timestamp if start_event else returned_event.timestamp

    return EvidenceEntry(
        kind=EvidenceKind.LLM_CALL,
        ok=ok,
        started_at=started_at,
        ended_at=returned_event.timestamp,
        payload=payload,
        source_event_ids=tuple(source_event_ids),
    )


def _build_llm_entry_from_start_only(
    call_id: str,
    start_event: BaseEvent,
) -> EvidenceEntry | None:
    del call_id  # reserved for future correlation logging
    if not isinstance(start_event.data, dict):
        return None
    model_id = start_event.data.get("model_id")
    if isinstance(model_id, str):
        model_id = model_id.strip() or None
    caller = start_event.data.get("caller")
    if isinstance(caller, str):
        caller = caller.strip() or None
    payload = _llm_payload(
        model_id=model_id if isinstance(model_id, str) else None,
        caller=caller if isinstance(caller, str) else None,
        duration_ms=None,
        is_error=None,
        error_kind=None,
    )
    return EvidenceEntry(
        kind=EvidenceKind.LLM_CALL,
        ok=None,
        started_at=start_event.timestamp,
        ended_at=None,
        payload=payload,
        source_event_ids=(start_event.id,),
    )


def filter_events_for_ac(
    events: Sequence[BaseEvent],
    ac_id: str,
) -> tuple[BaseEvent, ...]:
    """Return events whose scope channels reference the given AC.

    Convenience helper for callers that hold a flat list of events and
    want the AC-scoped subset before calling :func:`normalize_events`.
    The match is multi-channel and considers, in priority order:

    1. ``event.data["ac_id"]``
    2. ``event.aggregate_id``
    3. ``event.data["execution_id"]``
    4. ``event.data["phase"]``

    Events with no scope tokens that match the target ``ac_id`` are
    excluded. See :func:`_event_scope_tokens` for the precise set of
    channels considered.
    """
    normalized = ac_id.strip()
    if not normalized:
        msg = "filter_events_for_ac requires a non-blank ac_id"
        raise ValueError(msg)
    return tuple(event for event in events if _event_matches_ac(event, normalized))


__all__ = [
    "JOURNAL_SCHEMA_VERSION",
    "EvidenceEntry",
    "EvidenceKind",
    "EvidenceManifest",
    "FrozenMapping",
    "IdentifierTuple",
    "filter_events_for_ac",
    "normalize_events",
]
