"""The controller's own numbered record of the shell calls a worker made.

The per-criterion verifier used to match the command strings a worker wrote in
its evidence record against the runtime transcript, and every new command shape
needed another matching rule. The evidence turn removes that matching: after
the worker finishes, the controller shows it this ledger (one numbered row per
recorded shell call, with the command and the result the runtime recorded) and
the worker cites rows by number. The controller then reads its own row; the
worker's text is never compared with the transcript.

A row's result comes from structured runtime data only:

- an integer exit status the runtime recorded for the call or for a completion
  correlated with it (Codex: ``exit_code`` / ``meta.exit_status``); every
  record that states one must agree on 0 for the call to have passed;
- for a runtime whose shell result carries no exit status (Claude), the
  structured ``is_error`` bit of the correlated tool result, when the caller
  says this runtime's ``is_error`` is its exit verdict; ``is_error: false`` is
  a pass. The text of a result (``Exit code 3``) is never read.

Anything else, including an ambiguous correlation, is ``UNKNOWN``: a call with
no recorded result can be cited as run by nobody, and never as passing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
import os

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.claims import (
    _runtime_message_command_values,
    _runtime_message_effective_cwd,
    _runtime_message_has_conflicting_tool_call_ids,
    _runtime_message_is_tool_completion,
    _runtime_message_tool_call_id,
    _runtime_message_tool_call_ids,
)

SHELL_TOOL_NAME = "Bash"


class CallResult(StrEnum):
    """The result the runtime recorded for one shell call."""

    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ResultSource(StrEnum):
    """Which structured field decided a row's result."""

    EXIT_STATUS = "exit_status"
    IS_ERROR = "is_error"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """One shell call as the controller recorded it.

    ``number`` is the 1-based position among the transcript's shell calls and
    is what the worker cites; ``index`` is the call's position in the message
    list. ``exit_status`` is set only when ``source`` is ``EXIT_STATUS``.
    ``in_workspace`` is False when the call ran outside the task workspace.
    """

    number: int
    index: int
    command: str
    result: CallResult
    source: ResultSource
    exit_status: int | None
    in_workspace: bool

    def result_label(self) -> str:
        """The recorded result as the evidence turn shows it."""
        if self.source is ResultSource.EXIT_STATUS:
            return f"exit {self.exit_status}"
        if self.source is ResultSource.IS_ERROR:
            return f"is_error: {'false' if self.result is CallResult.PASSED else 'true'}"
        return "no recorded result"


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _recorded_exit_statuses(message: AgentMessage) -> tuple[list[int], bool]:
    """Integer exit statuses a message records, and whether one is malformed.

    Only the authoritative keys count: ``exit_code`` on the message or its
    ``tool_result`` and ``meta.exit_status`` on either. The audit-only
    ``reported_*`` copies a runtime keeps for an unknown verdict never count.
    """
    containers: list[dict[str, object]] = [message.data]
    tool_result = message.data.get("tool_result")
    if isinstance(tool_result, dict):
        containers.append(tool_result)
    statuses: list[int] = []
    malformed = False
    for container in containers:
        for raw in (
            container.get("exit_code", None),
            (container.get("meta") or {}).get("exit_status", None)
            if isinstance(container.get("meta"), dict)
            else None,
        ):
            if raw is None:
                continue
            value = _integer(raw)
            if value is None:
                malformed = True
            else:
                statuses.append(value)
    return statuses, malformed


def _recorded_is_error(message: AgentMessage) -> bool | None:
    """The structured ``is_error`` bit of a tool result, or None when absent or mixed."""
    values: list[object] = []
    if "is_error" in message.data:
        values.append(message.data["is_error"])
    tool_result = message.data.get("tool_result")
    if isinstance(tool_result, dict) and "is_error" in tool_result:
        values.append(tool_result["is_error"])
    if not values or any(not isinstance(value, bool) for value in values):
        return None
    if len(set(values)) != 1:
        return None
    first = values[0]
    return first if isinstance(first, bool) else None


