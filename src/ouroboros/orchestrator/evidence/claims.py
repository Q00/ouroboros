"""Runtime transcript claim-matching helpers."""

from __future__ import annotations

from pathlib import Path
import re
import shlex

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.common import _flatten_evidence_values
from ouroboros.orchestrator.evidence.shell_parsing import (
    _has_trailing_output_filter_pipeline,
    _normalized_command_claim_aliases,
    _single_command_after_safe_shell_preamble,
    _test_command_invocation,
    _test_command_invocation_allowing_output_plumbing,
)


def _runtime_message_search_text(message: AgentMessage) -> str:
    """Build searchable transcript text for one non-final runtime message."""
    parts: list[str] = [message.content]
    if message.tool_name:
        parts.append(message.tool_name)
    tool_input = message.data.get("tool_input")
    if isinstance(tool_input, dict):
        parts.extend(str(value) for value in tool_input.values() if value is not None)
    parts.extend(_flatten_evidence_values(message.data))
    return "\n".join(parts).lower()


def _runtime_message_file_path_values(message: AgentMessage) -> tuple[str, ...]:
    """Return explicit file path values carried by a runtime message.

    Codex/OpenCode file-change events may report absolute workspace paths while
    typed evidence should normally claim workspace-relative paths. Keep this
    structured path extraction separate from broad text search so read-only text
    mentions still cannot prove ``files_touched``.
    """
    path_keys = {
        "file_path",
        "filepath",
        "filePath",
        "notebook_path",
        "notebookPath",
        "path",
        "target_file",
        "targetFile",
    }
    values: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in path_keys and isinstance(child, str) and child.strip():
                    values.append(child.strip())
                else:
                    visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    for container_key in ("tool_input", "input", "arguments", "args"):
        visit(message.data.get(container_key))
    return tuple(values)


def _runtime_message_command_values(message: AgentMessage) -> tuple[str, ...]:
    """Return explicit command strings carried by a runtime message.

    Runtime adapters normalize shell calls slightly differently.  Codex-like
    events usually expose ``tool_input.command``; Goose may expose ``cmd`` or a
    list argv form.  Keep extraction structured, not prose-based, so command
    evidence does not fall back to arbitrary assistant text.
    """
    values: list[str] = []
    for container_key in ("tool_input", "input", "arguments", "args"):
        container = message.data.get(container_key)
        if not isinstance(container, dict):
            continue
        for command_key in ("command", "cmd", "command_line"):
            command = container.get(command_key)
            normalized = _runtime_command_value_to_text(command)
            if normalized and normalized not in values:
                values.append(normalized)
    return tuple(values)


