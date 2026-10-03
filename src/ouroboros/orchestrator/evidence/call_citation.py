"""Evidence by transcript call number.

The worker cites recorded Bash calls by number instead of retyping command
strings. The controller reads each cited call's command and exit status from
the runtime transcript, so a citation cannot misquote what ran. A citation is
still only a pointer chosen by the worker: it may point at a call that passed
without checking the criterion, at a call whose exit status belongs to an
output filter (``pytest x | tail -5`` records ``tail``'s exit), at an edit and
a check that share one call (``apply_patch ...; python repro.py``), or at a
check the worker wrote itself that encodes the worker's own misunderstanding.

This module therefore separates three things:

``recorded_calls``
    The numbered call table, built from structured transcript data only (the
    recorded command parsed with shell quoting, the recorded integer exit).
``qualify_call``
    Whether one cited call is evidence under a ``CitationPolicy``. The default
    policy accepts only a call that (1) recorded exit 0, (2) is a test
    invocation whose test files already exist in the base tree (the worker
    did not write the check), (3) names a file the change touched or its
    paired test, (4) passed again when the controller replayed it on the
    frozen artifact, and (5) executed at least one changed line in that
    replay. Anything less is no evidence.
``decide_citations``
    The criterion outcome. It can accept only on a qualified call. A cited
    call whose replay failed is an executed failure and never accepts. With
    no qualified call the outcome is ``no_evidence`` (the criterion stays
    undecided); it is never fabrication and never a rejection: a citation
    that does not qualify says nothing about the work, only that the worker
    pointed at a call the controller cannot use.

Nothing here reads criterion text, and no model output can grant: the worker
supplies call numbers, the controller supplies everything else.

A pass under the default policy is regression evidence (existing tests still
pass and they run the changed code). It does not show that the criterion's new
behavior exists: a check that also passes on the unchanged base cannot show
that. The admitted check package remains the authority for that question.
"""

from __future__ import annotations

from collections.abc import Collection, Container, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
import posixpath
import re
import shlex

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.claims import (
    _runtime_message_command_values,
    _runtime_message_has_conflicting_tool_call_ids,
    _runtime_message_is_tool_completion,
)
from ouroboros.orchestrator.evidence.command_replay import (
    _is_pure_output_filter,
    transcript_exit_status,
)
from ouroboros.orchestrator.evidence.observed_runs import (
    _script_operand,
    _without_heredoc_bodies,
)
from ouroboros.orchestrator.evidence.replay_policy import resolve_replay_program
from ouroboros.orchestrator.evidence.shell_parsing import (
    _changes_directory,
    _command_lists,
    _commands_implied_by_success,
    _is_env_assignment,
    _is_python_executable,
    _looks_like_test_command,
    _peel_shell_wrappers,
    _strip_env_prefix,
    _top_level_shell_character_positions,
    python_inline_program,
)

# ``2>&1`` and ``> /dev/null``: descriptor plumbing that is not an argument.
_FD_PLUMBING = re.compile(r"(?<=\s)(?:\d?>&\d|[12&]?>>?\s*/dev/null)(?=\s|$|;|&|\|)")

KIND_TEST = "test"
KIND_SCRIPT = "script"
KIND_INLINE = "inline"
KIND_OTHER = "other"
_KIND_RANK = {KIND_TEST: 3, KIND_SCRIPT: 2, KIND_INLINE: 1, KIND_OTHER: 0}


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """One Bash call of the transcript, as the controller reads it.

    ``number`` is the 1-based position among the transcript's Bash calls and
    is what the worker cites. ``exit_status`` is the recorded integer exit of
    the whole call (None when the transcript recorded none). ``masked`` means
    a trailing output filter decided that exit, so it says nothing about the
    command in front of the filter. ``executions`` are the commands whose
    success a zero exit implies and that run project code
    (``relative_executions``: the same with workspace-relative paths); ``kind``
    is the strongest of them. ``files`` are the script and test files those
    executions name, workspace-relative. ``replay_command`` is what a replay
    runs: the executions alone, without the edits or file writes that shared
    the call and without the output filter.
    """

    number: int
    command: str
    exit_status: int | None
    masked: bool
    kind: str
    executions: tuple[tuple[str, ...], ...]
    files: tuple[str, ...]
    replay_command: str
    directory: str | None = None
    relative_executions: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class ReplayOutcome:
    """What the controller observed when it replayed a call's ``replay_command``.

    ``returncode`` is the replay's exit on the frozen artifact (None when the
    replay did not run or timed out). ``executed_changed_line`` is True when
    the replay ran at least one line the change added or modified (None when
    not measured).
    """

    returncode: int | None
    executed_changed_line: bool | None = None


