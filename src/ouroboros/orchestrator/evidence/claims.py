"""Runtime transcript claim-matching helpers."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import sys

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.common import _flatten_evidence_values
from ouroboros.orchestrator.evidence.harness_observation import observation_from_message
from ouroboros.orchestrator.evidence.shell_parsing import (
    _has_trailing_output_filter_pipeline,
    _normalized_command_claim_aliases,
    _shell_command_body,
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
    runtime_cwd: str | None = None,
) -> bool:
    """Return True when a claimed workspace path matches a runtime path value."""
    try:
        claim_path = Path(claim.strip())
    except (OSError, RuntimeError, ValueError):
        return False
    if not claim_path or claim_path.is_absolute() or ".." in claim_path.parts:
        return False

    if not runtime_path.strip():
        return False

    try:
        runtime_candidate = Path(runtime_path)
    except (OSError, RuntimeError, ValueError):
        return False
    if task_cwd is not None:
        try:
            base = Path(task_cwd).resolve()
            runtime_base = Path(runtime_cwd).resolve() if runtime_cwd is not None else base
            runtime_base.relative_to(base)
            claimed_candidate = base / claim_path
            runtime_candidate_absolute = (
                runtime_candidate
                if runtime_candidate.is_absolute()
                else runtime_base / runtime_candidate
            )
            if _path_has_resolution_error(claimed_candidate) or _path_has_resolution_error(
                runtime_candidate_absolute
            ):
                return False
            claimed_absolute = claimed_candidate.resolve()
            runtime_absolute = runtime_candidate_absolute.resolve()
            claimed_absolute.relative_to(base)
            runtime_absolute.relative_to(base)
        except (OSError, RuntimeError, ValueError):
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

    try:
        base = Path(task_cwd).resolve()
        candidate = Path(raw_value)
        if not candidate.is_absolute() and ".." in candidate.parts:
            return None
        absolute_candidate = candidate if candidate.is_absolute() else base / candidate
        if _path_has_resolution_error(absolute_candidate):
            return None
        resolved = absolute_candidate.resolve()
        relative = resolved.relative_to(base)
    except (OSError, RuntimeError, ValueError):
        return None

    if not relative.parts or ".." in relative.parts:
        return None
    return relative.as_posix()


def _path_has_resolution_error(path: Path) -> bool:
    try:
        path.resolve(strict=True)
    except FileNotFoundError:
        parent = path.parent
        return parent != path and _path_has_resolution_error(parent)
    except (OSError, RuntimeError, ValueError):
        return True
    return False


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
        try:
            base = Path(task_cwd).resolve()
            absolute_candidate = base / candidate
            if _path_has_resolution_error(absolute_candidate):
                return False
            resolved = absolute_candidate.resolve()
        except (OSError, RuntimeError, ValueError):
            return False
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
                    allow_observation=False,
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
    allow_observation: bool = True,
) -> bool:
    """Return True when one message plausibly reports touching a file reference."""
    normalized_reference = reference.strip().lower()
    if not normalized_reference:
        return False
    observation = observation_from_message(message)
    if observation is not None:
        # The harness saw the workspace change during this leaf's window; that
        # is support the leaf could not have narrated into existence. The
        # observation only ever answers for the full workspace-relative claim:
        # the basename fallback passes ``allow_observation=False`` so a changed
        # ``foo.py`` cannot vouch for an unchanged ``nested/foo.py``.
        return allow_observation and observation.supports_file_claim(reference)
    if message.tool_name == "Bash":
        return _bash_message_mutates_file_reference(
            message,
            reference=reference,
            normalized_reference=normalized_reference,
            task_cwd=task_cwd,
            allow_bash_command_text=allow_bash_command_text,
            messages=messages,
            index=index,
        ) and _runtime_message_has_success_evidence(message, messages=messages, index=index)
    if message.tool_name in {"Edit", "Write", "NotebookEdit"}:
        return _runtime_message_has_success_evidence(
            message,
            messages=messages,
            index=index,
        ) and any(
            _file_claim_matches_runtime_path(reference, path, task_cwd=task_cwd)
            for path in _runtime_message_file_path_values(message)
        )
    text = _runtime_message_file_proof_text(message)
    return _text_supports_file_mutation_reference(text, normalized_reference)


def _bash_message_mutates_file_reference(
    message: AgentMessage,
    *,
    reference: str,
    normalized_reference: str,
    task_cwd: str | None,
    allow_bash_command_text: bool,
    messages: tuple[AgentMessage, ...],
    index: int,
) -> bool:
    text = _runtime_message_file_proof_text(message)
    if text and _text_supports_file_mutation_reference(text, normalized_reference):
        return True
    return allow_bash_command_text and _bash_command_mutates_file_reference(
        message,
        reference=reference,
        task_cwd=task_cwd,
        messages=messages,
        index=index,
    )


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


def _bash_command_mutates_file_reference(
    message: AgentMessage,
    *,
    reference: str,
    task_cwd: str | None,
    messages: tuple[AgentMessage, ...],
    index: int,
) -> bool:
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
    effective_cwd = _runtime_message_effective_cwd(message, task_cwd=task_cwd)
    if task_cwd is not None and effective_cwd is None:
        return False
    if not _runtime_message_has_stable_file_identity(
        message,
        reference=reference,
        task_cwd=task_cwd,
        effective_cwd=effective_cwd,
        messages=messages,
        index=index,
    ):
        # Command text and a zero exit status do not bind the receiver inode at
        # execution time. A symlink may have been replaced before verification.
        return False
    for command in _runtime_message_command_values(message):
        if not command.strip():
            continue
        if _has_unquoted_compound_shell_control(command):
            # A successful compound command does not prove that every textual
            # mutation branch executed. Require structured file evidence
            # instead of interpreting control flow from a transcript string.
            continue
        python_command_match = _python_c_command_file_claim_match(
            command,
            task_cwd=effective_cwd,
        )
        if python_command_match is not None:
            if python_command_match:
                return True
            # Python source is never command-text evidence for its own file
            # receiver.  A distinct, literal shell output redirection remains
            # authoritative, however: the execution-span lease above proves
            # that the shell opened and changed that receiver independently of
            # anything the ``-c`` source may have done.
            return _shell_command_targets_file_claim(
                command,
                reference=reference,
                task_cwd=task_cwd,
                effective_cwd=effective_cwd,
                redirections_only=True,
            )
        if _text_needs_shell_expansion(command):
            # Expansion in a payload does not weaken a literal shell output
            # redirection: a successful shell necessarily opened that target.
            # All other expanded forms remain non-authoritative because they
            # can reconstruct the command, mutation primitive, or target.
            if _shell_command_targets_file_claim(
                command,
                reference=reference,
                task_cwd=task_cwd,
                effective_cwd=effective_cwd,
                redirections_only=True,
            ):
                return True
            continue
        if _shell_command_targets_file_claim(
            command,
            reference=reference,
            task_cwd=task_cwd,
            effective_cwd=effective_cwd,
        ):
            return True
    return False


def _runtime_message_has_stable_file_identity(
    message: AgentMessage,
    *,
    reference: str,
    task_cwd: str | None,
    effective_cwd: str | None,
    messages: tuple[AgentMessage, ...],
    index: int,
) -> bool:
    """Verify adapter-recorded execution-time identity against the current file."""
    if task_cwd is None or effective_cwd is None:
        return False
    for evidence_message in _runtime_message_correlated_completions(message, messages, index):
        effects = evidence_message.data.get("filesystem_effects")
        if not isinstance(effects, (list, tuple)):
            continue
        for effect in effects:
            if not isinstance(effect, dict):
                continue
            path = effect.get("path")
            device = effect.get("st_dev")
            inode = effect.get("st_ino")
            mode = effect.get("st_mode")
            if (
                effect.get("capture") != "ouroboros.leaf-dispatch.v1"
                or not isinstance(path, str)
                or isinstance(device, bool)
                or not isinstance(device, int)
                or isinstance(inode, bool)
                or not isinstance(inode, int)
                or isinstance(mode, bool)
                or not isinstance(mode, int)
                or not stat.S_ISREG(mode)
                or not _file_claim_matches_runtime_path(
                    reference,
                    path,
                    task_cwd=task_cwd,
                    runtime_cwd=effective_cwd,
                )
            ):
                continue
            try:
                candidate = Path(path)
                absolute = candidate if candidate.is_absolute() else Path(effective_cwd) / candidate
                current = absolute.lstat()
            except (OSError, RuntimeError, ValueError):
                continue
            if (
                stat.S_ISREG(current.st_mode)
                and current.st_dev == device
                and current.st_ino == inode
                and stat.S_IFMT(current.st_mode) == stat.S_IFMT(mode)
            ):
                return True
    return False


def _runtime_message_correlated_completions(
    message: AgentMessage,
    messages: tuple[AgentMessage, ...],
    index: int,
) -> tuple[AgentMessage, ...]:
    """Return only unambiguous completion messages correlated to a call."""
    if _runtime_message_has_conflicting_tool_call_ids(message):
        return ()
    call_id = _runtime_message_tool_call_id(message)
    if call_id is not None:
        if any(
            _runtime_message_is_tool_completion(candidate)
            and _runtime_message_has_conflicting_tool_call_ids(candidate)
            and call_id in _runtime_message_tool_call_ids(candidate)
            for candidate in messages[index + 1 :]
        ):
            return ()
        completions = tuple(
            candidate
            for candidate in messages[index + 1 :]
            if _runtime_message_is_tool_completion(candidate)
            and not _runtime_message_has_conflicting_tool_call_ids(candidate)
            and _runtime_message_tool_call_id(candidate) == call_id
        )
        return completions if len(completions) == 1 else ()
    for candidate in messages[index + 1 :]:
        if _runtime_message_is_tool_completion(candidate):
            if (
                not _runtime_message_has_conflicting_tool_call_ids(candidate)
                and _runtime_message_tool_call_id(candidate) is None
                and (candidate.tool_name is None or candidate.tool_name == message.tool_name)
            ):
                return (candidate,)
            break
        if candidate.tool_name is not None:
            break
    return ()


def _shell_command_targets_file_claim(
    command: str,
    *,
    reference: str,
    task_cwd: str | None,
    effective_cwd: str | None,
    redirections_only: bool = False,
) -> bool:
    """Return whether shell mutation syntax targets the canonical claim path.

    Relative targets are interpreted from the recorded command cwd, never from
    the task root by assumption. Tokenizing with punctuation awareness also
    distinguishes real shell syntax from mutation-looking quoted payload text.
    """
    if task_cwd is None or effective_cwd is None:
        return False
    return any(
        _file_claim_matches_runtime_path(
            reference,
            target,
            task_cwd=task_cwd,
            runtime_cwd=effective_cwd,
        )
        for target in _shell_command_mutation_targets(
            command,
            redirections_only=redirections_only,
        )
    )


def _shell_command_mutation_targets(
    command: str,
    *,
    redirections_only: bool = False,
) -> tuple[str, ...]:
    """Extract literal targets from the narrow supported shell mutations."""
    candidate = command
    seen: set[str] = set()
    for _ in range(16):
        wrapped_body = _shell_command_body(candidate)
        if wrapped_body is None:
            break
        if wrapped_body == candidate or wrapped_body in seen:
            return ()
        seen.add(candidate)
        candidate = wrapped_body
    else:
        return ()
    candidate = _strip_unquoted_shell_comment(candidate)
    try:
        lexer = shlex.shlex(
            _mask_quoted_output_operators(_mark_adjacent_fd_selectors(candidate)),
            posix=True,
            punctuation_chars=";&|<>",
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except (RuntimeError, ValueError):
        return ()
    mutation_arguments, targets = _shell_tokens_without_redirections(tokens)
    if redirections_only:
        return tuple(dict.fromkeys(targets))

    command_index = 0
    while command_index < len(mutation_arguments) and re.match(
        r"^[A-Za-z_][A-Za-z0-9_]*=", mutation_arguments[command_index]
    ):
        command_index += 1
    if command_index >= len(mutation_arguments):
        return tuple(targets)
    executable = Path(mutation_arguments[command_index]).name.lower()
    mutation_arguments = mutation_arguments[command_index + 1 :]
    candidates: tuple[str, ...] = ()
    if executable == "touch":
        candidates = _mutation_operands_with_options(
            mutation_arguments,
            short_flags=frozenset("acfmh"),
            short_values=frozenset("Adrt"),
            long_flags=frozenset({"--no-create", "--no-dereference"}),
            long_values=frozenset({"--date", "--reference", "--time"}),
        )
    elif executable == "truncate":
        candidates = _mutation_operands_with_options(
            mutation_arguments,
            short_flags=frozenset("co"),
            short_values=frozenset("rs"),
            long_flags=frozenset({"--io-blocks", "--no-create"}),
            long_values=frozenset({"--reference", "--size"}),
        )
    elif executable == "tee":
        candidates = _mutation_operands_with_options(
            mutation_arguments,
            short_flags=frozenset("aip"),
            short_values=frozenset(),
            long_flags=frozenset({"--append", "--ignore-interrupts", "--output-error"}),
            long_values=frozenset(),
            long_value_prefixes=("--output-error=",),
        )
    elif executable == "sed":
        candidates = _sed_in_place_operands(mutation_arguments)
    elif executable == "perl":
        candidates = _perl_in_place_operands(mutation_arguments)
    targets.extend(
        candidate for candidate in candidates if not _text_needs_shell_expansion(candidate)
    )
    return tuple(dict.fromkeys(targets))


def _shell_tokens_without_redirections(tokens: list[str]) -> tuple[list[str], list[str]]:
    """Separate argv from shell redirections without treating fd copies as files."""
    arguments: list[str] = []
    file_targets: list[str] = []
    output_operators = {">", ">>", ">&", "&>", ">|"}
    non_file_operators = {"<", "<<", "<<<", "<&", "<>"}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token not in output_operators | non_file_operators:
            arguments.append(token)
            index += 1
            continue
        if arguments and arguments[-1].startswith(_FD_SELECTOR_MARKER):
            arguments.pop()
        if index + 1 >= len(tokens):
            return [], []
        target = tokens[index + 1]
        if target in output_operators | non_file_operators | {";", "&", "|", "&&", "||"}:
            return [], []
        if token in {">", ">>", "&>", ">|"} and not _text_needs_shell_expansion(target):
            file_targets.append(target)
        # ``>&`` is descriptor duplication for the bounded proof grammar. Bash
        # also accepts filename extensions of this form, but declining them is
        # safer than confusing ``>&2`` with a file receiver.
        index += 2
    if any(argument.startswith(_FD_SELECTOR_MARKER) for argument in arguments):
        return [], []
    return arguments, file_targets


_FD_SELECTOR_MARKER = "\uf001ouroboros-fd:"


def _mark_adjacent_fd_selectors(command: str) -> str:
    """Mark shell IO-number tokens only when lexically adjacent to ``<``/``>``."""
    marked: list[str] = []
    quote: str | None = None
    escaped = False
    at_word_start = True
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            marked.append(char)
            escaped = False
            at_word_start = False
            index += 1
            continue
        if quote == "'":
            marked.append(char)
            if char == "'":
                quote = None
            index += 1
            continue
        if char == "\\":
            marked.append(char)
            escaped = True
            index += 1
            continue
        if quote == '"':
            marked.append(char)
            if char == '"':
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            marked.append(char)
            quote = char
            at_word_start = False
            index += 1
            continue
        if at_word_start and char.isascii() and char.isdigit():
            end = index + 1
            while end < len(command) and command[end].isascii() and command[end].isdigit():
                end += 1
            digits = command[index:end]
            if end < len(command) and command[end] in {"<", ">"}:
                marked.append(f"{_FD_SELECTOR_MARKER}{digits}")
            else:
                marked.append(digits)
            at_word_start = False
            index = end
            continue
        marked.append(char)
        if char.isspace() or char in ";&|()<>":
            at_word_start = True
        else:
            at_word_start = False
        index += 1
    return "".join(marked)


def _mutation_operands_with_options(
    arguments: list[str],
    *,
    short_flags: frozenset[str],
    short_values: frozenset[str],
    long_flags: frozenset[str],
    long_values: frozenset[str],
    long_value_prefixes: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Return positional receivers for a bounded conventional option grammar."""
    operands: list[str] = []
    options = True
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if options and argument == "--":
            options = False
            index += 1
            continue
        if options and argument.startswith("--"):
            if argument in long_flags or any(
                argument.startswith(prefix) and len(argument) > len(prefix)
                for prefix in long_value_prefixes
            ):
                index += 1
                continue
            name, separator, _value = argument.partition("=")
            if name in long_values:
                if separator:
                    index += 1
                elif index + 1 < len(arguments):
                    index += 2
                else:
                    return ()
                continue
            return ()
        if options and argument.startswith("-") and argument != "-":
            cluster = argument[1:]
            cluster_index = 0
            while cluster_index < len(cluster):
                option = cluster[cluster_index]
                if option in short_flags:
                    cluster_index += 1
                    continue
                if option in short_values:
                    if cluster_index + 1 < len(cluster):
                        cluster_index = len(cluster)
                    elif index + 1 < len(arguments):
                        index += 1
                        cluster_index = len(cluster)
                    else:
                        return ()
                    continue
                return ()
            index += 1
            continue
        operands.append(argument)
        index += 1
    return tuple(operands)


