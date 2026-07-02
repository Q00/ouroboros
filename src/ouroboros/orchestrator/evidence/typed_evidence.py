"""Typed evidence reconciliation helpers."""

from __future__ import annotations

from pathlib import Path
import re

from ouroboros.orchestrator.evidence.claims import (
    _runtime_message_command_values,
    _workspace_relative_file_claim,
)
from ouroboros.orchestrator.evidence.common import (
    _flatten_evidence_values,
    _normalize_exact_command,
    _normalized_evidence_text,
)
from ouroboros.orchestrator.evidence.shell_parsing import _runtime_command_evidence_aliases
from ouroboros.orchestrator.evidence.test_detection import _successful_runtime_test_commands
from ouroboros.orchestrator.parallel_executor_models import ACExecutionOutcome, ACExecutionResult


def _add_runtime_command_evidence(commands: set[str], command: str) -> None:
    """Add runtime command evidence without accepting compound shell aliases."""
    commands.update(_runtime_command_evidence_aliases(command))


def _typed_evidence_is_usable_for_sibling_reconciliation(result: ACExecutionResult) -> bool:
    """Return True when typed evidence was not rejected by validation/verifier."""
    if result.typed_evidence is None:
        return False
    if result.typed_evidence_error:
        return False
    if result.typed_evidence_validation is not None and not result.typed_evidence_validation.ok:
        return False
    return result.atomic_verifier_verdict is None or result.atomic_verifier_verdict.passed


def _typed_file_evidence_proves_current_existence(
    result: ACExecutionResult,
    relative_path: str,
) -> bool:
    """Return True when typed file evidence is backed by current end-state existence."""
    if result.runtime_handle is None or result.runtime_handle.cwd is None:
        return False
    candidate = (Path(result.runtime_handle.cwd).resolve() / relative_path).resolve()
    try:
        candidate.relative_to(Path(result.runtime_handle.cwd).resolve())
    except ValueError:
        return False
    return candidate.is_file()


def _evidence_values_from_result(result: ACExecutionResult) -> tuple[set[str], set[str], set[str]]:
    """Return normalized file paths, run commands, and passed commands.

    This intentionally uses only structured/runtime evidence, not broad natural
    language success claims, so sibling AC completion remains conservative.
    """
    files: set[str] = set()
    run_commands: set[str] = set()
    passed_commands: set[str] = set()

    if _typed_evidence_is_usable_for_sibling_reconciliation(result):
        assert result.typed_evidence is not None
        task_cwd = result.runtime_handle.cwd if result.runtime_handle is not None else None
        for value in _flatten_evidence_values(result.typed_evidence.get("files_touched")):
            normalized = (
                _workspace_relative_file_claim(str(value), task_cwd=task_cwd) or str(value).strip()
            )
            if normalized and _typed_file_evidence_proves_current_existence(result, normalized):
                files.add(normalized)
        for value in _flatten_evidence_values(result.typed_evidence.get("commands_run")):
            _add_runtime_command_evidence(run_commands, str(value))
        for value in _flatten_evidence_values(result.typed_evidence.get("tests_passed")):
            _add_runtime_command_evidence(passed_commands, str(value))
            _add_runtime_command_evidence(run_commands, str(value))

    for message in result.messages:
        if not message.tool_name:
            continue

        if message.tool_name == "Bash":
            for command in _runtime_message_command_values(message):
                _add_runtime_command_evidence(run_commands, command)
            continue

    passed_commands.update(_successful_runtime_test_commands(result.messages))
    return files, run_commands, passed_commands


def _criterion_satisfied_by_evidence(
    criterion: str,
    files: set[str],
    run_commands: set[str],
    passed_commands: set[str] | None = None,
) -> bool:
    """Conservatively decide whether evidence satisfies a sibling criterion."""
    normalized_run_commands = {
        _normalize_exact_command(command) for command in run_commands if command
    }
    normalized_passed_commands = {
        _normalize_exact_command(command) for command in (passed_commands or set()) if command
    }

    for file_path in files:
        if file_path and _criterion_is_exact_file_presence_ac(criterion, file_path):
            return True

    for command in normalized_passed_commands:
        if command and _criterion_is_exact_command_pass_ac(criterion, command):
            return True

    for command in normalized_run_commands:
        if command and _criterion_is_exact_command_run_ac(criterion, command):
            return True

    return False