class CallState(StrEnum):
    QUALIFIED = "qualified"
    UNQUALIFIED = "unqualified"
    EXECUTED_FAILURE = "executed_failure"


class CitationDecision(StrEnum):
    ACCEPT = "accept"
    NO_EVIDENCE = "no_evidence"


@dataclass(frozen=True, slots=True)
class CitationPolicy:
    """Which cited calls are evidence: the qualification gates, all on by default."""

    require_base_tree_test: bool = True
    require_argv_touch: bool = True
    require_replay: bool = True
    require_changed_line: bool = True


@dataclass(frozen=True, slots=True)
class CitationVerdict:
    decision: CitationDecision
    evidence: tuple[int, ...] = ()
    reasons: tuple[str, ...] = field(default_factory=tuple)


def _core_text(peeled: str) -> tuple[str, bool]:
    """Return the command without trailing pure output filters, and whether any was removed."""
    text, masked = peeled, False
    for _ in range(4):
        positions = [
            position
            for position in _top_level_shell_character_positions(text, "|")
            if text[position : position + 2] != "||"
            and (position == 0 or text[position - 1] != "|")
        ]
        if not positions:
            break
        segment = text[positions[-1] + 1 :].lstrip("&").strip()
        if "\n" in segment or not _is_pure_output_filter(segment):
            break
        text, masked = text[: positions[-1]].rstrip(), True
    return text, masked


def _workspace_relative(token: str, task_cwd: str | None) -> str:
    value = token
    if task_cwd and value.startswith(task_cwd.rstrip("/") + "/"):
        value = value[len(task_cwd.rstrip("/")) + 1 :]
    return value


def _execution_kind(
    argv: Sequence[str], heredoc: bool, task_cwd: str | None
) -> tuple[str, str | None]:
    """Classify one implied command structurally; return (kind, script it runs)."""
    relative = tuple(_workspace_relative(token, task_cwd) for token in argv)
    stripped = tuple(_strip_env_prefix(list(relative)))
    for candidate in (relative, stripped):
        if not candidate:
            continue
        runner = resolve_replay_program(candidate, workspace=None)
        if _looks_like_test_command(shlex.join(candidate)):
            script = (
                _script_operand(candidate)
                if runner is not None and runner.kind == "script"
                else None
            )
            return KIND_TEST, script
        if runner is not None and runner.kind == "script":
            return KIND_SCRIPT, _script_operand(candidate)
        if python_inline_program(candidate) is not None:
            return KIND_INLINE, None
    if stripped and _is_python_executable(stripped[0]):
        if heredoc and (len(stripped) == 1 or stripped[1] == "-"):
            return KIND_INLINE, None
        if len(stripped) > 1 and stripped[1].endswith(".py"):
            return KIND_SCRIPT, stripped[1]
    return KIND_OTHER, None


def _join_with_assignments(argv: Sequence[str]) -> str:
    """``shlex.join`` that keeps leading ``NAME=value`` tokens as assignments."""
    head: list[str] = []
    index = 0
    while index < len(argv) and _is_env_assignment(argv[index]):
        name, _, value = argv[index].partition("=")
        head.append(f"{name}={shlex.quote(value)}")
        index += 1
    rest = shlex.join(argv[index:]) if index < len(argv) else ""
    return " ".join([*head, rest]).strip()