def _sed_in_place_operands(arguments: list[str]) -> tuple[str, ...]:
    """Return sed input files for conservative GNU/BSD in-place forms."""
    positionals: list[str] = []
    in_place = False
    script_from_option = False
    options = True
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if options and argument == "--":
            options = False
            index += 1
            continue
        if options and argument.startswith("--"):
            name, separator, _value = argument.partition("=")
            if name == "--in-place":
                in_place = True
                index += 1
                continue
            if name in {"--expression", "--file"}:
                script_from_option = True
                if separator:
                    index += 1
                elif index + 1 < len(arguments):
                    index += 2
                else:
                    return ()
                continue
            if name in {"--quiet", "--silent", "--regexp-extended", "--separate"}:
                index += 1
                continue
            return ()
        if options and argument.startswith("-") and argument != "-":
            cluster = argument[1:]
            cluster_index = 0
            while cluster_index < len(cluster):
                option = cluster[cluster_index]
                if option in {"n", "E", "r", "s", "u", "z"}:
                    cluster_index += 1
                    continue
                if option == "i":
                    in_place = True
                    if cluster_index + 1 < len(cluster):
                        cluster_index = len(cluster)
                    elif (
                        index + 1 < len(arguments) and arguments[index + 1] == ""
                    ) or not sys.platform.startswith("linux"):
                        if index + 1 >= len(arguments):
                            return ()
                        index += 1
                        cluster_index = len(cluster)
                    else:
                        cluster_index += 1
                    continue
                if option in {"e", "f"}:
                    script_from_option = True
                    if cluster_index + 1 < len(cluster):
                        cluster_index = len(cluster)
                    elif index + 1 < len(arguments):
                        index += 1
                        cluster_index = len(cluster)
                    else:
                        return ()
                    continue
                return ()
            index += 1
            continue
        positionals.append(argument)
        index += 1
    if not in_place:
        return ()
    if script_from_option:
        return tuple(positionals)
    return tuple(positionals[1:]) if len(positionals) >= 2 else ()


