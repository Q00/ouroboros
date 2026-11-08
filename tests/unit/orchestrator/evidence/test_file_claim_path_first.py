"""``files_touched``: an edit tool call's path is compared before its success scan.

The success check walks the whole transcript, while the path comparison reads
one message. Checking success first cost one transcript scan per edit call for
every claim. These tests pin that the reordered check returns the same verdict
(and raises the same way) as the success-first order, kept below as a
reference copy, and that it runs the success scan only for edit calls that
name the claimed path.
"""

from __future__ import annotations

from collections.abc import Callable
import itertools
from pathlib import Path
import random
import sys
from typing import Any
from unittest import mock

import pytest

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence import claims
from ouroboros.orchestrator.evidence.harness_observation import observation_from_message

_EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}


# --- reference: the success-first order this change replaced ----------------


def _reference_support_file_claim(
    value: str,
    messages: tuple[AgentMessage, ...],
    *,
    task_cwd: str | None,
) -> bool:
    if task_cwd is not None:
        relative_claim = claims._workspace_relative_file_claim(value, task_cwd=task_cwd)
        if relative_claim is None:
            return False
        candidate = Path(relative_claim)
        try:
            base = Path(task_cwd).resolve()
            absolute_candidate = base / candidate
            if claims._path_has_resolution_error(absolute_candidate):
                return False
            resolved = absolute_candidate.resolve()
        except (OSError, RuntimeError, ValueError):
            return False
        if any(
            _reference_supports_file_reference(
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
                _reference_supports_file_reference(
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

    raw_claim = value.strip()
    if not raw_claim:
        return False
    return any(
        message.tool_name in _EDIT_TOOLS
        and claims._runtime_message_has_success_evidence(
            message,
            messages=messages,
            index=index,
        )
        and any(
            claims._file_claim_matches_runtime_path(raw_claim, path, task_cwd=None)
            for path in claims._runtime_message_file_path_values(message)
        )
        for index, message in enumerate(messages)
    )


def _reference_supports_file_reference(
    reference: str,
    message: AgentMessage,
    *,
    messages: tuple[AgentMessage, ...],
    index: int,
    task_cwd: str | None,
    allow_bash_command_text: bool = True,
    allow_observation: bool = True,
) -> bool:
    normalized_reference = reference.strip().lower()
    if not normalized_reference:
        return False
    observation = observation_from_message(message)
    if observation is not None:
        return allow_observation and observation.supports_file_claim(reference)
    if message.tool_name == "Bash":
        return claims._bash_message_mutates_file_reference(
            message,
            reference=reference,
            normalized_reference=normalized_reference,
            task_cwd=task_cwd,
            allow_bash_command_text=allow_bash_command_text,
            messages=messages,
            index=index,
        ) and claims._runtime_message_has_success_evidence(message, messages=messages, index=index)
    if message.tool_name in _EDIT_TOOLS:
        return claims._runtime_message_has_success_evidence(
            message,
            messages=messages,
            index=index,
        ) and any(
            claims._file_claim_matches_runtime_path(reference, path, task_cwd=task_cwd)
            for path in claims._runtime_message_file_path_values(message)
        )
    text = claims._runtime_message_file_proof_text(message)
    return claims._text_supports_file_mutation_reference(text, normalized_reference)


# --- transcript builders ------------------------------------------------------


def _start(tool: str, path: str, ids: dict[str, Any], **extra: Any) -> AgentMessage:
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return AgentMessage(
        type="assistant",
        content="",
        tool_name=tool,
        data={"tool_input": {key: path}, **ids, **extra},
    )


def _result(ids: dict[str, Any], *, is_error: bool, tool: str | None = None) -> AgentMessage:
    return AgentMessage(
        type="tool_result",
        content="ok",
        tool_name=tool,
        data={"subtype": "tool_result", "tool_result": {"is_error": is_error}, **ids},
    )


_ID_SHAPES = (
    "absent",
    "present",
    "conflicting_start",
    "conflicting_result",
    "duplicate_start",
    "foreign_conflict",
)
_RESULT_SHAPES = ("success", "failure", "missing", "inline_exit_zero", "named_other_tool")
_PATH_SHAPES = ("match", "mismatch", "absolute_match", "basename_only")


def _case_transcript(
    root: Path,
    *,
    tool: str,
    path_shape: str,
    result_shape: str,
    id_shape: str,
    interleaved: bool,
) -> tuple[AgentMessage, ...]:
    path = {
        "match": "src/target.py",
        "mismatch": "src/other.py",
        "absolute_match": str(root / "src" / "target.py"),
        "basename_only": "target.py",
    }[path_shape]
    start_ids: dict[str, Any] = {} if id_shape == "absent" else {"tool_call_id": "c1"}
    result_ids: dict[str, Any] = dict(start_ids)
    if id_shape == "conflicting_start":
        start_ids = {"tool_call_id": "c1", "meta": {"tool_use_id": "c2"}}
    if id_shape == "conflicting_result":
        result_ids = {"tool_call_id": "c1", "meta": {"tool_use_id": "c2"}}

    extra = {"exit_code": 0} if result_shape == "inline_exit_zero" else {}
    messages: list[AgentMessage] = [
        _start("Edit", "src/noise.py", {"tool_call_id": "n0"}),
        _result({"tool_call_id": "n0"}, is_error=False),
        _start(tool, path, start_ids, **extra),
    ]
    if interleaved:
        messages.append(_start("Write", "src/interleaved.py", {}))
    if result_shape == "success":
        messages.append(_result(result_ids, is_error=False))
    elif result_shape == "failure":
        messages.append(_result(result_ids, is_error=True))
    elif result_shape == "named_other_tool":
        messages.append(_result(result_ids, is_error=False, tool="Read"))
    if id_shape == "duplicate_start":
        messages.append(_start("Write", "src/elsewhere.py", {"tool_call_id": "c1"}))
    if id_shape == "foreign_conflict":
        messages.append(
            AgentMessage(
                type="assistant",
                content="",
                data={"tool_call_id": "c1", "call_id": "c9"},
            )
        )
    messages.append(AgentMessage(type="assistant", content="done"))
    return tuple(messages)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path.resolve()
    (root / "src").mkdir()
    for name in ("target.py", "other.py", "noise.py"):
        (root / "src" / name).write_text("x\n", encoding="utf-8")
    return root


def test_verdicts_match_success_first_order_on_case_table(workspace: Path) -> None:
    """Path match/mismatch x success/failure x call-id shapes x workspace.

    The tool only selects the branch and the path key, so it rotates across
    the full product of the other dimensions instead of multiplying it.
    """
    verdicts: set[bool] = set()
    tools = itertools.cycle(sorted(_EDIT_TOOLS))
    for path_shape, result_shape, id_shape, interleaved in itertools.product(
        _PATH_SHAPES, _RESULT_SHAPES, _ID_SHAPES, (False, True)
    ):
        tool = next(tools)
        messages = _case_transcript(
            workspace,
            tool=tool,
            path_shape=path_shape,
            result_shape=result_shape,
            id_shape=id_shape,
            interleaved=interleaved,
        )
        case = (tool, path_shape, result_shape, id_shape, interleaved)
        for task_cwd in (str(workspace), None):
            for claim in ("src/target.py", str(workspace / "src" / "target.py"), "target.py"):
                expected = _reference_support_file_claim(claim, messages, task_cwd=task_cwd)
                actual = claims._runtime_messages_support_file_claim(
                    claim, messages, task_cwd=task_cwd
                )
                assert actual == expected, (case, task_cwd, claim)
                verdicts.add(actual)
            # Per message, for each tier's arguments, on the changed branch.
            for index, message in enumerate(messages):
                if message.tool_name not in _EDIT_TOOLS:
                    continue
                for reference, allow in (("src/target.py", True), ("target.py", False)):
                    kwargs = {
                        "messages": messages,
                        "index": index,
                        "task_cwd": task_cwd,
                        "allow_bash_command_text": allow,
                        "allow_observation": allow,
                    }
                    assert claims._runtime_message_supports_file_reference(
                        reference, message, **kwargs
                    ) == _reference_supports_file_reference(reference, message, **kwargs), (
                        case,
                        task_cwd,
                        index,
                        reference,
                    )
    # The table must exercise both verdicts, or the comparison proves nothing.
    assert verdicts == {True, False}


def _random_transcript(rng: random.Random, root: Path) -> tuple[AgentMessage, ...]:
    paths = ("src/target.py", "src/other.py", str(root / "src" / "target.py"), "target.py")
    ids = ("a", "b", "c")
    messages: list[AgentMessage] = []
    for _ in range(rng.randint(1, 12)):
        kind = rng.random()
        id_choice = rng.random()
        if id_choice < 0.3:
            call_ids: dict[str, Any] = {}
        elif id_choice < 0.85:
            call_ids = {"tool_call_id": rng.choice(ids)}
        else:
            call_ids = {"tool_call_id": rng.choice(ids), "meta": {"call_id": rng.choice(ids)}}
        if kind < 0.45:
            extra = {"exit_code": 0} if rng.random() < 0.15 else {}
            messages.append(
                _start(rng.choice(sorted(_EDIT_TOOLS)), rng.choice(paths), call_ids, **extra)
            )
        elif kind < 0.85:
            tool = rng.choice((None, None, "Edit", "Write", "Read"))
            messages.append(_result(call_ids, is_error=rng.random() < 0.3, tool=tool))
        elif kind < 0.92:
            messages.append(
                AgentMessage(
                    type="assistant",
                    content="",
                    tool_name="Bash",
                    data={"tool_input": {"command": "ls"}, **call_ids},
                )
            )
        else:
            messages.append(AgentMessage(type="assistant", content="thinking"))
    return tuple(messages)


def test_verdicts_match_success_first_order_on_random_transcripts(workspace: Path) -> None:
    rng = random.Random(20261109)
    verdicts: set[bool] = set()
    for _ in range(300):
        messages = _random_transcript(rng, workspace)
        for task_cwd in (str(workspace), None):
            for claim in ("src/target.py", "target.py", "src/other.py"):
                expected = _reference_support_file_claim(claim, messages, task_cwd=task_cwd)
                assert (
                    claims._runtime_messages_support_file_claim(claim, messages, task_cwd=task_cwd)
                    == expected
                ), (messages, task_cwd, claim)
                verdicts.add(expected)
    assert verdicts == {True, False}


def _counting_success_evidence() -> tuple[dict[str, int], Callable[..., bool]]:
    real = claims._runtime_message_has_success_evidence
    calls = {"count": 0}

    def counting(*args: Any, **kwargs: Any) -> bool:
        calls["count"] += 1
        return real(*args, **kwargs)

    return calls, counting


def _unrelated_edits_then(root: Path | None, count: int, target: str) -> tuple[AgentMessage, ...]:
    def path(relative: str) -> str:
        return str(root / relative) if root is not None else relative

    messages: list[AgentMessage] = []
    for i in range(count):
        messages.append(_start("Edit", path(f"src/unrelated_{i}.py"), {"tool_call_id": f"u{i}"}))
        messages.append(_result({"tool_call_id": f"u{i}"}, is_error=False))
    messages.append(_start("Write", path(target), {"tool_call_id": "target"}))
    messages.append(_result({"tool_call_id": "target"}, is_error=False))
    return tuple(messages)


@pytest.mark.parametrize("unrelated", [1, 25, 80])
def test_success_scan_runs_only_for_edits_naming_the_claim(workspace: Path, unrelated: int) -> None:
    (workspace / "src" / "untouched.py").write_text("x\n", encoding="utf-8")
    messages = _unrelated_edits_then(workspace, unrelated, "src/target.py")
    calls, counting = _counting_success_evidence()
    with mock.patch.object(claims, "_runtime_message_has_success_evidence", counting):
        assert claims._runtime_messages_support_file_claim(
            "src/target.py", messages, task_cwd=str(workspace)
        )
        # Supported in the exact tier: one scan, for the one matching call.
        assert calls["count"] == 1
        calls["count"] = 0
        # Unsupported existing file: both tiers run, and no call names it.
        assert not claims._runtime_messages_support_file_claim(
            "src/untouched.py", messages, task_cwd=str(workspace)
        )
        assert calls["count"] == 0
        # The success-first order scanned once per edit call, per tier.
        calls["count"] = 0
        assert _reference_support_file_claim("src/target.py", messages, task_cwd=str(workspace))
        assert calls["count"] == unrelated + 1
        calls["count"] = 0
        assert not _reference_support_file_claim(
            "src/untouched.py", messages, task_cwd=str(workspace)
        )
        assert calls["count"] == 2 * (unrelated + 1)


@pytest.mark.parametrize("unrelated", [1, 25, 80])
def test_success_scan_without_workspace_runs_only_for_matching_edit(unrelated: int) -> None:
    messages = _unrelated_edits_then(None, unrelated, "src/target.py")
    calls, counting = _counting_success_evidence()
    with mock.patch.object(claims, "_runtime_message_has_success_evidence", counting):
        assert claims._runtime_messages_support_file_claim("src/target.py", messages, task_cwd=None)
        assert calls["count"] == 1
        calls["count"] = 0
        assert not claims._runtime_messages_support_file_claim(
            "src/missing.py", messages, task_cwd=None
        )
        assert calls["count"] == 0
        calls["count"] = 0
        assert _reference_support_file_claim("src/target.py", messages, task_cwd=None)
        assert calls["count"] == unrelated + 1


def _deeply_nested_edit(root: Path | None, *, succeeded: bool) -> tuple[AgentMessage, ...]:
    nested: Any = "leaf"
    for _ in range(sys.getrecursionlimit() + 100):
        nested = {"next": nested}
    path = str(root / "src" / "target.py") if root is not None else "src/target.py"
    start = AgentMessage(
        type="assistant",
        content="",
        tool_name="Edit",
        data={"tool_input": {"file_path": path, "nested": nested}, "tool_call_id": "deep"},
    )
    return (start, _result({"tool_call_id": "deep"}, is_error=not succeeded))


@pytest.mark.parametrize("has_workspace", [True, False])
def test_input_nested_past_recursion_limit_raises_as_before(
    workspace: Path, has_workspace: bool
) -> None:
    """The path walk recurses; success-first raised there only after success."""
    root = workspace if has_workspace else None
    task_cwd = str(workspace) if has_workspace else None

    failed = _deeply_nested_edit(root, succeeded=False)
    assert not _reference_support_file_claim("src/target.py", failed, task_cwd=task_cwd)
    assert not claims._runtime_messages_support_file_claim(
        "src/target.py", failed, task_cwd=task_cwd
    )

    succeeded = _deeply_nested_edit(root, succeeded=True)
    with pytest.raises(RecursionError):
        _reference_support_file_claim("src/target.py", succeeded, task_cwd=task_cwd)
    with pytest.raises(RecursionError):
        claims._runtime_messages_support_file_claim("src/target.py", succeeded, task_cwd=task_cwd)