def describe_command(
    number: int, command: str, exit_status: int | None, *, task_cwd: str | None
) -> RecordedCall:
    """Build the ``RecordedCall`` of one recorded command string."""
    peeled = _peel_shell_wrappers(command)
    core, masked = _core_text(peeled)
    body = _without_heredoc_bodies(core)
    heredoc = body is not None and body != core
    if body is None:
        body = core
    body = _FD_PLUMBING.sub(" ", body)
    lists = _command_lists(body)
    implied = _commands_implied_by_success(body) if lists else ()
    kind = KIND_OTHER
    executions: list[tuple[str, ...]] = []
    files: list[str] = []
    for argv in implied:
        found, script = _execution_kind(argv, heredoc, task_cwd)
        if found == KIND_OTHER:
            continue
        executions.append(tuple(argv))
        if _KIND_RANK[found] > _KIND_RANK[kind]:
            kind = found
        if script:
            files.append(script)
        if found == KIND_TEST:
            for token in _strip_env_prefix([_workspace_relative(t, task_cwd) for t in argv])[1:]:
                target = token.split("::")[0]
                if not token.startswith("-") and target.endswith(".py") and target not in files:
                    files.append(target)
    directory = None
    for commands in lists:
        for argv, _ in commands:
            if _changes_directory(argv) and len(argv) > 1:
                directory = argv[1]
    relative_directory = _workspace_relative(directory, task_cwd) if directory else None
    if relative_directory and task_cwd and relative_directory == task_cwd.rstrip("/"):
        relative_directory = "."
    normalized: list[str] = []
    for name in files:
        value = _workspace_relative(name, task_cwd)
        if relative_directory and not value.startswith("/"):
            value = posixpath.join(relative_directory, value)
        normalized.append(posixpath.normpath(value))
    stdin_program = any(
        (stripped := _strip_env_prefix(list(argv)))
        and _is_python_executable(stripped[0])
        and (len(stripped) == 1 or stripped[1] == "-")
        for argv in executions
    )
    replay_command = core
    if sum(len(commands) for commands in lists) > 1 and executions and not stdin_program:
        prefix = f"cd {shlex.quote(directory)} && " if directory else ""
        replay_command = prefix + " && ".join(
            _join_with_assignments(argv) for argv in reversed(executions)
        )
    return RecordedCall(
        number=number,
        command=peeled,
        exit_status=exit_status,
        masked=masked,
        kind=kind,
        executions=tuple(executions),
        files=tuple(normalized),
        replay_command=replay_command,
        directory=relative_directory,
        relative_executions=tuple(
            tuple(_workspace_relative(token, task_cwd) for token in argv) for argv in executions
        ),
    )


def recorded_calls(
    messages: Sequence[AgentMessage], *, task_cwd: str | None
) -> tuple[RecordedCall, ...]:
    """Return the transcript's Bash calls, numbered from 1 in recorded order.

    A call whose tool-call ids conflict gets no exit status (it can never be
    evidence). The exit status is the one ``transcript_exit_status`` reads:
    every record of the call must state success for it to be 0.
    """
    calls: list[RecordedCall] = []
    for index, message in enumerate(messages):
        if message.tool_name != "Bash" or _runtime_message_is_tool_completion(message):
            continue
        commands = _runtime_message_command_values(message)
        if not commands:
            continue
        status = (
            None
            if _runtime_message_has_conflicting_tool_call_ids(message)
            else transcript_exit_status(messages, index)
        )
        calls.append(describe_command(len(calls) + 1, commands[0], status, task_cwd=task_cwd))
    return tuple(calls)


