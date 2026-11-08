"""Equivalence and cost tests for the accepted tool-call correlation index.

``_accepted_tool_start_entries`` used to rescan the whole execution stream for
every Bash/Edit/Write/NotebookEdit start (accepted starts per call id,
conflicting completions, completions per call id, the id-less span and tail
checks).  It now answers those questions from one ``_index_accepted_tool_calls``
pass.  The pre-index implementation is kept below, verbatim apart from routing
its helpers through the module, as the reference every admit/veto decision is
compared against.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
import json
import random
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.events.base import BaseEvent
import ouroboros.harness.deliver_gate as dg
from ouroboros.harness.journal import EvidenceEntry, EvidenceKind

_EXECUTION_ID = "exec_1"
_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_TOOL_EVENT_TYPES = frozenset({"execution.tool.started", "execution.tool.completed"})


# --- Reference: the implementation before the index (origin/main 592c667f) ---------------


def _reference_accepted_tool_start_entries(
    events: Iterable[BaseEvent],
    *,
    scope_id: str,
    execution_id: str,
    retry_attempt: int | None,
    session_attempt_id: str | None,
) -> tuple[EvidenceEntry, ...]:
    """Project accepted-leaf tool dispatches into claim-independent entries.

    The accepted-leaf + exact-match + verifier-PASS preconditions are necessary
    but not sufficient for file mutation or command success: a failed Edit/Write
    or Bash call can still be followed by an overall successful assistant result.
    Mutation and command starts therefore require their own explicit success
    signal or one exact successful completion. Missing, failed, or ambiguous
    correlation is omitted fail-closed.
    """
    chronological = tuple(events)
    event_id_counts = Counter(event.id for event in chronological)
    idless_completion_by_start = dg._owned_idless_tool_completions(
        chronological,
        scope_id=scope_id,
        execution_id=execution_id,
        retry_attempt=retry_attempt,
        session_attempt_id=session_attempt_id,
        event_id_counts=event_id_counts,
    )
    owned_idless_completion_indexes = frozenset(idless_completion_by_start.values())
    entries: list[EvidenceEntry] = []
    for index, event in enumerate(chronological):
        if event.type != "execution.tool.started" or not dg._event_matches_scope(event, scope_id):
            continue
        if event_id_counts[event.id] != 1:
            continue
        data = event.data
        if retry_attempt is not None and data.get("retry_attempt") != retry_attempt:
            continue
        if session_attempt_id is not None and data.get("session_attempt_id") != session_attempt_id:
            continue
        if dg._event_has_conflicting_tool_call_ids(event):
            continue
        tool_name = data.get("tool_name") if isinstance(data, Mapping) else None
        tool_input = data.get("tool_input") if isinstance(data, Mapping) else None
        if not isinstance(tool_name, str) or not tool_name.strip():
            continue
        normalized_tool_name = tool_name.strip()
        completion: BaseEvent | None = None
        if normalized_tool_name in {"Bash", "Edit", "Write", "NotebookEdit"}:
            require_command_verdict = normalized_tool_name == "Bash"
            call_id = dg._event_tool_call_id(event)
            if call_id is not None:
                matching_starts = tuple(
                    candidate
                    for candidate in chronological
                    if candidate.type == "execution.tool.started"
                    and dg._event_matches_accepted_attempt(
                        candidate,
                        scope_id=scope_id,
                        execution_id=execution_id,
                        retry_attempt=retry_attempt,
                        session_attempt_id=session_attempt_id,
                    )
                    and dg._event_tool_call_id(candidate) == call_id
                )
                if len(matching_starts) != 1:
                    continue
            start_success = dg._event_has_explicit_tool_success(
                event,
                require_command_verdict=require_command_verdict,
            )
            completion, completion_veto = _reference_correlated_tool_completion(
                chronological,
                start_index=index,
                scope_id=scope_id,
                execution_id=execution_id,
                retry_attempt=retry_attempt,
                session_attempt_id=session_attempt_id,
                require_command_verdict=require_command_verdict,
                event_id_counts=event_id_counts,
                idless_completion_by_start=idless_completion_by_start,
                owned_idless_completion_indexes=owned_idless_completion_indexes,
            )
            if completion_veto or (completion is None and not start_success):
                continue
        normalized_input = dict(tool_input) if isinstance(tool_input, Mapping) else {}
        payload: dict[str, Any] = {
            "tool_name": normalized_tool_name,
            "accepted_leaf_dispatch": True,
            "execution_id": execution_id,
            "ac_id": scope_id,
        }
        accepted_call_id = dg._event_tool_call_id(event)
        if accepted_call_id is not None:
            payload["tool_call_id"] = accepted_call_id
        if retry_attempt is not None:
            payload["retry_attempt"] = retry_attempt
        if session_attempt_id is not None:
            payload["session_attempt_id"] = session_attempt_id
        if normalized_input:
            payload["args_preview"] = json.dumps(
                normalized_input,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        command = normalized_input.get("command")
        if isinstance(command, str) and command.strip():
            payload["command"] = command.strip()
        raw_path = next(
            (
                normalized_input[key]
                for key in ("file_path", "path", "notebook_path")
                if isinstance(normalized_input.get(key), str) and str(normalized_input[key]).strip()
            ),
            None,
        )
        if isinstance(raw_path, str):
            payload["file_path"] = raw_path.strip()
            relative_path = dg._event_workspace_relative_path(raw_path, data)
            if relative_path is not None:
                payload["workspace_relative_path"] = relative_path
        if normalized_tool_name == "Bash" and completion is not None and completion.id != event.id:
            command_artifacts = dg._command_artifacts_from_completion(
                completion,
                retry_attempt=retry_attempt,
                session_attempt_id=session_attempt_id,
            )
            if command_artifacts is not None:
                payload["command_artifacts"] = command_artifacts
        result_preview = dg._event_result_preview(event, completion)
        if result_preview is not None:
            payload["result_preview"] = result_preview
        entries.append(
            EvidenceEntry(
                kind=EvidenceKind.TOOL_INVOCATION,
                ok=True,
                started_at=event.timestamp,
                ended_at=completion.timestamp if completion is not None else event.timestamp,
                payload=payload,
                source_event_ids=(
                    (event.id, completion.id) if completion is not None else (event.id,)
                ),
            )
        )
    return tuple(entries)


def _reference_correlated_tool_completion(
    events: tuple[BaseEvent, ...],
    *,
    start_index: int,
    scope_id: str,
    execution_id: str,
    retry_attempt: int | None,
    session_attempt_id: str | None,
    require_command_verdict: bool,
    event_id_counts: Mapping[str, int],
    idless_completion_by_start: Mapping[int, int],
    owned_idless_completion_indexes: frozenset[int],
) -> tuple[BaseEvent | None, bool]:
    """Return an exact completion and whether failure/ambiguity vetoes the start."""
    start = events[start_index]
    if event_id_counts.get(start.id) != 1:
        return None, True
    start_data = start.data
    if not isinstance(start_data, Mapping):
        return None, True
    if dg._event_has_conflicting_tool_call_ids(start):
        return None, True
    start_tool = start_data.get("tool_name")
    if not isinstance(start_tool, str):
        return None, True
    start_call_id = dg._event_tool_call_id(start)

    if start_call_id is not None:
        matching_starts = tuple(
            candidate
            for candidate in events
            if candidate.type == "execution.tool.started"
            and not dg._event_has_conflicting_tool_call_ids(candidate)
            and dg._event_matches_accepted_attempt(
                candidate,
                scope_id=scope_id,
                execution_id=execution_id,
                retry_attempt=retry_attempt,
                session_attempt_id=session_attempt_id,
            )
            and dg._event_tool_call_id(candidate) == start_call_id
        )
        if len(matching_starts) != 1:
            return None, True
        if any(
            candidate.type == "execution.tool.completed"
            and dg._event_matches_accepted_attempt(
                candidate,
                scope_id=scope_id,
                execution_id=execution_id,
                retry_attempt=retry_attempt,
                session_attempt_id=session_attempt_id,
            )
            and dg._event_has_conflicting_tool_call_ids(candidate)
            and start_call_id in dg._event_tool_call_aliases(candidate)[0]
            for candidate in events
        ):
            return None, True
        matches = tuple(
            (candidate_index, candidate)
            for candidate_index, candidate in enumerate(events)
            if candidate.type == "execution.tool.completed"
            and dg._event_matches_accepted_attempt(
                candidate,
                scope_id=scope_id,
                execution_id=execution_id,
                retry_attempt=retry_attempt,
                session_attempt_id=session_attempt_id,
            )
            and dg._event_tool_call_id(candidate) == start_call_id
        )
        if not matches:
            return None, False
        if len(matches) != 1:
            return None, True
        completion_index, completion = matches[0]
        if completion_index <= start_index or event_id_counts.get(completion.id) != 1:
            return None, True
        for candidate_index, candidate in enumerate(
            events[start_index + 1 : completion_index],
            start=start_index + 1,
        ):
            if (
                candidate.type != "execution.tool.completed"
                or not dg._event_matches_accepted_attempt(
                    candidate,
                    scope_id=scope_id,
                    execution_id=execution_id,
                    retry_attempt=retry_attempt,
                    session_attempt_id=session_attempt_id,
                )
            ):
                continue
            if dg._event_tool_call_id(candidate) is not None:
                continue
            if candidate_index in owned_idless_completion_indexes:
                continue
            candidate_tool_value = (
                candidate.data.get("tool_name") if isinstance(candidate.data, Mapping) else None
            )
            candidate_tool = (
                candidate_tool_value.strip()
                if isinstance(candidate_tool_value, str) and candidate_tool_value.strip()
                else None
            )
            if candidate_tool in {None, start_tool.strip()}:
                # An id-less same-tool (or nameless) terminal could have closed
                # this named command.  Do not let a later matching completion
                # revive its authority, even for a crafted durable journal.
                return None, True
        if (
            not isinstance(completion.data, Mapping)
            or completion.data.get("tool_name") != start_tool
        ):
            return None, True
        if not dg._event_has_explicit_tool_success(
            completion,
            require_command_verdict=require_command_verdict,
        ):
            return None, True
        return completion, False

    # Legacy id-less streams keep one independent slot across concurrent named
    # peers. Only a uniquely owned id-less terminal may close the start.
    owned_completion_index = idless_completion_by_start.get(start_index)
    if owned_completion_index is not None:
        completion = events[owned_completion_index]
        if event_id_counts.get(completion.id) != 1 or not dg._event_has_explicit_tool_success(
            completion,
            require_command_verdict=require_command_verdict,
        ):
            return None, True
        return completion, False

    for candidate in events[start_index + 1 :]:
        if candidate.type not in {"execution.tool.started", "execution.tool.completed"}:
            continue
        if not dg._event_matches_accepted_attempt(
            candidate,
            scope_id=scope_id,
            execution_id=execution_id,
            retry_attempt=retry_attempt,
            session_attempt_id=session_attempt_id,
        ):
            continue
        aliases, _malformed = dg._event_tool_call_aliases(candidate)
        if not aliases:
            return None, True
    return None, False


# --- Randomized streams -------------------------------------------------------------------

_SCOPES = ("ac_1", "ac_2")
_CALL_IDS = tuple(f"call_{n}" for n in range(6))
_TOOLS: tuple[object, ...] = ("Bash", "Edit", "Write", "NotebookEdit", "Read", " Bash ", "", 7)
_MISSING = object()
_ATTEMPTS = ((0, "sa_0"), (1, "sa_1"), (1, "sa_0"))
# (retry_attempt, session_attempt_id) filters, including ones no event satisfies.
_FILTERS = (
    (None, None),
    (0, None),
    (1, None),
    (None, "sa_1"),
    (0, "sa_0"),
    (1, "sa_1"),
    (2, "sa_9"),
)
_ID_KEYS = ("tool_call_id", "tool_use_id", "call_id")


def _place_call_id(
    rng: random.Random,
    data: dict[str, Any],
    tool_result: dict[str, Any],
    call_id: str | None,
) -> None:
    """Attach correlation aliases in every supported spot, sometimes conflicting or malformed."""
    if call_id is not None:
        key = rng.choice(_ID_KEYS)
        spot = rng.random()
        if spot < 0.6:
            data[key] = call_id
        elif spot < 0.75:
            data["meta"] = {key: call_id}
        elif spot < 0.9:
            tool_result[key] = call_id
        else:
            tool_result.setdefault("meta", {})[key] = call_id
        roll = rng.random()
        other_key = rng.choice([k for k in _ID_KEYS if k != key])
        if roll < 0.08:
            data[other_key] = rng.choice(_CALL_IDS)  # agreeing duplicate or conflicting alias
        elif roll < 0.12:
            data[other_key] = rng.choice(["", "  ", 5, None])  # malformed beside a valid alias
        return
    roll = rng.random()
    if roll < 0.08:
        data[rng.choice(_ID_KEYS)] = rng.choice(["", "  ", 3, None])  # malformed, alias-less
    elif roll < 0.12:
        data["meta"] = "not-a-mapping"
    elif roll < 0.15:
        data["tool_result"] = ["not-a-mapping"]


def _tool_event_data(
    rng: random.Random,
    *,
    serial: int,
    scope: str,
    attempt: tuple[int, str],
    tool: object,
    call_id: str | None,
    completion: bool,
) -> dict[str, Any]:
    retry_attempt, session_attempt_id = attempt
    data: dict[str, Any] = {
        "execution_id": _EXECUTION_ID,
        "ac_id": scope,
        "session_scope_id": scope,
        "retry_attempt": retry_attempt,
        "attempt_number": retry_attempt + 1,
        "session_attempt_id": session_attempt_id,
    }
    if tool is not _MISSING:
        data["tool_name"] = tool
    if rng.random() < 0.05:
        data["execution_id"] = "exec_other"
    if rng.random() < 0.03:
        del data["execution_id"]
    if rng.random() < 0.05:
        data["parent_execution_id"] = rng.choice([_EXECUTION_ID, "exec_other"])
    if rng.random() < 0.04:
        data["attempt_number"] = rng.choice([0, 3, "1"])
    if rng.random() < 0.04:
        data[rng.choice(["ac_id", "session_scope_id"])] = rng.choice(["", "ac_other", 1])
    if rng.random() < 0.04:
        del data[rng.choice(["retry_attempt", "session_attempt_id", "attempt_number"])]
    tool_result: dict[str, Any] = {}
    _place_call_id(rng, data, tool_result, call_id)
    if completion:
        outcome = rng.random()
        if outcome < 0.5:
            data["is_error"] = False
            if rng.random() < 0.5:
                data["exit_code"] = 0
        elif outcome < 0.6:
            data["is_error"] = True
        elif outcome < 0.67:
            data["exit_code"] = rng.choice([0, 1, True, "0"])
        elif outcome < 0.74:
            tool_result["is_error"] = False
            tool_result.setdefault("meta", {})["exit_status"] = rng.choice([0, 0, 2])
        elif outcome < 0.8:
            data["status"] = rng.choice(["completed", "failed", "succeeded"])
        elif outcome < 0.85:
            data["runtime_event_type"] = rng.choice(["tool.completed", "tool.failed"])
        elif outcome < 0.9:
            data["success"] = rng.choice([True, False])
        data["stdout"] = f"output {serial}"
    else:
        data["tool_input"] = rng.choice(
            [
                {"command": "pytest -q"},
                {"file_path": "src/app.py"},
                {"notebook_path": "nb.ipynb"},
                {},
                "raw",
            ]
        )
        if rng.random() < 0.15:
            data["exit_code"] = 0  # start-level explicit success
        if rng.random() < 0.05:
            data["runtime"] = {"cwd": "/work"}
    if tool_result and "tool_result" not in data:
        data["tool_result"] = tool_result
    return data


def _random_stream(rng: random.Random, size: int) -> tuple[BaseEvent, ...]:
    """Interleave starts, completions and noise across scopes and attempts."""
    events: list[BaseEvent] = []
    open_calls: list[dict[str, Any]] = []
    clock = _T0

    def emit(event_type: str, scope: str, data: dict[str, Any]) -> None:
        nonlocal clock
        clock += timedelta(milliseconds=rng.choice([0, 0, 1, 7]))
        reuse_id = bool(events) and rng.random() < 0.03
        events.append(
            BaseEvent(
                id=rng.choice(events).id if reuse_id else f"evt_{len(events)}",
                type=event_type,
                timestamp=clock,
                aggregate_type="execution" if rng.random() < 0.95 else "session",
                aggregate_id=scope if rng.random() < 0.95 else _EXECUTION_ID,
                data=data,
            )
        )

    def new_call() -> dict[str, Any]:
        return {
            "scope": rng.choice(_SCOPES),
            "attempt": rng.choice(_ATTEMPTS),
            "tool": rng.choice(_TOOLS) if rng.random() < 0.95 else _MISSING,
            "call_id": rng.choice(_CALL_IDS) if rng.random() < 0.7 else None,
        }

    def emit_completion(call: dict[str, Any]) -> None:
        tool = call["tool"] if rng.random() < 0.85 else rng.choice((*_TOOLS, _MISSING))
        call_id = call["call_id"] if rng.random() < 0.9 else rng.choice((*_CALL_IDS, None))
        data = _tool_event_data(
            rng,
            serial=len(events),
            scope=call["scope"],
            attempt=call["attempt"],
            tool=tool,
            call_id=call_id,
            completion=True,
        )
        emit("execution.tool.completed", call["scope"], data)

    while len(events) < size:
        roll = rng.random()
        if open_calls and (roll < 0.45 or len(open_calls) > 4):
            call = open_calls.pop(rng.randrange(len(open_calls)))
            emit_completion(call)
            if rng.random() < 0.04:
                emit_completion(call)
        elif roll < 0.88:
            call = new_call()
            data = _tool_event_data(
                rng,
                serial=len(events),
                scope=call["scope"],
                attempt=call["attempt"],
                tool=call["tool"],
                call_id=call["call_id"],
                completion=False,
            )
            emit("execution.tool.started", call["scope"], data)
            if rng.random() < 0.9:
                open_calls.append(call)
        elif roll < 0.94:
            emit_completion(new_call())  # orphan completion
        else:
            scope = rng.choice(_SCOPES)
            emit(
                rng.choice(["workflow.progress.updated", "execution.tool.failed"]),
                scope,
                {"execution_id": _EXECUTION_ID, "ac_id": scope, "tool_name": "Bash"},
            )
    return tuple(events)


def _index_inputs(
    events: tuple[BaseEvent, ...],
    *,
    scope_id: str,
    retry_attempt: int | None,
    session_attempt_id: str | None,
) -> tuple[Counter[str], dict[int, int], dg._AcceptedToolCallIndex]:
    event_id_counts = Counter(event.id for event in events)
    idless = dg._owned_idless_tool_completions(
        events,
        scope_id=scope_id,
        execution_id=_EXECUTION_ID,
        retry_attempt=retry_attempt,
        session_attempt_id=session_attempt_id,
        event_id_counts=event_id_counts,
    )
    call_index = dg._index_accepted_tool_calls(
        events,
        scope_id=scope_id,
        execution_id=_EXECUTION_ID,
        retry_attempt=retry_attempt,
        session_attempt_id=session_attempt_id,
        owned_idless_completion_indexes=frozenset(idless.values()),
    )
    return event_id_counts, idless, call_index


def _compare_random_streams(seed: int, streams: int) -> Counter[str]:
    """Assert identical decisions on ``streams`` random streams; return outcome counts."""
    rng = random.Random(seed)
    outcomes: Counter[str] = Counter()
    for _ in range(streams):
        events = _random_stream(rng, rng.randint(1, 70))
        for scope_id in _SCOPES:
            for retry_attempt, session_attempt_id in _FILTERS:
                anchors: dict[str, Any] = {
                    "scope_id": scope_id,
                    "execution_id": _EXECUTION_ID,
                    "retry_attempt": retry_attempt,
                    "session_attempt_id": session_attempt_id,
                }
                expected = _reference_accepted_tool_start_entries(events, **anchors)
                assert dg._accepted_tool_start_entries(events, **anchors) == expected
                outcomes["admitted"] += len(expected)
                outcomes["admitted_with_completion"] += sum(
                    len(entry.source_event_ids) == 2 for entry in expected
                )

                event_id_counts, idless, call_index = _index_inputs(
                    events,
                    scope_id=scope_id,
                    retry_attempt=retry_attempt,
                    session_attempt_id=session_attempt_id,
                )
                for index, event in enumerate(events):
                    if event.type != "execution.tool.started":
                        continue
                    for require_command_verdict in (False, True):
                        old_completion, old_veto = _reference_correlated_tool_completion(
                            events,
                            start_index=index,
                            require_command_verdict=require_command_verdict,
                            event_id_counts=event_id_counts,
                            idless_completion_by_start=idless,
                            owned_idless_completion_indexes=frozenset(idless.values()),
                            **anchors,
                        )
                        new_completion, new_veto = dg._correlated_tool_completion(
                            events,
                            start_index=index,
                            require_command_verdict=require_command_verdict,
                            event_id_counts=event_id_counts,
                            idless_completion_by_start=idless,
                            call_index=call_index,
                        )
                        assert new_completion is old_completion
                        assert new_veto is old_veto
                        if old_completion is not None:
                            outcomes["completion"] += 1
                        else:
                            outcomes["veto" if old_veto else "no_completion"] += 1
    return outcomes


@pytest.mark.parametrize("seed", range(4))
def test_indexed_correlation_matches_reference_on_random_streams(seed: int) -> None:
    outcomes = _compare_random_streams(seed, streams=60)
    # The corpus must reach every outcome often, or equality above proves little.
    for outcome in ("admitted", "admitted_with_completion", "completion", "veto", "no_completion"):
        assert outcomes[outcome] >= 50, outcomes


# --- Hand-written edge cases --------------------------------------------------------------


def _tool_event(
    event_id: str,
    kind: str,
    *,
    at: int,
    tool: object = "Bash",
    cid: str | None = None,
    scope: str = "ac_1",
    **extra: Any,
) -> BaseEvent:
    data: dict[str, Any] = {
        "execution_id": _EXECUTION_ID,
        "ac_id": scope,
        "retry_attempt": 0,
        "session_attempt_id": "sa_0",
    }
    if tool is not _MISSING:
        data["tool_name"] = tool
    if cid is not None:
        data["tool_call_id"] = cid
    if kind == "started":
        data["tool_input"] = {"command": "pytest -q"}
    else:
        data.update({"is_error": False, "exit_code": 0, "stdout": f"output of {event_id}"})
    data.update(extra)
    return BaseEvent(
        id=event_id,
        type=f"execution.tool.{kind}",
        timestamp=_T0 + timedelta(seconds=at),
        aggregate_type="execution",
        aggregate_id=scope,
        data=data,
    )


def _s(event_id: str, at: int, **kwargs: Any) -> BaseEvent:
    return _tool_event(event_id, "started", at=at, **kwargs)


def _c(event_id: str, at: int, **kwargs: Any) -> BaseEvent:
    return _tool_event(event_id, "completed", at=at, **kwargs)


_EDGE_CASES: dict[str, tuple[list[BaseEvent], tuple[tuple[str, ...], ...]]] = {
    "unique call id": ([_s("s1", 0, cid="c1"), _c("d1", 1, cid="c1")], (("s1", "d1"),)),
    "second accepted start with the same id": (
        [_s("s1", 0, cid="c1"), _s("s2", 1, cid="c1"), _c("d1", 2, cid="c1")],
        (),
    ),
    "same id started by another attempt": (
        [
            _s("s1", 0, cid="c1"),
            _s("s2", 1, cid="c1", retry_attempt=1),
            _c("d1", 2, cid="c1"),
        ],
        (("s1", "d1"),),
    ),
    "conflicting completion names the id": (
        [
            _s("s1", 0, cid="c1"),
            _c("d1", 1, cid="c1"),
            _c("dx", 2, cid="c1", tool_use_id="c2"),
        ],
        (),
    ),
    "malformed completion names the id": (
        [
            _s("s1", 0, cid="c1"),
            _c("d1", 1, cid="c1"),
            _c("dx", 2, cid="c1", call_id=""),
        ],
        (),
    ),
    "conflicting completion in another scope": (
        [
            _s("s1", 0, cid="c1"),
            _c("d1", 1, cid="c1"),
            _c("dx", 2, cid="c1", tool_use_id="c2", scope="ac_2"),
        ],
        (("s1", "d1"),),
    ),
    "two completions for one id": (
        [_s("s1", 0, cid="c1"), _c("d1", 1, cid="c1"), _c("d2", 2, cid="c1")],
        (),
    ),
    "no completion and no command verdict": ([_s("s1", 0, cid="c1")], ()),
    "no completion but explicit start success": (
        [_s("s1", 0, cid="c1", exit_code=0)],
        (("s1",),),
    ),
    "completion before its start": ([_c("d1", 0, cid="c1"), _s("s1", 1, cid="c1")], ()),
    "unowned id-less same-tool completion inside the span": (
        [_s("s1", 0, cid="c1"), _c("dx", 1), _c("d1", 2, cid="c1")],
        (),
    ),
    "unowned nameless id-less completion inside the span": (
        [_s("s1", 0, cid="c1"), _c("dx", 1, tool=_MISSING), _c("d1", 2, cid="c1")],
        (),
    ),
    "unowned id-less other-tool completion inside the span": (
        [_s("s1", 0, cid="c1"), _c("dx", 1, tool="Edit"), _c("d1", 2, cid="c1")],
        (("s1", "d1"),),
    ),
    "owned id-less completion inside the span": (
        [_s("s1", 0, cid="c1"), _s("sy", 1), _c("dy", 2), _c("d1", 3, cid="c1")],
        (("s1", "d1"), ("sy", "dy")),
    ),
    "unowned id-less completion right after the completion": (
        [_s("s1", 0, cid="c1"), _c("d1", 1, cid="c1"), _c("dx", 2)],
        (("s1", "d1"),),
    ),
    "unowned id-less completion in another scope inside the span": (
        [_s("s1", 0, cid="c1"), _c("dx", 1, scope="ac_2"), _c("d1", 2, cid="c1")],
        (("s1", "d1"),),
    ),
    "id-less start followed by another id-less start": (
        [_s("sy", 0, exit_code=0), _s("sz", 1, exit_code=0)],
        (("sz",),),
    ),
    "id-less start followed only by id-bearing events": (
        [_s("sy", 0, exit_code=0), _s("s1", 1, cid="c1"), _c("d1", 2, cid="c1")],
        (("sy",), ("s1", "d1")),
    ),
    "id-less start followed by a malformed alias-less completion": (
        [_s("sy", 0, exit_code=0), _c("dx", 1, call_id="")],
        (),
    ),
}


@pytest.mark.parametrize("case", sorted(_EDGE_CASES))
def test_edge_case_decisions_match_reference(case: str) -> None:
    events, expected_sources = _EDGE_CASES[case]
    stream = tuple(events)
    anchors: dict[str, Any] = {
        "scope_id": "ac_1",
        "execution_id": _EXECUTION_ID,
        "retry_attempt": 0,
        "session_attempt_id": "sa_0",
    }
    expected = _reference_accepted_tool_start_entries(stream, **anchors)
    actual = dg._accepted_tool_start_entries(stream, **anchors)
    assert actual == expected
    assert tuple(entry.source_event_ids for entry in actual) == expected_sources


# --- Cost ---------------------------------------------------------------------------------


def _sequential_stream(*, n_acs: int, calls_per_ac: int) -> tuple[BaseEvent, ...]:
    """Several ACs of one execution, each with id-bearing and id-less sequential calls."""
    events: list[BaseEvent] = []
    for ac in range(n_acs):
        scope = f"ac_{ac + 1}"
        for call in range(calls_per_ac):
            tool = "Bash" if call % 2 else "Edit"
            call_id = f"call_{ac}_{call}" if call % 5 else None
            at = len(events)
            events.append(_s(f"s_{ac}_{call}", at, tool=tool, cid=call_id, scope=scope))
            events.append(_c(f"d_{ac}_{call}", at + 1, tool=tool, cid=call_id, scope=scope))
        events.append(_s(f"tail_{ac}", len(events), scope=scope, exit_code=0))
    return tuple(events)


def _count_attempt_matcher_calls(run: Any) -> int:
    with patch.object(
        dg,
        "_event_matches_accepted_attempt",
        wraps=dg._event_matches_accepted_attempt,
    ) as spy:
        run()
    return spy.call_count


@pytest.mark.parametrize("n_acs", [2, 4, 8])
def test_attempt_matcher_runs_twice_per_tool_event_not_per_start(n_acs: int) -> None:
    calls_per_ac = 20
    events = _sequential_stream(n_acs=n_acs, calls_per_ac=calls_per_ac)
    tool_events = sum(event.type in _TOOL_EVENT_TYPES for event in events)
    anchors: dict[str, Any] = {
        "scope_id": "ac_1",
        "execution_id": _EXECUTION_ID,
        "retry_attempt": 0,
        "session_attempt_id": "sa_0",
    }
    entries: list[tuple[EvidenceEntry, ...]] = []

    indexed_calls = _count_attempt_matcher_calls(
        lambda: entries.append(dg._accepted_tool_start_entries(events, **anchors))
    )
    reference_calls = _count_attempt_matcher_calls(
        lambda: entries.append(_reference_accepted_tool_start_entries(events, **anchors))
    )

    assert entries[0] == entries[1]
    # Every call of ac_1 is admitted; the trailing id-less start is vetoed by
    # nothing and admitted on its own exit code.
    assert len(entries[0]) == calls_per_ac + 1
    # One pass for id-less ownership, one for the index: independent of starts.
    assert indexed_calls == 2 * tool_events
    # The reference rescanned the stream for each of the 16 id-bearing starts.
    id_bearing_starts = sum(1 for call in range(calls_per_ac) if call % 5)
    assert reference_calls > id_bearing_starts * tool_events


async def test_loader_evaluates_attempt_matcher_twice_per_tool_event() -> None:
    events = list(_sequential_stream(n_acs=4, calls_per_ac=20))
    tool_events = sum(event.type in _TOOL_EVENT_TYPES for event in events)

    class _Store:
        async def query_execution_related_events(
            self, execution_id: str, event_type: str | None = None, limit: int | None = 50
        ) -> list[BaseEvent]:
            assert execution_id == _EXECUTION_ID
            return events

        async def query_session_related_events(self, *args: Any, **kwargs: Any) -> list[BaseEvent]:
            raise AssertionError("session query not expected")

    manifests = []

    async def load() -> None:
        manifests.append(
            await dg.load_ac_evidence_manifest(
                _Store(),
                ac_id="ac_1",
                execution_id=_EXECUTION_ID,
                admit_accepted_tool_starts=True,
                accepted_retry_attempt=0,
                accepted_session_attempt_id="sa_0",
            )
        )

    with patch.object(
        dg,
        "_event_matches_accepted_attempt",
        wraps=dg._event_matches_accepted_attempt,
    ) as spy:
        await load()

    assert spy.call_count == 2 * tool_events
    admitted = [
        entry
        for entry in manifests[0].entries
        if entry.kind is EvidenceKind.TOOL_INVOCATION
        and entry.payload.get("accepted_leaf_dispatch")
    ]
    assert len(admitted) == 21
