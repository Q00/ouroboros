"""Projection builder over the EventStore.

This module is the second slice of issue #946. It walks an ordered
sequence of :class:`ouroboros.events.base.BaseEvent` records and
produces the public projection vocabulary delivered by
:mod:`ouroboros.harness.projection`. The builder is intentionally
small in this PR — it covers the substrate (one builder class plus the
event families already emitted by the canonical I/O recorder) and
leaves CLI / MCP query surfaces and richer event-family coverage to
follow-up PRs.

Recognized event families in PR-1b:

* ``tool.call.started`` / ``tool.call.returned`` — paired by ``call_id``
  into a :class:`StepRecord` of kind :attr:`StepKind.TOOL_CALL`.
  ``Bash`` tool calls are classified as :attr:`StepKind.SHELL_COMMAND`.
* ``llm.call.requested`` / ``llm.call.returned`` — paired by ``call_id``
  into a :class:`StepRecord` of kind :attr:`StepKind.MODEL_CALL`.
* Stage / verdict events are not mapped yet. The builder produces a
  single default :class:`StageRecord` of kind
  :attr:`StageKind.EXECUTE` that owns every step; richer stage
  detection is deferred to a future PR.

The builder is a **pure read** transformation: it never persists the
records and never mutates the events it walks.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from ouroboros.events.base import BaseEvent
from ouroboros.harness.projection import (
    PROJECTION_SCHEMA_VERSION,
    RunRecord,
    StageKind,
    StageRecord,
    StepKind,
    StepRecord,
)

_TOOL_STARTED = "tool.call.started"
_TOOL_RETURNED = "tool.call.returned"
_LLM_REQUESTED = "llm.call.requested"
_LLM_RETURNED = "llm.call.returned"

_SHELL_TOOL_NAMES = frozenset({"Bash"})


@dataclass(frozen=True, slots=True)
class ProjectionBuildResult:
    """Bundle of records produced from a single event sweep.

    Attributes:
        run: Top-level :class:`RunRecord`.
        stages: Stage records owned by the run.
        steps: Step records owned by the stages.
    """

    run: RunRecord
    stages: tuple[StageRecord, ...]
    steps: tuple[StepRecord, ...]


class ProjectionBuilder:
    """Walk events and emit the projection record bundle.

    The builder is constructed with a ``seed_id`` (and optional goal
    text) and accepts events incrementally via :meth:`add_event`, or in
    bulk via :meth:`add_events`. :meth:`build` finalizes the records.

    The same builder instance can be replayed multiple times: each call
    to :meth:`build` produces a fresh record bundle reflecting the
    events ingested so far. The builder does not deduplicate replayed
    events — callers must ensure each event is fed once per build.
    """

    def __init__(
        self,
        *,
        seed_id: str,
        goal: str = "",
        run_id: str | None = None,
        stage_id: str | None = None,
        source_key: str | None = None,
    ) -> None:
        if not seed_id.strip():
            msg = "ProjectionBuilder requires a non-blank seed_id"
            raise ValueError(msg)
        self._seed_id = seed_id.strip()
        self._goal = goal
        self._run_id_override = run_id.strip() if run_id and run_id.strip() else None
        self._stage_id_override = stage_id.strip() if stage_id and stage_id.strip() else None
        self._source_key = source_key.strip() if source_key and source_key.strip() else None
        self._tool_started: OrderedDict[str, BaseEvent] = OrderedDict()
        self._llm_started: OrderedDict[str, BaseEvent] = OrderedDict()
        self._steps: OrderedDict[str, StepRecord] = OrderedDict()
        self._identity_events: list[BaseEvent] = []
        self._idle_started_at = datetime.now(UTC)
        self._first_event_at: datetime | None = None
        self._last_event_at: datetime | None = None

    # -- public API -----------------------------------------------------

    def add_events(self, events: Iterable[BaseEvent]) -> ProjectionBuilder:
        """Ingest a batch of events. Returns self for chaining."""
        for event in events:
            self.add_event(event)
        return self

    def add_event(self, event: BaseEvent) -> ProjectionBuilder:
        """Ingest a single event. Returns self for chaining."""
        self._update_timestamps(event)
        self._identity_events.append(event)

        if event.type == _TOOL_STARTED:
            call_id = _extract_call_id(event)
            if call_id is not None:
                self._tool_started[call_id] = event
                key = _slot_key("tool", call_id)
                self._steps[key] = _step_from_start_only(
                    call_id=call_id,
                    start_event=event,
                    run_id="run_placeholder",
                    stage_id="stage_placeholder",
                    kind=_tool_kind(event),
                    family="tool",
                )
            return self

        if event.type == _TOOL_RETURNED:
            self._handle_tool_returned(event)
            return self

        if event.type == _LLM_REQUESTED:
            call_id = _extract_call_id(event)
            if call_id is not None:
                self._llm_started[call_id] = event
                key = _slot_key("llm", call_id)
                self._steps[key] = _step_from_start_only(
                    call_id=call_id,
                    start_event=event,
                    run_id="run_placeholder",
                    stage_id="stage_placeholder",
                    kind=StepKind.MODEL_CALL,
                    family="llm",
                )
            return self

        if event.type == _LLM_RETURNED:
            self._handle_llm_returned(event)
            return self

        # Other event types are ignored in PR-1b; they will be mapped
        # in follow-up PRs alongside their dedicated kinds.
        return self

    def build(self) -> ProjectionBuildResult:
        """Finalize the record bundle from ingested events.

        Repeated calls derive ``run_id`` / ``stage_id`` from the current
        source key so incremental builds converge with one-shot replays
        over the same final event set.
        """
        source_key = self._source_key or _derive_projection_source_key(
            self._identity_events,
            seed_id=self._seed_id,
        )
        run_id = self._run_id_override or _stable_run_id(source_key)
        stage_id = self._stage_id_override or _stable_stage_id(run_id, StageKind.EXECUTE)

        started_at = self._first_event_at or self._idle_started_at
        ended_at = self._last_event_at

        steps_for_stage = tuple(
            _rewrite_step_identity(
                step,
                step_id=_stable_step_id(source_key, *_slot_parts(slot_key)),
                run_id=run_id,
                stage_id=stage_id,
            )
            for slot_key, step in self._steps.items()
        )

        stage = StageRecord(
            schema_version=PROJECTION_SCHEMA_VERSION,
            stage_id=stage_id,
            run_id=run_id,
            kind=StageKind.EXECUTE,
            started_at=started_at,
            ended_at=ended_at,
            step_ids=tuple(step.step_id for step in steps_for_stage),
        )

        run = RunRecord(
            schema_version=PROJECTION_SCHEMA_VERSION,
            run_id=run_id,
            seed_id=self._seed_id,
            goal=self._goal,
            started_at=started_at,
            ended_at=ended_at,
            stage_ids=(stage_id,),
        )

        return ProjectionBuildResult(
            run=run,
            stages=(stage,),
            steps=steps_for_stage,
        )

    # -- internals ------------------------------------------------------

    def _update_timestamps(self, event: BaseEvent) -> None:
        if self._first_event_at is None or event.timestamp < self._first_event_at:
            self._first_event_at = event.timestamp
        if self._last_event_at is None or event.timestamp > self._last_event_at:
            self._last_event_at = event.timestamp

    def _handle_tool_returned(self, returned_event: BaseEvent) -> None:
        call_id = _extract_call_id(returned_event)
        if call_id is None:
            return
        start_event = self._tool_started.pop(call_id, None)
        kind = _tool_kind(start_event or returned_event)
        tool_name = _extract_tool_name(start_event or returned_event)
        if not tool_name:
            return

        source_event_ids = tuple(
            event.id for event in (start_event, returned_event) if event is not None
        )
        if not source_event_ids:
            return

        is_error = _safe_bool(returned_event.data.get("is_error"))
        ok = (not is_error) if is_error is not None else None

        key = _slot_key("tool", call_id)
        previous = self._steps.get(key)
        step = StepRecord(
            schema_version=PROJECTION_SCHEMA_VERSION,
            step_id=previous.step_id
            if previous is not None
            else _stable_step_id("pending", "tool", call_id),
            run_id="run_placeholder",  # rewritten in build()
            stage_id="stage_placeholder",
            kind=kind,
            name=tool_name,
            ac_id=_extract_ac_id(start_event or returned_event),
            started_at=(start_event or returned_event).timestamp,
            ended_at=returned_event.timestamp,
            ok=ok,
            source_event_ids=source_event_ids,
            metadata=_tool_step_metadata(start_event, returned_event),
        )
        self._steps[key] = step

    def _handle_llm_returned(self, returned_event: BaseEvent) -> None:
        call_id = _extract_call_id(returned_event)
        if call_id is None:
            return
        start_event = self._llm_started.pop(call_id, None)
        model_id = _extract_model_id(returned_event) or _extract_model_id(start_event)
        if not model_id:
            return

        source_event_ids = tuple(
            event.id for event in (start_event, returned_event) if event is not None
        )
        if not source_event_ids:
            return

        is_error = _safe_bool(returned_event.data.get("is_error"))
        ok = (not is_error) if is_error is not None else None

        key = _slot_key("llm", call_id)
        previous = self._steps.get(key)
        step = StepRecord(
            schema_version=PROJECTION_SCHEMA_VERSION,
            step_id=previous.step_id
            if previous is not None
            else _stable_step_id("pending", "llm", call_id),
            run_id="run_placeholder",  # rewritten in build()
            stage_id="stage_placeholder",
            kind=StepKind.MODEL_CALL,
            name=model_id,
            ac_id=_extract_ac_id(start_event or returned_event),
            started_at=(start_event or returned_event).timestamp,
            ended_at=returned_event.timestamp,
            ok=ok,
            source_event_ids=source_event_ids,
            metadata=_llm_step_metadata(start_event, returned_event),
        )
        self._steps[key] = step


# ---------------------------------------------------------------------------
# Convenience entry points + helpers
# ---------------------------------------------------------------------------


def build_projection(
    events: Sequence[BaseEvent],
    *,
    seed_id: str,
    goal: str = "",
    source_key: str | None = None,
) -> ProjectionBuildResult:
    """One-shot projection from a sequence of events."""
    return (
        ProjectionBuilder(seed_id=seed_id, goal=goal, source_key=source_key)
        .add_events(events)
        .build()
    )


def _extract_call_id(event: BaseEvent) -> str | None:
    if not isinstance(event.data, dict):
        return None
    value = event.data.get("call_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _extract_tool_name(event: BaseEvent | None) -> str | None:
    if event is None or not isinstance(event.data, dict):
        return None
    value = event.data.get("tool_name")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _extract_model_id(event: BaseEvent | None) -> str | None:
    if event is None or not isinstance(event.data, dict):
        return None
    value = event.data.get("model_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _extract_ac_id(event: BaseEvent | None) -> str | None:
    if event is None or not isinstance(event.data, dict):
        return None
    value = event.data.get("ac_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _tool_kind(event: BaseEvent | None) -> StepKind:
    tool_name = _extract_tool_name(event)
    if tool_name in _SHELL_TOOL_NAMES:
        return StepKind.SHELL_COMMAND
    return StepKind.TOOL_CALL


def _safe_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _derive_projection_source_key(events: Sequence[BaseEvent], *, seed_id: str) -> str:
    """Return the deterministic identity source for a projected run.

    Projection records are rebuildable read-models over the journal, so
    run/stage IDs must not depend on object construction time. Prefer an
    explicit execution aggregate when the event slice has exactly one,
    then a single session aggregate, then a stable digest of the event
    slice. Empty synthetic projections fall back to the seed id.
    """
    execution_ids = _event_scope_ids(events, scope="execution")
    if len(execution_ids) == 1:
        return f"execution:{next(iter(execution_ids))}"

    session_ids = _event_scope_ids(events, scope="session")
    if len(session_ids) == 1:
        return f"session:{next(iter(session_ids))}"

    if events:
        digest = uuid5(
            NAMESPACE_URL,
            "ouroboros:harness:projection:events:"
            + "|".join(
                _event_identity_part(event) for event in sorted(events, key=_event_sort_key)
            ),
        ).hex[:12]
        return f"events:{digest}"

    return f"seed:{seed_id}"


def _event_scope_ids(events: Sequence[BaseEvent], *, scope: str) -> frozenset[str]:
    values: set[str] = set()
    data_key = f"{scope}_id"
    for event in events:
        if event.aggregate_type == scope and event.aggregate_id.strip():
            values.add(event.aggregate_id.strip())
        if isinstance(event.data, dict):
            value = event.data.get(data_key)
            if isinstance(value, str) and value.strip():
                values.add(value.strip())
    return frozenset(values)


def _event_sort_key(event: BaseEvent) -> tuple[datetime, str]:
    return (event.timestamp, event.id)


def _event_identity_part(event: BaseEvent) -> str:
    return (
        f"{event.timestamp.isoformat()}::{event.id}::"
        f"{event.aggregate_type}:{event.aggregate_id}::{event.type}"
    )


def _stable_run_id(source_key: str) -> str:
    digest = uuid5(NAMESPACE_URL, f"ouroboros:harness:run:{source_key}").hex[:12]
    return f"run_{digest}"


def _stable_stage_id(run_id: str, kind: StageKind) -> str:
    digest = uuid5(NAMESPACE_URL, f"ouroboros:harness:stage:{run_id}:{kind.value}").hex[:12]
    return f"stage_{digest}"


def _tool_step_metadata(
    start_event: BaseEvent | None,
    returned_event: BaseEvent,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if start_event is not None and isinstance(start_event.data, dict):
        preview = start_event.data.get("args_preview")
        if isinstance(preview, str) and preview:
            metadata["args_preview"] = preview
    if isinstance(returned_event.data, dict):
        result_preview = returned_event.data.get("result_preview")
        if isinstance(result_preview, str) and result_preview:
            metadata["result_preview"] = result_preview
        duration = returned_event.data.get("duration_ms")
        if isinstance(duration, int):
            metadata["duration_ms"] = duration
        error_kind = returned_event.data.get("error_kind")
        if isinstance(error_kind, str) and error_kind:
            metadata["error_kind"] = error_kind
    return metadata


def _llm_step_metadata(
    start_event: BaseEvent | None,
    returned_event: BaseEvent,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if start_event is not None and isinstance(start_event.data, dict):
        caller = start_event.data.get("caller")
        if isinstance(caller, str) and caller:
            metadata["caller"] = caller
    if isinstance(returned_event.data, dict):
        duration = returned_event.data.get("duration_ms")
        if isinstance(duration, int):
            metadata["duration_ms"] = duration
        error_kind = returned_event.data.get("error_kind")
        if isinstance(error_kind, str) and error_kind:
            metadata["error_kind"] = error_kind
    return metadata


def _slot_key(family: str, call_id: str) -> str:
    return f"{family}:{call_id}"


def _stable_step_id(source_key: str, family: str, call_id: str) -> str:
    digest = uuid5(
        NAMESPACE_URL,
        f"ouroboros:harness:step:{source_key}:{family}:{call_id}",
    ).hex[:12]
    return f"step_{family}_{digest}"


def _slot_parts(slot_key: str) -> tuple[str, str]:
    family, call_id = slot_key.split(":", 1)
    return family, call_id


def _rewrite_step_identity(
    step: StepRecord,
    *,
    step_id: str,
    run_id: str,
    stage_id: str,
) -> StepRecord:
    return StepRecord(
        schema_version=step.schema_version,
        step_id=step_id,
        run_id=run_id,
        stage_id=stage_id,
        kind=step.kind,
        name=step.name,
        ac_id=step.ac_id,
        started_at=step.started_at,
        ended_at=step.ended_at,
        ok=step.ok,
        source_event_ids=step.source_event_ids,
        legacy_inferred=step.legacy_inferred,
        artifact_ids=step.artifact_ids,
        metadata=step.metadata,
    )


def _step_from_start_only(
    *,
    call_id: str,
    start_event: BaseEvent,
    run_id: str,
    stage_id: str,
    kind: StepKind,
    family: str,
) -> StepRecord:
    name = (
        _extract_tool_name(start_event)
        if kind in (StepKind.TOOL_CALL, StepKind.SHELL_COMMAND)
        else _extract_model_id(start_event)
    ) or kind.value
    metadata: dict[str, Any] = {}
    if isinstance(start_event.data, dict):
        preview = start_event.data.get("args_preview")
        if isinstance(preview, str) and preview:
            metadata["args_preview"] = preview
    return StepRecord(
        schema_version=PROJECTION_SCHEMA_VERSION,
        step_id=_stable_step_id("pending", family, call_id),
        run_id=run_id,
        stage_id=stage_id,
        kind=kind,
        name=name,
        ac_id=_extract_ac_id(start_event),
        started_at=start_event.timestamp,
        ended_at=None,
        ok=None,
        source_event_ids=(start_event.id,),
        metadata=metadata,
    )


__all__ = [
    "ProjectionBuildResult",
    "ProjectionBuilder",
    "build_projection",
]