def _perl_in_place_operands(arguments: list[str]) -> tuple[str, ...]:
    """Return Perl input files for conservative in-place edit forms."""
    positionals: list[str] = []
    in_place = False
    script_from_option = False
    options = True
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if options and argument == "--":
            options = False
            index += 1
            continue
        if options and argument.startswith("--"):
            return ()
        if options and argument.startswith("-") and argument != "-":
            cluster = argument[1:]
            cluster_index = 0
            while cluster_index < len(cluster):
                option = cluster[cluster_index]
                if option in {"a", "c", "l", "n", "p", "s", "w", "W", "X"}:
                    cluster_index += 1
                    continue
                if option == "i":
                    in_place = True
                    # Any remainder is the backup extension, not more options.
                    cluster_index = len(cluster)
                    continue
                if option in {"e", "E"}:
                    script_from_option = True
                    if cluster_index + 1 < len(cluster):
                        cluster_index = len(cluster)
                    elif index + 1 < len(arguments):
                        index += 1
                        cluster_index = len(cluster)
                    else:
                        return ()
                    continue
                if option in {"F", "I", "m", "M"}:
                    if cluster_index + 1 < len(cluster):
                        cluster_index = len(cluster)
                    elif index + 1 < len(arguments):
                        index += 1
                        cluster_index = len(cluster)
                    else:
                        return ()
                    continue
                if option in {"0", "C", "d", "D", "V", "x"}:
                    # These switches take optional attached values. A
                    # standalone form must not swallow the next switch/operand.
                    cluster_index = len(cluster)
                    continue
                return ()
            index += 1
            continue
        positionals.append(argument)
        index += 1
    if not in_place:
        return ()
    if script_from_option:
        return tuple(positionals)
    return tuple(positionals[1:]) if len(positionals) >= 2 else ()