def is_test_path(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    return (
        any(part in ("tests", "test", "testing") for part in parts[:-1])
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name in ("conftest.py", "tests.py")
    )


def touches_change(call: RecordedCall, changed_paths: Collection[str]) -> bool:
    """True when the call's recorded argv names a changed file, a directory holding one,
    or a test file paired by path with a changed source file. Argv and the diff only."""
    targets = [path for path in changed_paths if path]
    if not targets:
        return False
    stems = {
        posixpath.splitext(posixpath.basename(path))[0]
        for path in targets
        if not is_test_path(path)
    }
    for argv in call.relative_executions or call.executions:
        for token in argv[1:]:
            if token.startswith("-") or ("=" in token and "/" not in token):
                continue
            value = posixpath.normpath(token.split("::")[0]) if token else token
            candidates = {value}
            if call.directory not in (None, ".", ""):
                candidates.add(posixpath.normpath(posixpath.join(call.directory, value)))
            for candidate in candidates:
                for path in targets:
                    if candidate == path or path.startswith(candidate.rstrip("/") + "/"):
                        return True
                    dotted = candidate.replace(".", "/") if "/" not in candidate else None
                    stem_path = posixpath.splitext(path)[0]
                    if dotted and (
                        stem_path.endswith("/" + dotted) or f"/{dotted}/" in f"/{stem_path}/"
                    ):
                        return True
                base = posixpath.splitext(posixpath.basename(candidate))[0]
                last = candidate.split(".")[-1] if "/" not in candidate else base
                if any(
                    name in (f"test_{stem}", f"{stem}_test")
                    for stem in stems
                    for name in (base, last)
                ):
                    return True
    return False


def qualify_call(
    call: RecordedCall,
    *,
    policy: CitationPolicy,
    replay: ReplayOutcome | None,
    changed_paths: Collection[str],
    base_tree_paths: Container[str],
) -> tuple[CallState, str | None]:
    """Decide whether one cited call is evidence. Structural facts and replay outcomes only."""
    if call.exit_status != 0:
        return CallState.UNQUALIFIED, "exit_not_zero"
    replayed = replay is not None and replay.returncode is not None
    # A replay that failed is an executed failure whatever the other gates say.
    if policy.require_replay and replayed and replay is not None and replay.returncode != 0:
        return CallState.EXECUTED_FAILURE, "replay_failed"
    if call.masked and not (policy.require_replay and replayed):
        return CallState.UNQUALIFIED, "exit_belongs_to_output_filter"
    if call.kind == KIND_OTHER:
        return CallState.UNQUALIFIED, "not_an_execution"
    if policy.require_base_tree_test and not (
        call.kind == KIND_TEST and all(name in base_tree_paths for name in call.files)
    ):
        return CallState.UNQUALIFIED, "not_a_base_tree_test"
    if policy.require_argv_touch and not touches_change(call, changed_paths):
        return CallState.UNQUALIFIED, "argv_does_not_touch_change"
    if policy.require_replay or policy.require_changed_line:
        if not replayed or replay is None:
            return CallState.UNQUALIFIED, "not_replayed"
        if policy.require_changed_line and not replay.executed_changed_line:
            return CallState.UNQUALIFIED, "no_changed_line_executed"
    return CallState.QUALIFIED, None


def decide_citations(
    cited: Sequence[int],
    calls: Sequence[RecordedCall],
    *,
    policy: CitationPolicy,
    replays: Mapping[int, ReplayOutcome],
    changed_paths: Collection[str],
    base_tree_paths: Container[str],
) -> CitationVerdict:
    """Decide one criterion from the call numbers the worker cited for it.

    Accept only when a cited call qualifies and no cited call failed its
    replay. A failed replay leaves the criterion without evidence; it never
    accepts. A number that is not in the table is ignored and reported.
    """
    by_number = {call.number: call for call in calls}
    reasons: list[str] = []
    qualified: list[int] = []
    failed = False
    for number in dict.fromkeys(cited):
        call = by_number.get(number)
        if call is None:
            reasons.append(f"call {number}: not in transcript")
            continue
        state, reason = qualify_call(
            call,
            policy=policy,
            replay=replays.get(number),
            changed_paths=changed_paths,
            base_tree_paths=base_tree_paths,
        )
        if state is CallState.QUALIFIED:
            qualified.append(number)
        else:
            reasons.append(f"call {number}: {reason}")
            failed = failed or state is CallState.EXECUTED_FAILURE
    if qualified and not failed:
        return CitationVerdict(CitationDecision.ACCEPT, tuple(qualified), tuple(reasons))
    if not reasons:
        reasons.append("no call cited")
    return CitationVerdict(CitationDecision.NO_EVIDENCE, (), tuple(reasons))


__all__ = [
    "CallState",
    "CitationDecision",
    "CitationPolicy",
    "CitationVerdict",
    "RecordedCall",
    "ReplayOutcome",
    "decide_citations",
    "describe_command",
    "is_test_path",
    "qualify_call",
    "recorded_calls",
    "touches_change",
]