def _runtime_command_value_to_text(value: object) -> str | None:
    """Normalize a structured runtime command value into shell text."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list) and value:
        return shlex.join(str(part) for part in value)
    return None


def _file_claim_matches_runtime_path(
    claim: str,
    runtime_path: str,
    *,
    task_cwd: str | None,
) -> bool:
    """Return True when a claimed workspace path matches a runtime path value."""
    claim_path = Path(claim.strip())
    if not claim_path or claim_path.is_absolute() or ".." in claim_path.parts:
        return False

    runtime_path = runtime_path.strip()
    if not runtime_path:
        return False

    runtime_candidate = Path(runtime_path)
    if task_cwd is not None:
        base = Path(task_cwd).resolve()
        claimed_absolute = (base / claim_path).resolve()
        runtime_absolute = (
            runtime_candidate if runtime_candidate.is_absolute() else base / runtime_candidate
        ).resolve()
        try:
            claimed_absolute.relative_to(base)
            runtime_absolute.relative_to(base)
        except ValueError:
            return False
        return runtime_absolute == claimed_absolute

    # Without a trusted cwd, an absolute runtime path cannot prove that the
    # claimed relative file belongs to the task workspace. Basename/suffix
    # matching would admit arbitrary out-of-scope files. A structured relative
    # path may still match exactly because both sides remain workspace-relative.
    if runtime_candidate.is_absolute() or ".." in runtime_candidate.parts:
        return False
    normalized_claim = claim_path.as_posix().lower()
    normalized_runtime = runtime_candidate.as_posix().lower()
    return normalized_runtime == normalized_claim


def _workspace_relative_file_claim(value: str, *, task_cwd: str | None) -> str | None:
    """Normalize a files_touched claim to a workspace-relative path.

    The evidence producer should emit workspace-relative paths, but live Codex
    runs may still report absolute files under the disposable target repo.  Treat
    those as the same claim only after proving they resolve inside ``task_cwd``.
    Paths outside the workspace, empty paths, and relative traversal remain
    unsupported evidence claims.
    """
    raw_value = value.strip()
    if not raw_value or task_cwd is None:
        return None

    base = Path(task_cwd).resolve()
    candidate = Path(raw_value)
    if not candidate.is_absolute() and ".." in candidate.parts:
        return None

    resolved = (candidate if candidate.is_absolute() else base / candidate).resolve()
    try:
        relative = resolved.relative_to(base)
    except ValueError:
        return None

    if not relative.parts or ".." in relative.parts:
        return None
    return relative.as_posix()


def _runtime_support_messages_for_field(
    field_name: str,
    messages: tuple[AgentMessage, ...],
) -> tuple[AgentMessage, ...]:
    """Narrow support messages for profile-known evidence fields."""
    normalized = field_name.lower()
    if normalized == "files_touched":
        return messages
    if normalized in {"commands_run", "tests_passed"}:
        return tuple(message for message in messages if message.tool_name == "Bash")
    return messages


def _runtime_messages_support_claim(value: str, messages: tuple[AgentMessage, ...]) -> bool:
    """Return True when a non-final runtime message backs a claim string."""
    needle = value.strip().lower()
    return bool(needle) and any(
        needle in _runtime_message_search_text(message) for message in messages
    )


def _runtime_message_supports_command_claim(value: str, message: AgentMessage) -> bool:
    """Return True when one runtime message backs a command claim.

    Codex commonly records the executed Bash command as a shell wrapper such as
    ``/bin/zsh -lc 'cd /workspace && python -m unittest "test_hello.py"'``
    while typed evidence may claim the inner test command.  Treat those as
    equivalent only through the structured Bash command field; arbitrary output
    text or assistant narration must not create command aliases.
    """
    if message.tool_name != "Bash":
        return _runtime_messages_support_claim(value, (message,))
    claim_aliases = set(_normalized_command_claim_aliases(value))
    claim_test_invocation = _test_command_invocation(value)
    for runtime_command in _runtime_message_command_values(message):
        runtime_aliases = set(_normalized_command_claim_aliases(runtime_command))
        if claim_aliases and runtime_aliases and claim_aliases.intersection(runtime_aliases):
            return True

        runtime_inner_command = _single_command_after_safe_shell_preamble(runtime_command)
        if runtime_inner_command and runtime_inner_command in claim_aliases:
            return True

        runtime_test_invocation = _test_command_invocation(runtime_command)
        if (
            claim_test_invocation
            and runtime_test_invocation
            and (
                runtime_test_invocation == claim_test_invocation
                or runtime_test_invocation.startswith(claim_test_invocation + " ")
            )
        ):
            return True
    return False


def _runtime_messages_support_command_claim(
    value: str,
    messages: tuple[AgentMessage, ...],
) -> bool:
    """Return True when runtime messages back a command claim."""
    return any(_runtime_message_supports_command_claim(value, message) for message in messages)


def _runtime_messages_have_masked_test_command_form(
    value: str,
    messages: tuple[AgentMessage, ...],
) -> bool:
    """Return True when a test command claim matches only after unsafe plumbing.

    This deliberately does NOT prove the command claim. It distinguishes a real
    transcript shape that failed the evidence contract (for example a test run
    piped through ``tail`` without ``set -o pipefail``) from a fabrication where
    no related test command appears at all.
    """
    claim_invocation = _test_command_invocation(value)
    if claim_invocation is None:
        return False
    for message in messages:
        if message.tool_name != "Bash":
            continue
        for runtime_command in _runtime_message_command_values(message):
            if not _has_trailing_output_filter_pipeline(runtime_command):
                continue
            runtime_invocation = _test_command_invocation_allowing_output_plumbing(runtime_command)
            if runtime_invocation is None:
                continue
            if runtime_invocation == claim_invocation or runtime_invocation.startswith(
                claim_invocation + " "
            ):
                return True
    return False


def _runtime_messages_support_file_claim(
    value: str,
    messages: tuple[AgentMessage, ...],
    *,
    task_cwd: str | None,
) -> bool:
    """Return True when runtime transcript evidence backs a workspace file claim.

    Existence alone is not sufficient for ``files_touched``: a stale file in the
    workspace must not prove that this run created or modified it. Exact
    transcript support is preferred; basename support is accepted only when the
    claimed relative path resolves inside the active workspace, which covers
    tool outputs that report ``generated.py`` instead of ``src/generated.py``.
    """
    if task_cwd is not None:
        # Workspace is KNOWN: every ``files_touched`` claim must resolve inside
        # it. ``_workspace_relative_file_claim`` returns None for an absolute
        # outside-workspace claim or a ``..`` traversal that escapes the cwd, and
        # such claims are rejected here — they never fall through to command-text
        # mutation proof (which would otherwise let ``touch /tmp/outside.py`` back
        # an out-of-scope claim). The relative↔absolute (and macOS
        # ``/tmp`` <-> ``/private/tmp``) form mismatch is already handled by the
        # tiers below: ``_file_claim_matches_runtime_path`` resolves BOTH sides
        # against the workspace, so the permissive raw tier is unnecessary here.
        relative_claim = _workspace_relative_file_claim(value, task_cwd=task_cwd)
        if relative_claim is None:
            return False
        candidate = Path(relative_claim)
        base = Path(task_cwd).resolve()
        resolved = (base / candidate).resolve()
        if any(
            _runtime_message_supports_file_reference(
                relative_claim,
                message,
                messages=messages,
                index=index,
                task_cwd=task_cwd,
            )
            for index, message in enumerate(messages)
        ):
            return True
        if resolved.exists():
            basename = candidate.name.strip().lower()
            if basename and any(
                _runtime_message_supports_file_reference(
                    basename,
                    message,
                    messages=messages,
                    index=index,
                    task_cwd=task_cwd,
                    allow_bash_command_text=False,
                )
                for index, message in enumerate(messages)
            ):
                return True
        return False

    # Workspace is UNKNOWN (``task_cwd`` is None — the original live case where no
    # cwd was threaded to the verifier). Be conservative and scope to the
    # transcript's own structured mutation events: accept only an exact
    # exact workspace-relative Edit/Write/NotebookEdit path. An absolute runtime
    # path cannot be scoped without a trusted cwd and is rejected rather than
    # matched by basename/suffix; arbitrary ``touch <abspath>`` command text is
    # likewise never trusted here.
    raw_claim = value.strip()
    if not raw_claim:
        return False
    return any(
        message.tool_name in {"Edit", "Write", "NotebookEdit"}
        and _runtime_message_has_success_evidence(
            message,
            messages=messages,
            index=index,
        )
        and any(
            _file_claim_matches_runtime_path(raw_claim, path, task_cwd=None)
            for path in _runtime_message_file_path_values(message)
        )
        for index, message in enumerate(messages)
    )


def _runtime_message_supports_file_reference(
    reference: str,
    message: AgentMessage,
    *,
    messages: tuple[AgentMessage, ...],
    index: int,
    task_cwd: str | None,
    allow_bash_command_text: bool = True,
) -> bool:
    """Return True when one message plausibly reports touching a file reference."""
    normalized_reference = reference.strip().lower()
    if not normalized_reference:
        return False
    text = _runtime_message_file_proof_text(message)
    if message.tool_name == "Bash":
        return _text_supports_file_mutation_reference(text, normalized_reference) or (
            allow_bash_command_text
            and _bash_command_mutates_file_reference(message, normalized_reference)
            and _runtime_message_has_success_evidence(message, messages=messages, index=index)
        )
    if message.tool_name in {"Edit", "Write", "NotebookEdit"}:
        return _runtime_message_has_success_evidence(
            message,
            messages=messages,
            index=index,
        ) and any(
            _file_claim_matches_runtime_path(reference, path, task_cwd=task_cwd)
            for path in _runtime_message_file_path_values(message)
        )
    return _text_supports_file_mutation_reference(text, normalized_reference)


def _text_supports_file_mutation_reference(text: str, normalized_reference: str) -> bool:
    """Return True when text pairs a file reference with mutation language."""
    if not text:
        return False
    reference_pattern = _file_reference_pattern(normalized_reference)
    if not reference_pattern.search(text):
        return False
    return bool(
        re.search(
            rf"(?<![\w./-]){re.escape(normalized_reference)}(?![\w./-]).*\b("
            r"updated|modified|changed|created|generated|wrote|written|patched"
            r")\b|\b("
            r"updated|modified|changed|created|generated|wrote|written|patched"
            rf")\b.*(?<![\w./-]){re.escape(normalized_reference)}(?![\w./-])",
            text,
        )
    )


def _file_reference_pattern(normalized_reference: str) -> re.Pattern[str]:
    """Return a conservative token pattern for a workspace-relative file reference."""
    return re.compile(rf"(?<![\w./-]){re.escape(normalized_reference)}(?![\w./-])")


def _bash_command_mutates_file_reference(message: AgentMessage, normalized_reference: str) -> bool:
    """Return True for explicit shell writes to the referenced file.

    Bash command text is only trusted when the command itself carries mutation
    semantics for the claimed file. This preserves direct shell-edit evidence
    such as ``touch src/generated.py`` or ``printf ... > src/generated.py``
    without allowing read-only probes like ``grep updated src/generated.py`` to
    prove ``files_touched`` merely by containing a path and a mutation word.
    """
    tool_input = message.data.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    command = tool_input.get("command")
    if not isinstance(command, str):
        return False
    normalized_command = command.strip().lower()
    if not normalized_command:
        return False
    if not _file_reference_pattern(normalized_reference).search(normalized_command):
        return False
    quoted_reference = rf"['\"]?{re.escape(normalized_reference)}['\"]?"
    if re.search(rf"(^|[\s;&|])(?:\d?>|&>|>>|\d>>)\s*{quoted_reference}", normalized_command):
        return True
    return bool(
        re.search(
            rf"(^|[\s;&|])(touch|truncate|tee)\b[^;&|]*\s{quoted_reference}(?=$|[\s;&|])",
            normalized_command,
        )
        or re.search(
            rf"(^|[\s;&|])(sed|perl)\b[^;&|]*\s-[^\s;&|]*i[^;&|]*\s"
            rf"{quoted_reference}(?=$|[\s;&|])",
            normalized_command,
        )
    )


def _runtime_message_has_success_signal(message: AgentMessage) -> bool:
    """Return True only for explicit, machine-readable tool success evidence.

    Free-form words such as ``success`` are deliberately not sufficient here:
    a failed tool result can contain those words in a path, command, or error
    message.  Mutation evidence must be tied to a normalized status, exit code,
    or structured tool-result error bit.
    """
    if message.is_error:
        return False
    tool_result = message.data.get("tool_result")
    if message.data.get("is_error_invalid") is True:
        return False
    if "is_error" in message.data and not isinstance(message.data["is_error"], bool):
        return False
    if isinstance(message.data.get("is_error"), bool) and message.data["is_error"]:
        return False
    if tool_result is not None and not isinstance(tool_result, dict):
        return False
    if isinstance(tool_result, dict):
        if tool_result.get("is_error_invalid") is True:
            return False
        if "is_error" in tool_result and not isinstance(tool_result["is_error"], bool):
            return False
        if tool_result.get("is_error") is True:
            return False
    is_completion = _runtime_message_is_tool_completion(message)
    success_signal = is_completion and (
        message.data.get("is_error") is False
        or (isinstance(tool_result, dict) and tool_result.get("is_error") is False)
    )
    if "exit_code" in message.data:
        exit_code = message.data["exit_code"]
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            return False
        if exit_code != 0:
            return False
        success_signal = True
    if message.data.get("subtype") == "success":
        success_signal = True
    status = message.data.get("status")
    if isinstance(status, str):
        normalized_status = status.strip().lower()
        if normalized_status in {"failed", "error"}:
            return False
        if normalized_status in {"completed", "success", "succeeded"}:
            success_signal = True
    runtime_event_type = message.data.get("runtime_event_type")
    if isinstance(runtime_event_type, str):
        normalized_event_type = runtime_event_type.strip().lower()
        if normalized_event_type.endswith((".failed", ".error")):
            return False
        if normalized_event_type.endswith((".completed", ".succeeded")):
            success_signal = True
    return success_signal


def _runtime_message_tool_call_id(message: AgentMessage) -> str | None:
    """Return the normalized tool-call correlation id carried by a message."""
    for key in ("tool_call_id", "tool_use_id", "call_id"):
        value = message.data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    tool_result = message.data.get("tool_result")
    if isinstance(tool_result, dict):
        for key in ("tool_call_id", "tool_use_id", "call_id"):
            value = tool_result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        meta = tool_result.get("meta")
        if isinstance(meta, dict):
            for key in ("tool_call_id", "tool_use_id", "call_id"):
                value = meta.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def _runtime_message_is_tool_completion(message: AgentMessage) -> bool:
    """Return whether a message is a normalized tool completion/result."""
    if message.type == "tool_result" or message.data.get("subtype") == "tool_result":
        return True
    if isinstance(message.data.get("tool_result"), dict):
        return True
    runtime_event_type = message.data.get("runtime_event_type")
    return isinstance(runtime_event_type, str) and runtime_event_type.strip().lower().endswith(
        ("tool.completed", "tool.failed")
    )


def _runtime_message_has_success_evidence(
    message: AgentMessage,
    *,
    messages: tuple[AgentMessage, ...],
    index: int,
) -> bool:
    """Return True when a tool call itself or its correlated result proves success.

    Calls with ids require an exact id match. Legacy id-less streams are
    accepted only when the next tool-related message is one unambiguous result
    for the same named tool (or an unnamed adjacent result). Missing, failed, or
    ambiguous completion evidence fails closed.
    """
    call_id = _runtime_message_tool_call_id(message)
    if call_id is not None:
        matching_starts = tuple(
            candidate
            for candidate in messages
            if candidate.tool_name is not None
            and not _runtime_message_is_tool_completion(candidate)
            and _runtime_message_tool_call_id(candidate) == call_id
        )
        if len(matching_starts) != 1:
            return False
    if _runtime_message_has_success_signal(message):
        return True
    return _runtime_message_has_following_success(messages, index)


def _runtime_message_has_following_success(messages: tuple[AgentMessage, ...], index: int) -> bool:
    """Return True when a tool call has one correlated successful completion."""
    start = messages[index]
    start_call_id = _runtime_message_tool_call_id(start)
    if start_call_id is not None:
        matching_starts = tuple(
            candidate
            for candidate in messages
            if candidate.tool_name is not None
            and not _runtime_message_is_tool_completion(candidate)
            and _runtime_message_tool_call_id(candidate) == start_call_id
        )
        if len(matching_starts) != 1:
            return False
        matching_completions = tuple(
            candidate
            for candidate in messages[index + 1 :]
            if _runtime_message_is_tool_completion(candidate)
            and _runtime_message_tool_call_id(candidate) == start_call_id
        )
        return (
            len(matching_completions) == 1
            and (
                matching_completions[0].tool_name is None
                or matching_completions[0].tool_name == start.tool_name
            )
            and _runtime_message_has_success_signal(matching_completions[0])
        )

    for candidate in messages[index + 1 :]:
        candidate_call_id = _runtime_message_tool_call_id(candidate)
        if _runtime_message_is_tool_completion(candidate):
            # An id-bearing result cannot be safely assigned to an id-less
            # start, and an explicit different tool name is contradictory.
            if candidate_call_id is not None:
                return False
            if candidate.tool_name is not None and candidate.tool_name != start.tool_name:
                return False
            return _runtime_message_has_success_signal(candidate)

        # A subsequent tool invocation makes an id-less association ambiguous.
        if candidate.tool_name is not None and not _runtime_message_is_tool_completion(candidate):
            return False
    return False


def _runtime_message_file_proof_text(message: AgentMessage) -> str:
    """Return text that can prove a file was touched by the current run.

    For Bash tool invocations, command text is not proof by itself: read-only
    commands such as ``grep updated src/app.py`` can contain both the claimed
    path and mutation verbs. Trust Bash result/output fields instead. Dedicated
    edit/write tools still expose their tool inputs because their tool identity
    supplies the mutation semantics.
    """
    if message.tool_name == "Bash":
        parts: list[str] = []
        for key in ("result_preview", "output", "stdout", "stderr"):
            value = message.data.get(key)
            if isinstance(value, str):
                parts.append(value)
        return "\n".join(parts).lower()
    return _runtime_message_search_text(message)