def _strip_unquoted_shell_comment(command: str) -> str:
    """Drop a real shell comment while preserving literal ``#`` bytes.

    POSIX shells begin a comment only when an unquoted, unescaped ``#`` occurs
    at the start of a word.  Quoted hashes, escaped hashes, and hashes embedded
    in tokens such as ``foo#bar`` remain ordinary argv data.
    """
    quote: str | None = None
    escaped = False
    at_word_start = True
    uncommented: list[str] = []
    for char in command:
        if escaped:
            escaped = False
            if char == "\n":
                # A POSIX backslash-LF continuation is removed before token
                # recognition.  It therefore cannot turn a preceding word
                # boundary into token-internal state before a following '#'.
                uncommented.pop()
                continue
            uncommented.append(char)
            at_word_start = False
            continue
        if quote == "'":
            uncommented.append(char)
            if char == "'":
                quote = None
            continue
        if char == "\\":
            uncommented.append(char)
            escaped = True
            continue
        if quote == '"':
            uncommented.append(char)
            if char == '"':
                quote = None
            continue
        if char in {"'", '"'}:
            uncommented.append(char)
            quote = char
            at_word_start = False
            continue
        if char == "#" and at_word_start:
            return "".join(uncommented)
        uncommented.append(char)
        if char.isspace() or char in ";&|()<>":
            at_word_start = True
        else:
            at_word_start = False
    return "".join(uncommented)


