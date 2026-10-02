"""Structural reading of recorded shell calls, and the order the evidence turn shows them in.

Everything here reads the controller's own record of a call (its recorded
command, parsed with shell quoting, and its recorded result) plus the
harness's own workspace observation. Nothing reads a criterion, a claim, or
the worker's prose, and nothing matches keywords.

The evidence turn lists the calls most likely to be evidence first. The order
is a weighted sum of structural features only
(``config.models.EvidenceCallOrderingWeights``):

- ``verification``: a command whose success the call's zero exit implies is a
  verification invocation (a recognized test runner, an allowlisted runner,
  or a Python program run from a file, ``-c`` or standard input);
- ``exercises_patch``: such a command names a file the final workspace
  changed (or a directory holding one);
- ``latest_of_command``: no later call recorded the same command;
- ``passed``: the recorded result is a pass;
- ``after_last_edit``: the call ran after the last recorded edit of every
  changed file it names;
- ``replayable``: the recorded command is a replay candidate.

Ties keep transcript order, so the order is deterministic. Numbers never
change with the order: a row's number is its transcript position.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
import os
from pathlib import PurePosixPath
import shlex

from ouroboros.config.models import EvidenceCallOrderingWeights
from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.call_ledger import (
    CallResult,
    RecordedCall,
    is_latest_of_command,
)
from ouroboros.orchestrator.evidence.claims import (
    _runtime_message_file_path_values,
    _runtime_message_is_tool_completion,
)
from ouroboros.orchestrator.evidence.command_replay import replay_candidate
from ouroboros.orchestrator.evidence.harness_observation import observation_from_message
from ouroboros.orchestrator.evidence.observed_runs import (
    _script_operand,
    _script_present,
    _without_heredoc_bodies,
)
from ouroboros.orchestrator.evidence.replay_policy import resolve_replay_program
from ouroboros.orchestrator.evidence.shell_parsing import (
    _PYTHON_VALUE_OPTIONS,
    _changes_directory,
    _command_lists,
    _commands_implied_by_success,
    _is_python_executable,
    _peel_shell_wrappers,
    _strip_env_prefix,
    _test_command_invocation,
    program_chain,
)

# The evidence turn shows at most this many rows; every recorded call stays
# citable by its number whether shown or not.
MAX_LISTED_CALLS = 40


@dataclass(frozen=True, slots=True)
class CallShape:
    """What a recorded command runs, read with shell quoting.

    ``implied`` is the argv of each command a zero exit of the whole call
    implies succeeded (here-document bodies removed); empty when the command
    cannot be parsed. ``changes_directory`` is True when the call changes
    directory, so its commands did not necessarily run in the workspace.
    """

    implied: tuple[tuple[str, ...], ...]
    changes_directory: bool


def call_shape(command: str) -> CallShape:
    """Parse a recorded command into the commands its success implies."""
    body = _without_heredoc_bodies(_peel_shell_wrappers(command))
    if body is None:
        return CallShape(implied=(), changes_directory=False)
    lists = _command_lists(body)
    moves = any(_changes_directory(argv) for commands in lists for argv, _ in commands)
    return CallShape(implied=_commands_implied_by_success(body), changes_directory=moves)


def _program(argv: Sequence[str]) -> tuple[str, ...]:
    """The argv of the program ``argv`` finally runs (wrappers and launchers peeled)."""
    stripped = tuple(_strip_env_prefix(list(argv)))
    programs = program_chain(stripped)
    return tuple(programs[-1]) if programs else stripped


def _python_program_source(argv: Sequence[str]) -> tuple[str, str | None] | None:
    """For a Python interpreter run: ``("file", path)``, ``("inline", None)`` or ``("stdin", None)``.

    None when ``argv`` is not a Python interpreter running a program (for
    example ``python -m module``, which runs a named module, not a program).
    """
    program = _program(argv)
    if not program or not _is_python_executable(program[0]):
        return None
    index = 1
    while index < len(program):
        token = program[index]
        if token in _PYTHON_VALUE_OPTIONS:
            index += 2
            continue
        if token == "-":
            return "stdin", None
        if token.startswith("-c") or (
            token.startswith("-") and not token.startswith("--") and token.endswith("c")
        ):
            return "inline", None
        if token.startswith("-m"):
            return None
        if token.startswith("-"):
            index += 1
            continue
        return "file", token
    return "stdin", None


def is_verification_invocation(argv: Sequence[str]) -> bool:
    """Whether ``argv`` runs a test runner, an allowlisted runner, or a Python program."""
    if not argv:
        return False
    if _test_command_invocation(shlex.join(argv)) is not None:
        return True
    stripped = tuple(_strip_env_prefix(list(argv)))
    if stripped and resolve_replay_program(stripped, workspace=None) is not None:
        return True
    return _python_program_source(argv) is not None


def executed_script(argv: Sequence[str]) -> str | None:
    """The script file ``argv`` executes, or None when it runs none."""
    source = _python_program_source(argv)
    if source is not None:
        return source[1]
    return _script_operand(tuple(_strip_env_prefix(list(argv))))


def script_in_artifact(script: str, task_cwd: str | None) -> bool:
    """Whether ``script`` is a regular file inside the workspace now."""
    return task_cwd is not None and _script_present(script, task_cwd)


def verification_commands(shape: CallShape) -> tuple[tuple[str, ...], ...]:
    """The implied commands that are verification invocations."""
    return tuple(argv for argv in shape.implied if is_verification_invocation(argv))


def _normalized_relative(token: str, task_cwd: str | None) -> str | None:
    path_text = token.split("::", 1)[0]
    if not path_text or path_text.startswith("-") or "=" in path_text:
        return None
    if os.path.isabs(path_text):
        if task_cwd is None:
            return None
        try:
            relative = os.path.relpath(os.path.normpath(path_text), os.path.realpath(task_cwd))
        except ValueError:
            return None
    else:
        relative = os.path.normpath(path_text)
    parts = PurePosixPath(relative).parts
    if not parts or ".." in parts:
        return None
    return PurePosixPath(*parts).as_posix()


def exercised_changed_files(
    shape: CallShape, changed_files: frozenset[str], *, task_cwd: str | None
) -> frozenset[str]:
    """Changed files the call's verification commands name, directly or by directory."""
    named: set[str] = set()
    for argv in verification_commands(shape):
        for token in argv[1:]:
            relative = _normalized_relative(token, task_cwd)
            if relative is None:
                continue
            for changed in changed_files:
                if changed == relative or changed.startswith(relative.rstrip("/") + "/"):
                    named.add(changed)
    return frozenset(named)


