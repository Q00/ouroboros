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

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.claims import _runtime_message_command_values
from ouroboros.orchestrator.evidence.common import _flatten_evidence_values
from ouroboros.orchestrator.evidence.harness_observation import CommandObservation
from ouroboros.orchestrator.evidence.shell_parsing import (
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
        selected.append(candidate)
        if len(selected) >= MAX_REEXECUTED_COMMANDS:
            break
    return tuple(selected)


async def reexecute_test_commands(
    commands: Sequence[str],
    *,
    cwd: str,
    shell_path: str,
    env: Mapping[str, str],
    timeout_seconds: float,
) -> tuple[CommandObservation, ...]:
    """Run each command through the verify shell and record what happened."""
    observations: list[CommandObservation] = []
    for command in commands:
        run = await run_with_shell(
            (shell_path, "-c", command),
            cwd=cwd,
            env=env,
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
    "select_test_reexecution_commands",
]