def _mask_quoted_output_operators(command: str) -> str:
    """Mask quoted or escaped ``>`` bytes before punctuation tokenization."""
    quote: str | None = None
    escaped = False
    masked: list[str] = []
    for char in command:
        if escaped:
            masked.append("\uf000" if char == ">" else char)
            escaped = False
            continue
        if quote != "'" and char == "\\":
            masked.append(char)
            escaped = True
            continue
        if char in {"'", '"'}:
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
            masked.append(char)
            continue
        masked.append("\uf000" if char == ">" and quote is not None else char)
    return "".join(masked)


def _has_unquoted_compound_shell_control(command: str) -> bool:
    """Return True when command text contains execution control operators."""
    wrapped_body = _shell_command_body(command)
    if wrapped_body is not None:
        return _has_unquoted_compound_shell_control(wrapped_body)
    command = _strip_unquoted_shell_comment(command)
    quote: str | None = None
    escaped = False
    for index, char in enumerate(command):
        if escaped:
            escaped = False
            continue
        if quote == "'":
            if char == "'":
                quote = None
            continue
        if char == "\\":
            escaped = True
            continue
        if quote == '"':
            if char == '"':
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "|" and index > 0 and command[index - 1] == ">":
            # ``>|`` is the shell's clobber redirection operator, not a pipe.
            continue
        if char in {"|", ";", "\n", "\r"}:
            return True
        if char == "&":
            previous = command[index - 1] if index > 0 else ""
            following = command[index + 1] if index + 1 < len(command) else ""
            if previous not in {">", "<"} and following != ">":
                return True
    return False


