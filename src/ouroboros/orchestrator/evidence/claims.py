"""Runtime transcript claim-matching helpers."""

from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath
import re
import shlex
import sys

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
    if message.tool_name == "Bash":
        return _bash_message_mutates_file_reference(
            message,
            reference=reference,
            normalized_reference=normalized_reference,
            task_cwd=task_cwd,
            allow_bash_command_text=allow_bash_command_text,
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
) -> bool:
    text = _runtime_message_file_proof_text(message)
    if text and _text_supports_file_mutation_reference(text, normalized_reference):
        return True
    return allow_bash_command_text and _bash_command_mutates_file_reference(
        message,
        reference=reference,
        normalized_reference=normalized_reference,
        task_cwd=task_cwd,
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
    normalized_reference: str,
    task_cwd: str | None,
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
    quoted_reference = rf"['\"]?{re.escape(normalized_reference)}['\"]?"
    for command in _runtime_message_command_values(message):
        normalized_command = command.strip().lower()
        if not normalized_command:
            continue
        python_pathlib_match = _python_c_pathlib_write_reference_match(
            command,
            reference=reference,
            task_cwd=effective_cwd,
            claim_cwd=task_cwd,
        )
        if python_pathlib_match is not None:
            return python_pathlib_match
        if not _file_reference_pattern(normalized_reference).search(normalized_command):
            continue
        if re.search(rf"(^|[\s;&|])(?:\d?>|&>|>>|\d>>)\s*{quoted_reference}", normalized_command):
            return True
        if re.search(
            rf"(^|[\s;&|])(touch|truncate|tee)\b[^;&|]*\s{quoted_reference}(?=$|[\s;&|])",
            normalized_command,
        ) or re.search(
            rf"(^|[\s;&|])(sed|perl)\b[^;&|]*\s-[^\s;&|]*i[^;&|]*\s"
            rf"{quoted_reference}(?=$|[\s;&|])",
            normalized_command,
        ):
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


def _python_c_pathlib_write_targets_reference(
    command: str,
    *,
    reference: str,
    task_cwd: str | None,
    claim_cwd: str | None = None,
) -> bool:
    """Return True when a direct Python ``-c`` pathlib write targets the claim."""
    return (
        _python_c_pathlib_write_reference_match(
            command,
            reference=reference,
            task_cwd=task_cwd,
            claim_cwd=claim_cwd,
        )
        is True
    )


def _python_c_pathlib_write_reference_match(
    command: str,
    *,
    reference: str,
    task_cwd: str | None,
    claim_cwd: str | None = None,
) -> bool | None:
    """Classify a Python ``-c`` pathlib write as matching, rejected, or unrelated.

    ``None`` means the command is not a pathlib-write Python ``-c`` form and
    other shell mutation evidence may still be considered. ``False`` is
    authoritative: a recognized pathlib write did not safely prove the claim.
    """
    if task_cwd is None:
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        if _raw_command_mentions_python_c_pathlib_write(command):
            return False
        return None
    python_index = _python_c_argv_index(argv, task_cwd=task_cwd)
    if python_index is False:
        return False
    if python_index is None:
        if _raw_command_mentions_python_c_pathlib_write(
            command
        ) or _argv_mentions_python_c_pathlib_write(argv):
            return False
        return None
    if len(argv) <= python_index + 2 or "-c" not in argv[python_index + 1 :]:
        return None
    source_index = argv.index("-c", python_index + 1) + 1
    if len(argv) <= source_index:
        return None
    source = argv[source_index]
    if not _source_mentions_pathlib_write(source):
        return None
    if (
        python_index != 0
        or source_index != 4
        or len(argv) != 5
        or argv[1] != "-I"
        or argv[2] != "-S"
        or _source_needs_shell_expansion(source)
    ):
        return False
    try:
        tree = ast.parse(source)
    except (MemoryError, RecursionError, SyntaxError, ValueError):
        return False
    targets = _pathlib_write_targets(tree)
    if not targets:
        return False
    return any(
        _pathlib_static_target_matches_claim(
            reference,
            target,
            task_cwd=claim_cwd or task_cwd,
            runtime_cwd=task_cwd,
        )
        for target in targets
    )


def _pathlib_static_target_matches_claim(
    reference: str,
    target: str,
    *,
    task_cwd: str,
    runtime_cwd: str,
) -> bool:
    """Match static pathlib targets lexically, without final-state symlink resolution."""
    try:
        claim_path = Path(reference.strip())
        target_path = Path(target)
    except (OSError, RuntimeError, ValueError):
        return False
    if not reference.strip() or claim_path.is_absolute() or ".." in claim_path.parts:
        return False
    if any(part == ".." for part in target_path.parts):
        return False
    try:
        claim_base = Path(task_cwd).absolute()
        runtime_base = Path(runtime_cwd).absolute()
        claim_absolute = _normalize_absolute_path(claim_base / claim_path)
        target_absolute = _normalize_absolute_path(
            target_path if target_path.is_absolute() else runtime_base / target_path
        )
        target_absolute.relative_to(claim_base)
    except (OSError, RuntimeError, ValueError):
        return False
    return target_absolute == claim_absolute


def _normalize_absolute_path(path: Path) -> Path:
    """Collapse lexical ``.`` segments without resolving symlinks."""
    return Path(PurePosixPath(path).as_posix())


def _raw_command_mentions_python_c_pathlib_write(command: str) -> bool:
    return re.search(
        r"\bpython(?:3(?:\.\d+)?)?\s+-c\b", command, re.IGNORECASE
    ) is not None and _source_mentions_pathlib_write(command)


def _source_mentions_pathlib_write(source: str) -> bool:
    return re.search(r"\bwrite_(?:text|bytes)\b", source) is not None


def _source_needs_shell_expansion(source: str) -> bool:
    return "$" in source or "`" in source


def _pathlib_write_targets(tree: ast.AST) -> tuple[str, ...]:
    """Extract static-proof pathlib writes from a Python ``-c`` module.

    This intentionally accepts only inline top-level ``Path(...).write_*``
    expressions. Aliases, assignments, variables, and guarded/nested blocks are
    real Python patterns, but static transcript text cannot prove they executed
    or still bind to ``pathlib.Path``. Those cases need runtime file-change
    evidence instead.
    """
    targets: list[str] = []
    path_imported = False
    for node in getattr(tree, "body", ()):
        # Static transcript evidence can only prove direct statements. Nested or
        # guarded writes need runtime file-change evidence instead.
        if _imports_pathlib_path(node):
            path_imported = True
            continue
        if _binds_name(node, "Path"):
            return ()
        if not path_imported or not isinstance(node, ast.Expr):
            return ()
        call = node.value
        if not isinstance(call, ast.Call):
            return ()
        if not isinstance(call.func, ast.Attribute):
            return ()
        if call.func.attr not in {"write_text", "write_bytes"}:
            return ()
        if _call_has_side_effecting_arguments(call):
            return ()
        target = _literal_pathlib_receiver(call.func.value)
        if target is None:
            return ()
        targets.append(target)
    return tuple(targets)


def _call_has_side_effecting_arguments(call: ast.Call) -> bool:
    try:
        return any(
            isinstance(
                child, (ast.Call, ast.NamedExpr, ast.Lambda, ast.Await, ast.Yield, ast.YieldFrom)
            )
            for argument in (*call.args, *(keyword.value for keyword in call.keywords))
            for child in ast.walk(argument)
        )
    except RecursionError:
        return True


def _imports_pathlib_path(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.ImportFrom)
        and node.module == "pathlib"
        and any(alias.name == "Path" and alias.asname is None for alias in node.names)
    )