def _criterion_is_exact_file_presence_ac(criterion: str, file_path: str) -> bool:
    """Return True when the criterion is only an exact file-presence AC."""
    normalized_path = Path(file_path.strip()).as_posix()
    if (
        not normalized_path
        or Path(normalized_path).is_absolute()
        or ".." in Path(normalized_path).parts
    ):
        return False
    inline_code_paths = [
        Path(match.group(1).strip()).as_posix()
        for match in re.finditer(r"`([^`]+)`", criterion)
        if match.group(1).strip()
    ]
    if inline_code_paths != [normalized_path]:
        return False

    normalized = _normalized_evidence_text(
        re.sub(r"`[^`]+`", "<path>", criterion).strip().rstrip(".")
    )
    return normalized in {
        "<path> exists",
        "<path> is present",
        "the file <path> exists",
        "the file <path> is present",
    }


def _criterion_inline_code_values(criterion: str) -> list[str]:
    """Return normalized inline-code fragments from a criterion."""
    return [
        _normalize_exact_command(match.group(1).strip())
        for match in re.finditer(r"`([^`]+)`", criterion)
        if match.group(1).strip()
    ]


def _criterion_is_exact_command_pass_ac(criterion: str, command: str) -> bool:
    """Return True when the criterion is only an exact command-pass AC."""
    normalized_command = _normalize_exact_command(command)
    if not normalized_command or _criterion_inline_code_values(criterion) != [normalized_command]:
        return False
    normalized = _normalized_evidence_text(
        re.sub(r"`[^`]+`", "<command>", criterion).strip().rstrip(".")
    )
    return normalized in {
        "<command> passes",
        "<command> passed",
        "<command> succeeds",
        "<command> succeeded",
        "the exact command <command> passes",
        "the exact command <command> passed",
        "the exact command <command> succeeds",
        "the exact command <command> succeeded",
        "the exact command <command> exits with code 0",
        "the command <command> passes",
        "the command <command> succeeds",
    }


def _criterion_is_exact_command_run_ac(criterion: str, command: str) -> bool:
    """Return True when the criterion is only an exact command-run AC."""
    normalized_command = _normalize_exact_command(command)
    if not normalized_command or _criterion_inline_code_values(criterion) != [normalized_command]:
        return False
    normalized = _normalized_evidence_text(
        re.sub(r"`[^`]+`", "<command>", criterion).strip().rstrip(".")
    )
    return normalized in {
        "run <command>",
        "run the exact command <command>",
        "execute <command>",
        "execute the exact command <command>",
        "the exact command <command> runs",
        "the command <command> runs",
    }


def _complete_sibling_acs_from_evidence(
    *,
    level_results: list[ACExecutionResult],
    ac_statuses: dict[int, str],
    failed_indices: set[int],
    completed_count: int,
    level_success: int,
    level_failed: int,
) -> tuple[int, int, int, list[ACExecutionResult]]:
    """Mark sibling ACs satisfied when a successful sibling has exact evidence.

    Observation jobs often ask for separate ACs such as "file exists", "test
    file exists", and "pytest passes". A single worker can legitimately create
    both files and run the test. This function reconciles those concrete sibling
    ACs from runtime/typed evidence instead of leaving them failed or pending.
    """
    replacements: dict[int, ACExecutionResult] = {}
    successful_evidence = [
        (result.ac_index, *_evidence_values_from_result(result))
        for result in level_results
        if result.success
    ]
    if not successful_evidence:
        return completed_count, level_success, level_failed, level_results

    for result in level_results:
        if result.success or result.outcome != ACExecutionOutcome.FAILED:
            continue
        for source_idx, files, run_commands, passed_commands in successful_evidence:
            if source_idx == result.ac_index:
                continue
            if not _criterion_satisfied_by_evidence(
                result.ac_content,
                files,
                run_commands,
                passed_commands,
            ):
                continue
            replacements[result.ac_index] = ACExecutionResult(
                ac_index=result.ac_index,
                ac_content=result.ac_content,
                success=True,
                final_message=(f"Satisfied by runtime evidence from sibling AC {source_idx + 1}."),
                retry_attempt=result.retry_attempt,
                outcome=ACExecutionOutcome.SATISFIED_EXTERNALLY,
            )
            break

    if not replacements:
        return completed_count, level_success, level_failed, level_results

    reconciled: list[ACExecutionResult] = []
    for result in level_results:
        replacement = replacements.get(result.ac_index)
        if replacement is None:
            reconciled.append(result)
            continue
        if result.outcome == ACExecutionOutcome.FAILED:
            failed_indices.discard(result.ac_index)
            if level_failed > 0:
                level_failed -= 1
        ac_statuses[result.ac_index] = "completed"
        completed_count += 1
        level_success += 1
        reconciled.append(replacement)

    return completed_count, level_success, level_failed, reconciled