def _runtime_message_effective_cwd(message: AgentMessage, *, task_cwd: str | None) -> str | None:
    """Return the command cwd only when it is contained by the task workspace."""
    if task_cwd is None:
        return None
    tool_input = message.data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None if _path_has_symlink_component(Path(task_cwd)) else task_cwd
    cwd = tool_input.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        return None if _path_has_symlink_component(Path(task_cwd)) else task_cwd
    try:
        workspace = Path(task_cwd).resolve()
        candidate = Path(cwd)
        unresolved_effective = candidate if candidate.is_absolute() else Path(task_cwd) / candidate
        if _path_has_symlink_component(unresolved_effective):
            return None
        effective = unresolved_effective.resolve()
        effective.relative_to(workspace)
    except (OSError, RuntimeError, ValueError):
        return None
    return str(effective)


def _path_has_symlink_component(path: Path) -> bool:
    """Return True when any existing path component is a symlink."""
    current = Path(path.anchor) if path.is_absolute() else Path()
    for part in path.parts if not path.is_absolute() else path.parts[1:]:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _python_c_command_file_claim_match(
    command: str,
    *,
    task_cwd: str | None,
) -> bool | None:
    """Classify Python ``-c`` command text as rejected or unrelated.

    ``None`` means the command is not a recognized Python ``-c`` form and other
    shell mutation evidence may still be considered. ``False`` is authoritative:
    Python command text cannot safely prove a historical filesystem mutation
    without execution-bound file evidence.
    """
    if task_cwd is None:
        return None
    candidate = command
    seen: set[str] = set()
    for _ in range(16):
        match = _direct_python_c_command_file_claim_match(candidate, task_cwd=task_cwd)
        if match is not None:
            return match
        body = _shell_command_body(candidate)
        if body is None or body == candidate or body in seen:
            return None
        seen.add(candidate)
        candidate = body
    # Excessive supported-wrapper nesting is not trustworthy command-text
    # proof. Do not let inert inner argv reach generic shell heuristics.
    return False


