"""Test-success evidence detection helpers."""

from __future__ import annotations

import re

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.claims import (
    _runtime_message_command_values,
    _runtime_message_search_text,
    _runtime_message_supports_command_claim,
    _runtime_messages_support_file_claim,
)
from ouroboros.orchestrator.evidence.common import _normalized_evidence_text
from ouroboros.orchestrator.evidence.shell_parsing import (
    _has_trailing_output_filter_pipeline,
    _is_python_executable,
    _looks_like_test_command,
    _looks_like_unittest_command,
    _normalized_command_claim_aliases,
    _runtime_command_evidence_aliases,
    _test_command_invocation,
    _test_command_invocation_allowing_output_plumbing,
)


def _runtime_messages_have_masked_test_command_for_test_claim(
    *,
    value: str,
    messages: tuple[AgentMessage, ...],
    task_cwd: str | None,
) -> bool:
    """Return True when a rejected test claim depends on masked test output.

    This is diagnostic only. It lets the verifier classify a dependent
    ``tests_passed`` failure with the same evidence-form mismatch as the
    rejected ``commands_run`` claim, while still refusing to accept the masked
    command as proof.
    """
    for index, message in enumerate(messages):
        if message.tool_name != "Bash":
            continue
        masked_invocations: list[str] = []
        for runtime_command in _runtime_message_command_values(message):
            if not _has_trailing_output_filter_pipeline(runtime_command):
                continue
            runtime_invocation = _test_command_invocation_allowing_output_plumbing(runtime_command)
            if runtime_invocation is not None:
                masked_invocations.append(runtime_invocation)
        if not masked_invocations:
            continue

        chunk = [message]
        for following in messages[index + 1 :]:
            if following.tool_name and not _is_tool_result_message(following):
                break
            chunk.append(following)
        if not any(_message_contains_test_success(item) for item in chunk):
            continue
        chunk_text = "\n".join(_runtime_message_search_text(item) for item in chunk)
        chunk_test_proof_text = "\n".join(_runtime_message_test_proof_text(item) for item in chunk)
        if any(
            _test_command_targets_claim(
                command=command,
                claim=value,
                chunk_text=chunk_text,
                chunk_test_proof_text=chunk_test_proof_text,
                messages=messages,
                task_cwd=task_cwd,
            )
            for command in masked_invocations
        ):
            return True
    return False


def _text_contains_unittest_success(text: str) -> bool:
    """Return True for real unittest success output."""
    return _text_contains_test_success(text) and bool(
        re.search(r"\bran\s+[1-9]\d*\s+tests?\b[\s\S]*\bok\b", text.lower())
    )


def _text_contains_test_success(text: str) -> bool:
    """Return True when text contains a conservative test-success signal."""
    text = text.lower()
    zero_failure_pattern = (
        r"\b(0\s+(failed|failures?|errors?)|"
        r"(failed|failures?|errors?)\s*[:=]\s*0|"
        r"no\s+(tests?\s+)?(failed|failures?|errors?))\b"
    )
    failure_scan_text = re.sub(zero_failure_pattern, "", text)
    if re.search(
        r"\b[1-9]\d*\s+(failed|failures?|errors?)\b|"
        r"\b(failed|failure|failures?|error|errors)\b|"
        r"exit\s*code\s*[1-9]",
        failure_scan_text,
    ):
        return False
    if re.search(r"\b0\s+passed\b", text) and not re.search(r"\b[1-9]\d*\s+passed\b", text):
        return False
    if re.search(r"\btask\s+[:\w.-]*test\b[^\n]*(no-source|skipped)\b", text):
        return False
    if re.search(r"\b0\s+tests?\s+(completed|run|executed)\b", text):
        return False
    if re.search(r"\bno\s+tests?\s+(found|run|executed)\b", text):
        return False
    return bool(
        re.search(
            r"\b([1-9]\d*\s+passed|passed|pass|success|successful|succeeded)\b|"
            r"\bbuild\s+successful\b|exit\s*code\s*0",
            text,
        )
        or re.search(r"\bran\s+[1-9]\d*\s+tests?\b[\s\S]*\bok\b", text)
    )


