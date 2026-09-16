"""A nested runtime shell wrapper is the same command, and must prove it.

Observed on codex (bench ``orch_89ece88c9d11``, 2026-09-08): the leaf recorded
``/bin/zsh -lc "/bin/zsh -lc '<cmd>'"`` and cited ``<cmd>``. One peeled layer
still left a wrapper, so the exact command the leaf ran was rejected as an
unsupported claim.
"""

from __future__ import annotations

import shlex

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.shell_parsing import (
    _normalized_command_claim_aliases,
    _test_command_invocation,
)
from ouroboros.orchestrator.evidence.test_detection import (
    _functional_command_supports_test_claim,
)

CHECK = "python3 habit_tracker.py unknown-command; test $? -eq 2 && echo EXIT_TWO_OK"
PYTEST = "python3 -m pytest -q test_habit_tracker.py"


def _wrap(command: str, layers: int) -> str:
    wrapped = command
    for _ in range(layers):
        wrapped = "/bin/zsh -lc " + shlex.quote(wrapped)
    return wrapped


def test_nested_wrapper_aliases_reach_the_inner_command() -> None:
    for layers in (1, 2, 3):
        assert set(_normalized_command_claim_aliases(CHECK)) & set(
            _normalized_command_claim_aliases(_wrap(CHECK, layers))
        )


def test_nested_wrapper_does_not_alias_a_different_command() -> None:
    other = "python3 habit_tracker.py list"
    assert not set(_normalized_command_claim_aliases(other)) & set(
        _normalized_command_claim_aliases(_wrap(CHECK, 2))
    )


def test_nested_wrapper_exposes_the_test_invocation() -> None:
    assert _test_command_invocation(_wrap(PYTEST, 1)) is not None
    assert _test_command_invocation(_wrap(PYTEST, 2)) is not None
    assert _test_command_invocation(_wrap("rg --files", 2)) is None


def test_double_wrapped_functional_check_supports_claim(tmp_path) -> None:
    (tmp_path / "habit_tracker.py").write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
    wrapped = _wrap(CHECK, 2)
    start = AgentMessage(
        type="assistant",
        content=f"Calling tool: Bash: {wrapped}",
        tool_name="Bash",
        data={"tool_input": {"command": wrapped}, "tool_call_id": "item_2"},
    )
    result = AgentMessage(
        type="tool_result",
        content="EXIT_TWO_OK",
        data={
            "tool_call_id": "item_2",
            "exit_code": 0,
            "tool_result": {
                "is_error": False,
                "text_content": "EXIT_TWO_OK",
                "meta": {"tool_call_id": "item_2", "exit_status": 0},
            },
        },
    )
    assert (
        _functional_command_supports_test_claim(
            value=CHECK, messages=(start, result), task_cwd=str(tmp_path)
        )
        is True
    )
    failed = AgentMessage(
        type="tool_result",
        content="",
        data={"tool_call_id": "item_2", "exit_code": 1, "tool_result": {"is_error": True}},
    )
    assert (
        _functional_command_supports_test_claim(
            value=CHECK, messages=(start, failed), task_cwd=str(tmp_path)
        )
        is False
    )