def _direct_python_c_command_file_claim_match(
    command: str,
    *,
    task_cwd: str,
) -> bool | None:
    """Classify one unwrapped command as Python ``-c`` or unrelated."""
    # Command text can never prove the historical identity of a pathlib
    # receiver. Detect the Python source itself before shell tokenization so
    # executable quoting/concatenation (for example ``py"thon"``) cannot move
    # the same payload into generic shell mutation heuristics.
    if _raw_command_mentions_python_c_pathlib(command):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        if _raw_command_mentions_python_c_pathlib(command):
            return False
        return None
    python_index = _python_c_argv_index(argv, task_cwd=task_cwd)
    if python_index is False:
        return False
    if python_index is None:
        if _raw_command_mentions_python_c_pathlib(command) or _argv_mentions_python_c_pathlib(argv):
            return False
        return None
    if "-c" not in argv[python_index + 1 :]:
        return None
    source_index = argv.index("-c", python_index + 1) + 1
    if len(argv) <= source_index:
        return False
    # Once a Python ``-c`` invocation is recognized, its remaining argv is
    # Python data, not additional shell syntax. It must therefore be
    # authoritative before generic ``touch``/redirection heuristics even when
    # the source reconstructs pathlib without lexical ``pathlib``/``Path``
    # signatures. Command text alone never proves its historical file target.
    return False


def _normalize_absolute_path(path: Path) -> Path:
    """Collapse lexical ``.`` segments without resolving symlinks."""
    return Path(PurePosixPath(path).as_posix())


def _raw_command_mentions_python_c_pathlib(command: str) -> bool:
    return re.search(r"(?:^|\s)-c(?:\s|$)", command) is not None and _source_mentions_pathlib(
        command
    )


def _source_mentions_pathlib(source: str) -> bool:
    return re.search(r"\bpathlib\b|\bPath\s*\(", source, re.IGNORECASE) is not None


def _text_needs_shell_expansion(value: str) -> bool:
    quote: str | None = None
    escaped = False
    for char in value:
        if escaped:
            escaped = False
            continue
        if quote == "'":
            if char == "'":
                quote = None
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            quote = None if quote == '"' else '"'
            continue
        if char == "'" and quote is None:
            quote = "'"
            continue
        if char in {"$", "`"}:
            return True
    return False


def _argv_mentions_python_c_pathlib(argv: list[str]) -> bool:
    return any(_raw_command_mentions_python_c_pathlib(value) for value in argv)


def _python_c_argv_index(argv: list[str], *, task_cwd: str) -> int | None | bool:
    index = 0
    while index < len(argv) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[index]):
        index += 1
    if index >= len(argv):
        return None
    if Path(argv[index]).name.lower() == "env":
        if not _trusted_env_executable(argv[index]):
            return None
        index = _env_command_index(argv, index + 1)
        if index is None:
            return None
    executable = Path(argv[index]).name.lower()
    if (
        executable not in {"python", "python3"}
        and re.fullmatch(r"python3\.\d+", executable) is None
    ):
        return None
    if not _python_invocation_uses_c(argv, python_index=index):
        return None
    return index if _trusted_python_executable(argv[index], task_cwd=task_cwd) else False


def _trusted_env_executable(value: str) -> bool:
    """Accept only the immutable system ``env`` executable wrapper."""
    try:
        candidate = Path(value)
        if not candidate.is_absolute() or candidate.is_symlink():
            return False
        resolved = candidate.resolve()
        return any(
            system_env.exists() and resolved == system_env.resolve()
            for system_env in (Path("/usr/bin/env"), Path("/bin/env"))
        )
    except (OSError, RuntimeError, ValueError):
        return False