def _message_contains_test_success(message: AgentMessage) -> bool:
    """Return True when one message says a test command passed."""
    parts = [message.content]
    for key in ("result_preview", "output", "stdout", "status", "subtype"):
        value = message.data.get(key)
        if isinstance(value, str):
            parts.append(value)
    exit_code = message.data.get("exit_code")
    if type(exit_code) is int:
        parts.append(f"exit code {exit_code}")
    return _text_contains_test_success("\n".join(parts))


def _runtime_message_test_proof_text(message: AgentMessage) -> str:
    """Return runtime-produced text that can prove test output for a Bash chunk.

    Assistant narration after a Bash call is useful transcript context, but it
    is not runtime output for that command. Keep summary matching tied to the
    Bash output/result payloads and tool-result messages that runtimes emit.
    """
    resultish = (
        message.type in {"result", "tool_result"} or message.data.get("subtype") == "tool_result"
    )
    parts: list[str] = []
    if resultish:
        parts.append(message.content)
    for key in ("result_preview", "output", "stdout", "stderr", "tool_result_text"):
        value = message.data.get(key)
        if isinstance(value, str):
            parts.append(value)
    tool_result = message.data.get("tool_result")
    if isinstance(tool_result, dict):
        for key in ("text_content", "content", "output", "stdout", "stderr"):
            value = tool_result.get(key)
            if isinstance(value, str):
                parts.append(value)
    elif isinstance(tool_result, str):
        parts.append(tool_result)
    return "\n".join(parts)


def _is_tool_result_message(message: AgentMessage) -> bool:
    """Return True for runtime tool-result messages, including named-tool variants."""
    return message.type in {"result", "tool_result"} or message.data.get("subtype") == "tool_result"


def _test_claim_file_part(value: str) -> str | None:
    """Return the file path portion of a pytest node-id style claim."""
    stripped = value.strip()
    if not stripped:
        return None
    file_part = stripped.split("::", 1)[0].strip()
    return file_part or None


def _claim_summary_matches_runtime_chunk(
    *,
    command: str,
    claim: str,
    chunk_text: str,
) -> bool:
    """Return True when a command+summary claim is present in runtime output.

    This keeps the verifier transcript-driven: the claim may combine the backed
    command and a unittest-style success summary, but the summary itself must
    also appear in the runtime chunk. The claim text alone is never proof.
    """
    normalized_claim = _normalized_evidence_text(claim)
    normalized_chunk = _normalized_evidence_text(chunk_text)
    summary = ""
    for normalized_command in _normalized_command_claim_aliases(command):
        if normalized_command in normalized_claim:
            summary = normalized_claim.split(normalized_command, 1)[1].strip(" :-")
            break
    if not summary or summary not in normalized_chunk:
        return False
    if (
        summary == "ok"
        and _looks_like_unittest_command(command)
        and _text_contains_unittest_success(chunk_text)
    ):
        return True
    return _text_contains_test_success(summary)


def _claim_contains_command_success_summary(*, command: str, claim: str) -> bool:
    """Return True when a test claim appends a success summary to a command."""
    normalized_claim = _normalized_evidence_text(claim)
    for normalized_command in _normalized_command_claim_aliases(command):
        if normalized_command in normalized_claim:
            summary = normalized_claim.split(normalized_command, 1)[1].strip(" :-")
            return bool(summary) and _text_contains_test_success(summary)
    return False