def changed_files_from_observations(messages: Iterable[AgentMessage]) -> frozenset[str]:
    """Files the harness's own workspace snapshot saw change or appear."""
    changed: set[str] = set()
    for message in messages:
        observation = observation_from_message(message)
        if observation is not None:
            changed.update(observation.changed_paths)
    return frozenset(changed)


def _last_edit_index(
    messages: Sequence[AgentMessage], files: frozenset[str], *, task_cwd: str | None
) -> int:
    last = -1
    for index, message in enumerate(messages):
        if message.tool_name is None or _runtime_message_is_tool_completion(message):
            continue
        for value in _runtime_message_file_path_values(message):
            relative = _normalized_relative(value, task_cwd)
            if relative is not None and relative in files:
                last = index
    return last


@dataclass(frozen=True, slots=True)
class CallFeatures:
    """The structural features of one recorded call (each 0 or 1)."""

    verification: bool
    exercises_patch: bool
    latest_of_command: bool
    passed: bool
    after_last_edit: bool
    replayable: bool

    def score(self, weights: EvidenceCallOrderingWeights) -> float:
        return (
            weights.verification * self.verification
            + weights.exercises_patch * self.exercises_patch
            + weights.latest_of_command * self.latest_of_command
            + weights.passed * self.passed
            + weights.after_last_edit * self.after_last_edit
            + weights.replayable * self.replayable
        )


def call_features(
    call: RecordedCall,
    *,
    ledger: Sequence[RecordedCall],
    messages: Sequence[AgentMessage],
    changed_files: frozenset[str],
    task_cwd: str | None,
) -> CallFeatures:
    """Read the structural features of ``call`` from the controller's own records."""
    shape = call_shape(call.command)
    exercised = exercised_changed_files(shape, changed_files, task_cwd=task_cwd)
    return CallFeatures(
        verification=bool(verification_commands(shape)),
        exercises_patch=bool(exercised),
        latest_of_command=is_latest_of_command(call, ledger),
        passed=call.result is CallResult.PASSED,
        after_last_edit=bool(exercised)
        and _last_edit_index(messages, exercised, task_cwd=task_cwd) < call.index,
        replayable=replay_candidate(call.command, task_cwd) is not None,
    )


def order_calls(
    ledger: Sequence[RecordedCall],
    *,
    messages: Sequence[AgentMessage],
    task_cwd: str | None,
    weights: EvidenceCallOrderingWeights,
) -> tuple[RecordedCall, ...]:
    """The ledger by descending structural score, transcript order breaking ties."""
    changed = changed_files_from_observations(messages)
    scored = [
        (
            call_features(
                call, ledger=ledger, messages=messages, changed_files=changed, task_cwd=task_cwd
            ).score(weights),
            call.number,
            call,
        )
        for call in ledger
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return tuple(call for _score, _number, call in scored)


def render_call_list(ordered: Sequence[RecordedCall], *, limit: int = MAX_LISTED_CALLS) -> str:
    """The numbered list the evidence turn shows: number, recorded command, recorded result."""
    rows = []
    for call in ordered[:limit]:
        command = call.command or "(no recorded command)"
        rows.append(f"[{call.number}] {call.result_label()} :: {command}")
    return "\n".join(rows)


__all__ = [
    "MAX_LISTED_CALLS",
    "CallFeatures",
    "CallShape",
    "call_features",
    "call_shape",
    "changed_files_from_observations",
    "executed_script",
    "exercised_changed_files",
    "is_verification_invocation",
    "order_calls",
    "render_call_list",
    "script_in_artifact",
    "verification_commands",
]