def _env_command_index(argv: list[str], index: int) -> int | None:
    """Return the wrapped executable position for a narrow system-env prefix."""
    while index < len(argv):
        value = argv[index]
        if value == "--":
            index += 1
            break
        if value in {"-i", "--ignore-environment"} or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", value):
            index += 1
            continue
        if value in {"-u", "--unset"}:
            index += 2
            continue
        if value.startswith("--unset="):
            index += 1
            continue
        if value.startswith("-"):
            return None
        break
    return index if index < len(argv) else None


def _python_invocation_uses_c(argv: list[str], *, python_index: int) -> bool:
    """Return whether ``-c`` occurs in Python's option prefix, before a script."""
    index = python_index + 1
    while index < len(argv):
        value = argv[index]
        if value == "-c":
            return True
        if value in {"--", "-m"} or not value.startswith("-"):
            return False
        if value in {"-W", "-X"}:
            index += 2
            continue
        index += 1
    return False


def _trusted_python_executable(value: str, *, task_cwd: str) -> bool:
    try:
        path = Path(value)
        if not path.is_absolute():
            return False
        candidate = path if path.is_absolute() else Path(task_cwd) / path
        if candidate.is_symlink():
            return False
        verifier_executable = Path(sys.executable).resolve()
        if _normalize_absolute_path(candidate) != _normalize_absolute_path(verifier_executable):
            return False
        return candidate.resolve() == verifier_executable
    except (OSError, RuntimeError, ValueError):
        return False


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


def _runtime_message_tool_call_ids(message: AgentMessage) -> frozenset[str]:
    """Return every normalized correlation-id alias carried by a message."""
    values: set[str] = set()
    containers: list[dict[str, object]] = [message.data]
    message_meta = message.data.get("meta")
    if isinstance(message_meta, dict):
        containers.append(message_meta)
    tool_result = message.data.get("tool_result")
    if isinstance(tool_result, dict):
        containers.append(tool_result)
        meta = tool_result.get("meta")
        if isinstance(meta, dict):
            containers.append(meta)
    for container in containers:
        for key in ("tool_call_id", "tool_use_id", "call_id"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                values.add(value.strip())
    return frozenset(values)


def _runtime_message_has_conflicting_tool_call_ids(message: AgentMessage) -> bool:
    """Return whether aliases disagree, making correlation fail closed."""
    return len(_runtime_message_tool_call_ids(message)) > 1


def _runtime_message_tool_call_id(message: AgentMessage) -> str | None:
    """Return the sole normalized correlation id, or ``None`` if absent/ambiguous."""
    values = _runtime_message_tool_call_ids(message)
    return next(iter(values)) if len(values) == 1 else None


def _runtime_message_is_tool_completion(message: AgentMessage) -> bool:
    """Return whether a message is a normalized tool completion/result."""
    if message.type == "tool_result" or message.data.get("subtype") == "tool_result":
        return True
    if isinstance(message.data.get("tool_result"), dict):
        return True
    runtime_event_type = message.data.get("runtime_event_type")
    return isinstance(runtime_event_type, str) and runtime_event_type.strip().lower().endswith(
        ("tool.completed", "tool.failed", "tool.output")
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
    if _runtime_message_has_conflicting_tool_call_ids(message):
        return False
    call_id = _runtime_message_tool_call_id(message)
    if call_id is not None:
        if any(
            _runtime_message_has_conflicting_tool_call_ids(candidate)
            and call_id in _runtime_message_tool_call_ids(candidate)
            for candidate in messages
        ):
            return False
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
    if _runtime_message_has_conflicting_tool_call_ids(start):
        return False
    start_call_id = _runtime_message_tool_call_id(start)
    if start_call_id is not None:
        if any(
            _runtime_message_has_conflicting_tool_call_ids(candidate)
            and start_call_id in _runtime_message_tool_call_ids(candidate)
            for candidate in messages
        ):
            return False
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
            and not _runtime_message_has_conflicting_tool_call_ids(candidate)
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
        if _runtime_message_has_conflicting_tool_call_ids(candidate):
            return False
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
