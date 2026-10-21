"""Claims the transcript shows running inside a compound shell call.

A worker often runs its check as the last command of a larger Bash call,
after the edit that prepares it::

    /bin/bash -lc "apply_patch <<'PATCH'
    ...
    PATCH
    python repro_issue.py"

and then claims ``python repro_issue.py``. The whole-command aliases the
transcript verifier matches never equal that claim, and replay does not take
compound calls, so the claim used to read as unsupported and the criterion was
rejected as ``FABRICATION_SUSPECTED`` although the transcript recorded the run.

This module reads such a run from structured transcript data only: the Bash
call's recorded command, parsed with shell quoting (here-document bodies
removed), and the exit status the runtime recorded for that call. A claimed
command is an observed run when its argv is one of the commands whose success
the call's zero exit implies (``shell_parsing._commands_implied_by_success``)
and the latest call that ran it recorded an integer exit 0. The claim text itself is
never searched; it must parse as one simple command whose argv is compared
exactly.

When the observed run executes a workspace script (``python x.py``,
``sh x.sh``, ``./x.sh``) that is no longer in the workspace, typically a
scratch script the worker created, ran and deleted, the run cannot be replayed
and nothing vouches for what it ran: it yields no evidence
(``SCRIPT_ABSENT_FROM_ARTIFACT``), which is not fabrication.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shlex

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.claims import (
    _runtime_message_command_values,
    _runtime_message_effective_cwd,
    _runtime_message_has_conflicting_tool_call_ids,
    _runtime_message_is_tool_completion,
)
from ouroboros.orchestrator.evidence.command_replay import transcript_exit_status
from ouroboros.orchestrator.evidence.replay_policy import resolve_replay_program
from ouroboros.orchestrator.evidence.shell_parsing import (
    _changes_directory,
    _command_lists,
    _commands_implied_by_success,
    _peel_shell_wrappers,
)
from ouroboros.orchestrator.evidence.test_detection import (
    _functional_command_has_authoritative_zero_exit,
)

SCRIPT_ABSENT_FROM_ARTIFACT = "script_absent_from_artifact"
"""Typed not-replayed reason: the observed run's script is not in the artifact."""

_HEREDOC_OPERATORS = frozenset({"<<", "<<-"})


@dataclass(frozen=True, slots=True)
class ObservedRun:
    """A claimed command the transcript recorded running with exit 0.

    ``script`` is the workspace script the command executes (``None`` when it
    runs none) and ``script_present`` whether that script is a regular file
    in the workspace at verification time.
    """

    argv: tuple[str, ...]
    script: str | None
    script_present: bool


def _claim_argv(value: str) -> tuple[str, ...] | None:
    """Return the argv of a claim that is exactly one simple command, or None."""
    try:
        argv = tuple(shlex.split(value))
    except ValueError:
        return None
    if not argv or _command_lists(value) != [[(argv, None)]]:
        return None
    return argv


def _line_heredoc_delimiters(line: str) -> list[tuple[str, bool]] | None:
    """Return ``(delimiter, strip_tabs)`` for each here-document a line opens.

    None when the line cannot be tokenized on its own (an unbalanced quote
    spanning lines), so the caller fails closed.
    """
    try:
        lexer = shlex.shlex(line, posix=True, punctuation_chars="();<>|&")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    delimiters: list[tuple[str, bool]] = []
    for index, token in enumerate(tokens):
        if token not in _HEREDOC_OPERATORS:
            continue
        if index + 1 >= len(tokens):
            return None
        word = tokens[index + 1]
        strip_tabs = token == "<<-"
        if token == "<<" and word.startswith("-") and len(word) > 1:
            # ``<<-EOF`` tokenizes as ``<<`` and ``-EOF``.
            word, strip_tabs = word[1:], True
        if not word:
            return None
        delimiters.append((word, strip_tabs))
    return delimiters


def _without_heredoc_bodies(body: str) -> str | None:
    """Return ``body`` with every here-document body removed, or None.

    None when a here-document is not terminated (the shell would read the
    rest of the script as its body, so no later line runs) or a line cannot
    be tokenized.
    """
    kept: list[str] = []
    pending: list[tuple[str, bool]] = []
    for line in body.split("\n"):
        if pending:
            delimiter, strip_tabs = pending[0]
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                pending.pop(0)
            continue
        delimiters = _line_heredoc_delimiters(line)
        if delimiters is None:
            return None
        pending.extend(delimiters)
        kept.append(line)
    if pending:
        return None
    return "\n".join(kept)


def _implied_commands(recorded: str) -> tuple[tuple[str, ...], ...]:
    """Return the commands a zero exit of ``recorded`` implies succeeded.

    Empty when the call changes directory (the command would not have run
    where the workspace check looks) or cannot be parsed.
    """
    body = _without_heredoc_bodies(_peel_shell_wrappers(recorded))
    if body is None:
        return ()
    lists = _command_lists(body)
    if any(_changes_directory(argv) for commands in lists for argv, _ in commands):
        return ()
    return _commands_implied_by_success(body)


def _script_operand(argv: tuple[str, ...]) -> str | None:
    """Return the workspace script ``argv`` executes, or None when it runs none.

    Resolved lexically by the replay allowlist (``resolve_replay_program``
    without a workspace): a ``script`` runner's own arguments follow the
    script, so the token before them is the script.
    """
    runner = resolve_replay_program(argv, workspace=None)
    if runner is None or runner.kind != "script" or not runner.programs:
        return None
    final = runner.programs[-1]
    head = final[: len(final) - len(runner.arguments)]
    return head[-1] if head else None


def _script_present(script: str, cwd: str) -> bool:
    try:
        base = Path(cwd).resolve()
        candidate = (base / script).resolve()
        candidate.relative_to(base)
        return candidate.is_file()
    except (OSError, RuntimeError, ValueError):
        return False


def observed_zero_exit_run(
    value: str,
    messages: tuple[AgentMessage, ...],
    *,
    task_cwd: str | None,
) -> ObservedRun | None:
    """Return the run of claim ``value`` the transcript recorded with exit 0, or None.

    The latest Bash call whose zero exit would imply the claimed argv decides:
    when it recorded no exit or a failure, an earlier passing run does not
    stand in for it. The call must run in the workspace (``task_cwd``).
    """
    if task_cwd is None:
        return None
    argv = _claim_argv(value)
    if argv is None:
        return None
    for index in reversed(range(len(messages))):
        message = messages[index]
        if message.tool_name != "Bash" or _runtime_message_is_tool_completion(message):
            continue
        if not any(
            argv in _implied_commands(recorded)
            for recorded in _runtime_message_command_values(message)
        ):
            continue
        if _runtime_message_has_conflicting_tool_call_ids(message):
            return None
        # Every record of the call states success, and one states an integer
        # zero exit: a lifecycle-only "completed" is not an exit status.
        if transcript_exit_status(
            messages, index
        ) != 0 or not _functional_command_has_authoritative_zero_exit(messages, index=index):
            return None
        cwd = _runtime_message_effective_cwd(message, task_cwd=task_cwd)
        if cwd is None or os.path.realpath(cwd) != os.path.realpath(task_cwd):
            return None
        script = _script_operand(argv)
        return ObservedRun(
            argv=argv,
            script=script,
            script_present=script is not None and _script_present(script, cwd),
        )
    return None


__all__ = ["SCRIPT_ABSENT_FROM_ARTIFACT", "ObservedRun", "observed_zero_exit_run"]
