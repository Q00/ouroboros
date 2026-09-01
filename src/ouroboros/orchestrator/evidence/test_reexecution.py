"""Re-execute a leaf's claimed test commands when the transcript cannot prove them.

``tests_passed`` is proven from the transcript: a Bash call whose recorded
command targets the claim and whose runtime output proves the suite passed.
Some runtimes deliver that output partially or not at all — Codex completions
without an ``exit_code``, truncated ``aggregated_output``, tool results that
never reach the stream — and the leaf is then rejected for tests that pass.

Rather than teach the matcher every runtime's output shape, the harness runs
the test command itself, in the workspace, through the same POSIX shell the
verify gate uses, with the verify-command timeout. The exit status and output
are then the harness's own observation and are judged by the same rules as
transcript output (see ``test_detection._harness_reexecution_supports_test_claim``).

Re-execution is bounded and only happens when it can change the verdict: at
most ``MAX_REEXECUTED_COMMANDS`` distinct test-looking commands, and only when
at least one ``tests_passed`` claim is currently unsupported.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import shlex

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.claims import _runtime_message_command_values
from ouroboros.orchestrator.evidence.common import _flatten_evidence_values
from ouroboros.orchestrator.evidence.harness_observation import CommandObservation
from ouroboros.orchestrator.evidence.shell_parsing import (
    _is_env_assignment,
    _looks_like_test_command,
    _shell_command_body,
)
from ouroboros.orchestrator.evidence.test_detection import (
    _runtime_messages_support_test_claim,
)
from ouroboros.orchestrator.evidence_schema import EvidenceError, extract_evidence
from ouroboros.orchestrator.verify_command_runner import run_with_shell

MAX_REEXECUTED_COMMANDS = 3
OUTPUT_TAIL_CHARS = 4_000

# Characters that would let leaf-authored text smuggle shell behaviour into a
# re-executed command. Candidates are executed as a direct argv (never through
# a shell), so these can only appear as literal argument bytes — but a command
# whose meaning depends on shell interpretation is not the command the leaf
# claims to have run, so it is rejected outright instead of run differently.
_SHELL_METACHARACTERS = frozenset("`$;&|<>(){}\n\r")


def safe_test_argv(command: str) -> tuple[str, ...] | None:
    """Return the direct argv for a claimed test command, or None.

    The blocker this closes: self-reported ``tests_passed``/``commands_run``
    text was handed to ``bash -c``, so ``pytest -q "$(touch marker)"`` ran the
    substitution. Executable text must never be shell-interpreted: only a
    command that tokenizes cleanly and carries no shell metacharacters is
    eligible, and it runs as an argv with no shell in front of it.
    """
    stripped = command.strip()
    if not stripped or any(char in _SHELL_METACHARACTERS for char in stripped):
        return None
    try:
        argv = shlex.split(stripped)
    except ValueError:
        return None
    if not argv or any(
        not token or any(c in _SHELL_METACHARACTERS for c in token) for token in argv
    ):
        return None
    return tuple(argv)


def safe_test_invocation(command: str) -> tuple[dict[str, str], tuple[str, ...]] | None:
    """Split a claimed test command into (environment delta, executable argv).

    ``_looks_like_test_command`` accepts leading environment assignments
    (``REEXEC_FLAG=yes python -m pytest``) via the same rule the evidence
    matcher uses, so selection and execution must agree on that syntax: the
    assignments become a controlled environment delta and the remainder is the
    direct argv. Every token has already passed the metacharacter gate, so the
    assignment values are literal bytes in both shell and direct execution —
    the semantics the leaf claims are exactly the semantics that run. A bare
    ``env`` prefix is peeled the same way ``_strip_env_prefix`` does.
    """
    argv = safe_test_argv(command)
    if argv is None:
        return None
    index = 1 if argv[0] == "env" else 0
    env_delta: dict[str, str] = {}
    while index < len(argv) and _is_env_assignment(argv[index]):
        name, _, value = argv[index].partition("=")
        env_delta[name] = value
        index += 1
    executable = argv[index:]
    if not executable:
        return None
    return env_delta, executable


def select_test_reexecution_commands(
    *,
    final_message: str | None,
    messages: tuple[AgentMessage, ...],
    task_cwd: str | None,
) -> tuple[str, ...]:
    """Return the test commands worth re-running for this leaf, if any.

    Empty when the leaf emitted no parseable evidence, claimed no tests, or
    every ``tests_passed`` claim is already backed by the transcript.
    """
    if not final_message:
        return ()
    try:
        record = extract_evidence(final_message)
    except EvidenceError:
        return ()
    test_claims = _flatten_evidence_values(record.get("tests_passed"))
    if not test_claims:
        return ()

    support_messages = tuple(message for message in messages if not message.is_final)
    unsupported = [
        claim
        for claim in test_claims
        if not _runtime_messages_support_test_claim(
            value=claim,
            backed_commands=(),
            messages=support_messages,
            task_cwd=task_cwd,
        )
    ]
    if not unsupported:
        return ()

    candidates: list[str] = []
    # The claim itself, when it is a runnable test command.
    candidates.extend(claim.strip() for claim in unsupported if _looks_like_test_command(claim))
    # Test commands the leaf reported running.
    candidates.extend(
        command.strip()
        for command in _flatten_evidence_values(record.get("commands_run"))
        if _looks_like_test_command(command)
    )
    # Test commands the transcript shows the leaf running, unwrapped from the
    # runtime's shell wrapper when there is one.
    for message in support_messages:
        if message.tool_name != "Bash":
            continue
        for recorded in _runtime_message_command_values(message):
            body = _shell_command_body(recorded) or recorded
            if _looks_like_test_command(body):
                candidates.append(body.strip())

    selected: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = " ".join(candidate.split())
        if not key or key in seen:
            continue
        seen.add(key)
        if safe_test_invocation(candidate) is None:
            # Shell-dependent text is not a runnable claim; never a candidate.
            continue
        selected.append(candidate)
        if len(selected) >= MAX_REEXECUTED_COMMANDS:
            break
    return tuple(selected)


async def reexecute_test_commands(
    commands: Sequence[str],
    *,
    cwd: str,
    env: Mapping[str, str],
    timeout_seconds: float,
) -> tuple[CommandObservation, ...]:
    """Run each command as a direct argv (no shell) and record what happened."""
    observations: list[CommandObservation] = []
    for command in commands:
        invocation = safe_test_invocation(command)
        if invocation is None:
            continue
        env_delta, argv = invocation
        # ``run_with_shell`` executes exactly the argv it is given; no shell
        # is placed in front, so leaf-authored text cannot be interpreted.
        run = await run_with_shell(
            argv,
            cwd=cwd,
            env={**env, **env_delta} if env_delta else env,
            timeout_seconds=timeout_seconds,
        )
        if run.start_error is not None:
            # Could not start: no observation at all, not a failed one.
            continue
        observations.append(
            CommandObservation(
                command=command,
                returncode=run.returncode,
                output_tail=run.output[-OUTPUT_TAIL_CHARS:],
                timed_out=run.timed_out,
            )
        )
    return tuple(observations)


__all__ = [
    "MAX_REEXECUTED_COMMANDS",
    "reexecute_test_commands",
    "safe_test_argv",
    "safe_test_invocation",
    "select_test_reexecution_commands",
]