def _test_command_targets_claim(
    *,
    command: str,
    claim: str,
    chunk_text: str,
    chunk_test_proof_text: str,
    messages: tuple[AgentMessage, ...],
    task_cwd: str | None,
) -> bool:
    """Return True when a successful test command can cover a test claim."""
    needle = claim.strip().lower()
    if _claim_contains_command_success_summary(command=command, claim=claim):
        return _claim_summary_matches_runtime_chunk(
            command=command,
            claim=claim,
            chunk_text=chunk_test_proof_text,
        )
    if needle and needle in chunk_text:
        return True

    file_part = _test_claim_file_part(claim)
    if file_part is None:
        return False
    normalized_file = file_part.lower()
    normalized_command = command.lower()
    if normalized_file in chunk_text or normalized_file in normalized_command:
        return True
    if _claim_summary_matches_runtime_chunk(
        command=command,
        claim=claim,
        chunk_text=chunk_test_proof_text,
    ):
        return True

    # A broad suite command such as ``pytest`` can cover a node-id claim when
    # the claimed test file is also backed by current-run mutation evidence.
    # Existence alone is deliberately insufficient: otherwise a transcript with
    # unrelated ``pytest`` output could prove any stale test file in the tree.
    command_parts = (_test_command_invocation(command) or normalized_command).split()
    broad_pytest = command_parts in (["pytest"], ["py.test"]) or (
        len(command_parts) >= 3
        and _is_python_executable(command_parts[-3])
        and command_parts[-2:] == ["-m", "pytest"]
    )
    if not broad_pytest or task_cwd is None:
        return False
    return _runtime_messages_support_file_claim(file_part, messages, task_cwd=task_cwd)


def _runtime_messages_support_test_claim(
    *,
    value: str,
    backed_commands: tuple[str, ...],
    messages: tuple[AgentMessage, ...],
    task_cwd: str | None,
) -> bool:
    """Return True when a backed test command chunk proves one test claim."""
    needle = value.strip().lower()
    if not needle:
        return False
    for index, message in enumerate(messages):
        if message.tool_name != "Bash":
            continue
        # Candidate test commands are drawn from two transcript-grounded
        # sources: (1) ``commands_run`` evidence entries already proven against
        # the transcript, and (2) the Bash message's own recorded command. The
        # latter is backed by definition — it is the literal invocation in the
        # transcript — so a real ``pytest <file>`` run can support a node-id
        # ``tests_passed`` claim even when the agent did not also echo that
        # exact command into its ``commands_run`` evidence.
        #
        # These are NOT three independent checks. For a per-message candidate
        # the ``_runtime_message_supports_command_claim`` gate below is
        # tautological — the candidate is that message's own command, so it
        # trivially supports itself. The anti-fabrication guarantee is carried
        # entirely by the downstream gates: ``_message_contains_test_success``
        # (reads only structured runtime output, never agent narration) and
        # ``_test_command_targets_claim`` (anchors the claim's node-id/file to
        # the recorded command + proof text). The ``_looks_like_test_command``
        # filter still excludes non-test commands from this candidate source.
        candidate_commands = (*backed_commands, *_runtime_message_command_values(message))
        matching_commands = tuple(
            candidate
            for candidate in candidate_commands
            if _looks_like_test_command(candidate)
            and _runtime_message_supports_command_claim(candidate, message)
        )
        if not matching_commands:
            continue
        chunk = [message]
        for following in messages[index + 1 :]:
            if following.tool_name and not _is_tool_result_message(following):
                break
            chunk.append(following)
        if not any(_message_contains_test_success(item) for item in chunk):
            continue
        chunk_text = "\n".join(_runtime_message_search_text(item) for item in chunk)
        chunk_test_proof_text = "\n".join(_runtime_message_test_proof_text(item) for item in chunk)
        if any(
            _test_command_targets_claim(
                command=command,
                claim=value,
                chunk_text=chunk_text,
                chunk_test_proof_text=chunk_test_proof_text,
                messages=messages,
                task_cwd=task_cwd,
            )
            for command in matching_commands
        ):
            return True
    return False


def _successful_runtime_test_commands(messages: tuple[AgentMessage, ...]) -> set[str]:
    """Return Bash test commands backed by adjacent runtime success output."""
    commands: set[str] = set()
    for index, message in enumerate(messages):
        if message.tool_name != "Bash":
            continue
        message_commands = {
            alias
            for command in _runtime_message_command_values(message)
            if _looks_like_test_command(command)
            for alias in _runtime_command_evidence_aliases(command)
        }
        if not message_commands:
            continue
        chunk = [message]
        for following in messages[index + 1 :]:
            if following.tool_name and not _is_tool_result_message(following):
                break
            chunk.append(following)
        if any(
            not item.is_final
            and _text_contains_test_success(_runtime_message_test_proof_text(item))
            for item in chunk
        ):
            commands.update(command for command in message_commands if command)
    return commands