def _correlated_records(
    messages: Sequence[AgentMessage], index: int
) -> tuple[AgentMessage, ...] | None:
    """The call at ``index`` and every completion correlated with it, or None if ambiguous.

    Correlation is by tool-call id; an id-less call takes the next completion
    before another call (the rule ``command_replay.transcript_exit_status``
    uses). A correlation id carried with conflicting aliases is ambiguous.
    """
    call = messages[index]
    if _runtime_message_has_conflicting_tool_call_ids(call):
        return None
    records: list[AgentMessage] = [call]
    call_id = _runtime_message_tool_call_id(call)
    if call_id is not None:
        starts = 0
        for candidate in messages:
            if call_id not in _runtime_message_tool_call_ids(candidate):
                continue
            if _runtime_message_has_conflicting_tool_call_ids(candidate):
                return None
            if _runtime_message_is_tool_completion(candidate):
                records.append(candidate)
            elif candidate.tool_name is not None:
                starts += 1
        if starts != 1:
            return None
        return tuple(records)
    for candidate in messages[index + 1 :]:
        if _runtime_message_is_tool_completion(candidate):
            if _runtime_message_tool_call_id(candidate) is not None:
                return None
            records.append(candidate)
            break
        if candidate.tool_name is not None:
            break
    return tuple(records)


def _recorded_result(
    messages: Sequence[AgentMessage],
    index: int,
    *,
    is_error_is_exit_verdict: bool,
) -> tuple[CallResult, ResultSource, int | None]:
    records = _correlated_records(messages, index)
    if records is None:
        return CallResult.UNKNOWN, ResultSource.NONE, None
    statuses: list[int] = []
    for record in records:
        found, malformed = _recorded_exit_statuses(record)
        if malformed:
            return CallResult.UNKNOWN, ResultSource.NONE, None
        statuses.extend(found)
    if statuses:
        nonzero = [status for status in statuses if status != 0]
        status = nonzero[0] if nonzero else 0
        result = CallResult.PASSED if status == 0 else CallResult.FAILED
        return result, ResultSource.EXIT_STATUS, status
    if not is_error_is_exit_verdict:
        return CallResult.UNKNOWN, ResultSource.NONE, None
    bits = [
        bit
        for record in records[1:]
        if _runtime_message_is_tool_completion(record)
        and (bit := _recorded_is_error(record)) is not None
    ]
    if not bits or len(set(bits)) != 1:
        return CallResult.UNKNOWN, ResultSource.NONE, None
    return (
        CallResult.FAILED if bits[0] else CallResult.PASSED,
        ResultSource.IS_ERROR,
        None,
    )


def _in_workspace(message: AgentMessage, task_cwd: str | None) -> bool:
    if task_cwd is None:
        return False
    cwd = _runtime_message_effective_cwd(message, task_cwd=task_cwd)
    return cwd is not None and os.path.realpath(cwd) == os.path.realpath(task_cwd)


def build_call_ledger(
    messages: Sequence[AgentMessage],
    *,
    task_cwd: str | None,
    is_error_is_exit_verdict: bool,
) -> tuple[RecordedCall, ...]:
    """Number every shell call the transcript recorded, in transcript order.

    A call whose recorded command is missing or ambiguous (two different
    command values) is still numbered, with an empty command and an unknown
    result, so numbering never depends on how a call is parsed.
    """
    calls: list[RecordedCall] = []
    for index, message in enumerate(messages):
        if message.tool_name != SHELL_TOOL_NAME or _runtime_message_is_tool_completion(message):
            continue
        values = _runtime_message_command_values(message)
        command = values[0] if len(values) == 1 else ""
        if command:
            result, source, exit_status = _recorded_result(
                messages, index, is_error_is_exit_verdict=is_error_is_exit_verdict
            )
        else:
            result, source, exit_status = CallResult.UNKNOWN, ResultSource.NONE, None
        calls.append(
            RecordedCall(
                number=len(calls) + 1,
                index=index,
                command=command,
                result=result,
                source=source,
                exit_status=exit_status,
                in_workspace=_in_workspace(message, task_cwd),
            )
        )
    return tuple(calls)


def later_run_failed(call: RecordedCall, ledger: Sequence[RecordedCall]) -> bool:
    """Whether a later call recorded the same command and did not pass.

    The comparison is between two of the controller's own records (exact
    recorded command text), never with anything the worker wrote. A later
    rerun that failed, or whose result is unknown, defeats an earlier pass.
    """
    return any(
        other.number > call.number
        and other.command == call.command
        and other.result is not CallResult.PASSED
        for other in ledger
    )


def is_latest_of_command(call: RecordedCall, ledger: Sequence[RecordedCall]) -> bool:
    """Whether no later call recorded the same command text."""
    return not any(other.number > call.number and other.command == call.command for other in ledger)


__all__ = [
    "SHELL_TOOL_NAME",
    "CallResult",
    "RecordedCall",
    "ResultSource",
    "build_call_ledger",
    "is_latest_of_command",
    "later_run_failed",
]
