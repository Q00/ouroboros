"""Argv safety and workspace confinement for commands the harness runs itself.

The harness replays commands the transcript shows the leaf running (see
``evidence/command_replay.py``). Leaf-authored text must never be
shell-interpreted there: only a command that tokenizes cleanly and carries no
shell metacharacters is eligible, it runs as a direct argv, and a leading
``cd <relative-dir> &&`` or a project test-runner script must resolve inside
the workspace. At most ``MAX_REEXECUTED_COMMANDS`` commands run per criterion.
"""

from __future__ import annotations

from pathlib import Path
import shlex

from ouroboros.orchestrator.evidence.shell_parsing import (
    _is_env_assignment,
    _project_test_runner_script,
    _split_leading_cd,
)

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

    A leading ``cd <relative-dir> &&`` (see ``_split_leading_cd``) is not part
    of the argv: it is a working-directory change that
    ``confined_test_invocation`` resolves inside the workspace. Only the
    remainder is tokenized, so it passes the same metacharacter gate.
    """
    leading_cd = _split_leading_cd(command)
    argv = safe_test_argv(leading_cd[1] if leading_cd is not None else command)
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


def confined_test_invocation(
    command: str, workspace: str | None
) -> tuple[dict[str, str], tuple[str, ...], str | None] | None:
    """Return ``(environment delta, argv, cwd)`` confined to the workspace, or None.

    ``cwd`` is ``workspace`` unchanged unless the command starts with
    ``cd <relative-dir> &&``; then it is that directory, resolved (symlinks
    included) and required to be an existing directory inside the workspace.
    A project test-runner script (``tests/runtests.py``, ``manage.py``,
    ``bin/test``) must resolve to a regular file inside the workspace, the
    same bar an inline program's import must clear to anchor anything: a
    script outside the workspace is not the project's runner and is never run.
    """
    invocation = safe_test_invocation(command)
    if invocation is None:
        return None
    env_delta, argv = invocation
    leading_cd = _split_leading_cd(command)
    script = _project_test_runner_script(argv)
    if leading_cd is None and script is None:
        return env_delta, argv, workspace
    if workspace is None:
        return None
    try:
        root = Path(workspace).resolve()
        directory = (root / leading_cd[0]).resolve() if leading_cd is not None else root
        if not directory.is_relative_to(root) or not directory.is_dir():
            return None
        if script is not None:
            target = (directory / script).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                return None
    except (OSError, RuntimeError, ValueError):
        return None
    return env_delta, argv, str(directory) if leading_cd is not None else workspace


__all__ = [
    "MAX_REEXECUTED_COMMANDS",
    "confined_test_invocation",
    "safe_test_argv",
    "safe_test_invocation",
]