def _binds_name(node: ast.AST, name: str) -> bool:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name == name
    if isinstance(node, ast.ImportFrom):
        for alias in node.names:
            bound_name = alias.asname or alias.name
            if bound_name == name and not (node.module == "pathlib" and alias.name == "Path"):
                return True
        return False
    if isinstance(node, ast.Import):
        for alias in node.names:
            bound_name = alias.asname or alias.name.split(".", maxsplit=1)[0]
            if bound_name == name:
                return True
        return False
    targets: tuple[ast.AST, ...] = ()
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = tuple(getattr(node, "targets", ())) or (node.target,)
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        targets = (node.target,)
    elif isinstance(node, ast.With):
        targets = tuple(item.optional_vars for item in node.items if item.optional_vars is not None)
    try:
        return any(_target_binds_name(target, name) for target in targets)
    except RecursionError:
        return True


def _target_binds_name(target: ast.AST, name: str) -> bool:
    return any(
        isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store) and child.id == name
        for child in ast.walk(target)
    )


def _literal_pathlib_receiver(node: ast.AST) -> str | None:
    try:
        if isinstance(node, ast.Call) and _is_path_constructor(node.func):
            if not node.args or node.keywords:
                return None
            segments = tuple(_literal_path_segment(arg) for arg in node.args)
            if any(segment is None for segment in segments):
                return None
            return str(PurePosixPath(*segments))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left = _literal_pathlib_receiver(node.left)
            right = _literal_path_segment(node.right)
            if left is None or right is None:
                return None
            return str(PurePosixPath(left) / right)
        return None
    except RecursionError:
        return None


def _literal_path_segment(node: ast.AST) -> str | None:
    try:
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip():
            return node.value
        return None
    except RecursionError:
        return None


def _is_path_constructor(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "Path"


def _argv_mentions_python_c_pathlib_write(argv: list[str]) -> bool:
    return any(_raw_command_mentions_python_c_pathlib_write(value) for value in argv)


def _python_c_argv_index(argv: list[str], *, task_cwd: str) -> int | None | bool:
    for index, value in enumerate(argv):
        executable = Path(value).name.lower()
        if executable in {"python", "python3"} or re.fullmatch(r"python3\.\d+", executable):
            if index != 0:
                return False
            return index if _trusted_python_executable(value, task_cwd=task_cwd) else False
        if "=" not in value:
            return None
    return None


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
