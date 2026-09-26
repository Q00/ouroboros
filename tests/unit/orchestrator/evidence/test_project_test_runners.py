"""Project test runners (Django, SymPy) and ``cd <dir> &&`` for test re-execution.

The matcher recognized pytest, unittest and tox, so a worker that verified a
Django fix with ``python tests/runtests.py migrations`` or a SymPy fix with
``bin/test`` had its true ``tests_passed`` claim rejected: re-execution
selected nothing. Recognition is by executable/script name and subcommand;
re-execution stays a direct argv (no shell) confined to the workspace.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.harness_observation import (
    CommandObservation,
    WorkspaceObservation,
    build_observation_message,
)
from ouroboros.orchestrator.evidence.shell_parsing import (
    _looks_like_test_command,
    _split_leading_cd,
)
from ouroboros.orchestrator.evidence.test_detection import (
    _runtime_messages_support_test_claim,
)
from ouroboros.orchestrator.evidence.test_reexecution import (
    MAX_REEXECUTED_COMMANDS,
    confined_test_invocation,
    reexecute_test_commands,
    safe_test_invocation,
    select_test_reexecution_commands,
)

# Verbatim from the Django dev run (django__django-14580, claude-sonnet-4-6):
# the workers' ``commands_run`` / ``tests_passed`` entries and transcript commands.
DJANGO_WRITER = "python tests/runtests.py migrations.test_writer"
DJANGO_MIGRATIONS = "python tests/runtests.py migrations"
DJANGO_WRITER_QUIET = "python tests/runtests.py migrations.test_writer --verbosity=0"
DJANGO_ABSOLUTE = "python /testbed/tests/runtests.py migrations.test_writer"
DJANGO_PIPED = "python tests/runtests.py migrations 2>&1 | tail -10"
DJANGO_GREPPED = 'python tests/runtests.py migrations 2>&1 | grep -E "(OK|FAILED|Ran)"'
DJANGO_ABSOLUTE_CD_PIPED = (
    "cd /testbed/tests && python -m django test migrations.test_writer "
    "--settings=test_sqlite 2>&1 | tail -30"
)

DJANGO_OUTPUT = (
    "Found 49 test(s).\nSystem check identified no issues (0 silenced).\n"
    "..........\n----------------------------------------------------------------------\n"
    "Ran 49 tests in 0.214s\n\nOK\n"
)


def _bash_call(command: str, call_id: str = "call-1") -> AgentMessage:
    return AgentMessage(
        type="tool",
        content=f"Bash: {command}",
        tool_name="Bash",
        data={"tool_input": {"command": command}, "tool_call_id": call_id},
    )


def _bash_result(call_id: str = "call-1", **data: object) -> AgentMessage:
    payload: dict[str, object] = {"subtype": "tool_result", "tool_call_id": call_id}
    payload.update(data)
    return AgentMessage(type="tool_result", content="", data=payload)


def _django_workspace(root: Path) -> Path:
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "runtests.py").write_text("print('runner')\n", encoding="utf-8")
    return root


class TestRunnerRecognition:
    @pytest.mark.parametrize(
        "command",
        [
            DJANGO_WRITER,
            DJANGO_MIGRATIONS,
            DJANGO_WRITER_QUIET,
            DJANGO_ABSOLUTE,
            "python3 tests/runtests.py --parallel 1 migrations",
            "./tests/runtests.py migrations",
            "tests/runtests.py migrations",
            "python -m django test migrations.test_writer --settings=tests.test_sqlite",
            "python manage.py test",
            "python src/manage.py test app.tests",
            "./manage.py test",
            "django-admin test --settings=proj.settings",
            "bin/test sympy/core/tests/test_basic.py",
            "./bin/test -k test_foo",
            "python bin/test sympy/core",
            "bin/doctest sympy/core",
            "python bin/doctest",
            "DJANGO_SETTINGS_MODULE=test_sqlite python tests/runtests.py migrations",
        ],
    )
    def test_project_runners_are_test_commands(self, command: str) -> None:
        assert _looks_like_test_command(command)

    @pytest.mark.parametrize(
        "command",
        [
            # Not the test subcommand.
            "python manage.py migrate",
            "python manage.py runserver",
            "django-admin runserver",
            "python -m django check",
            "python -m django",
            # A bare name is a PATH lookup, not the project's script.
            "runtests.py migrations",
            "manage.py test",
            # ``test`` alone is the shell builtin; an absolute or escaping
            # ``bin/test`` is not the project's runner.
            "test -f setup.py",
            "/usr/bin/test -f setup.py",
            "python /usr/bin/test",
            "python ../bin/test",
            "bin/testing",
            "python tests/runtests_helper.py",
            # Interpreter options before the script are not accepted.
            "python -W error tests/runtests.py",
            # Non-executing modes.
            "python tests/runtests.py --help",
            "bin/test --help",
            "python -m django test --version",
            # Pipes and compound text stay unrecognized.
            DJANGO_PIPED,
            DJANGO_GREPPED,
            DJANGO_ABSOLUTE_CD_PIPED,
            "python tests/runtests.py migrations; echo done",
            "python tests/runtests.py migrations || true",
        ],
    )
    def test_non_runner_forms_are_not_test_commands(self, command: str) -> None:
        assert not _looks_like_test_command(command)


class TestLeadingCd:
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            (
                "cd tests && python runtests.py migrations",
                ("tests", "python runtests.py migrations"),
            ),
            ("cd sub/dir && pytest -q", ("sub/dir", "pytest -q")),
            ("cd ./pkg && bin/test", ("./pkg", "bin/test")),
            ('cd "my tests" && pytest', ("my tests", "pytest")),
        ],
    )
    def test_relative_cd_splits_into_directory_and_command(
        self, command: str, expected: tuple[str, str]
    ) -> None:
        assert _split_leading_cd(command) == expected
        assert _looks_like_test_command(command)

    @pytest.mark.parametrize(
        "command",
        [
            # Absolute, home, previous-directory, and escaping directories.
            "cd /testbed && python tests/runtests.py migrations",
            "cd ~ && pytest",
            "cd ~/proj && pytest",
            "cd - && pytest",
            "cd .. && python tests/runtests.py migrations",
            "cd tests/../.. && pytest",
            "cd $HOME && pytest",
            "cd te* && pytest",
            # Any other shell operator keeps the command unrecognized.
            "cd tests; python runtests.py migrations",
            "cd tests || python runtests.py migrations",
            "cd tests && python runtests.py migrations && rm -rf .",
            "cd tests && python runtests.py migrations | tail -5",
            "cd tests && python runtests.py migrations 2>&1",
            "cd tests && python runtests.py migrations > out.txt",
            "cd tests && python runtests.py $(id)",
            "cd tests && python runtests.py migrations &",
            "cd tests&&python runtests.py migrations",
            "cd tests && python runtests.py migrations\nrm -rf .",
            # Not a single cd.
            "cd && pytest",
            "cd a b && pytest",
            "pushd tests && pytest",
            "cd tests &&",
            # The remainder must itself be a test command.
            "cd tests && ls -la",
        ],
    )
    def test_other_cd_forms_are_not_test_commands(self, command: str) -> None:
        assert not _looks_like_test_command(command)

    def test_split_rejects_escapes_and_operators(self) -> None:
        assert _split_leading_cd("cd /abs && pytest") is None
        assert _split_leading_cd("cd .. && pytest") is None
        assert _split_leading_cd("cd tests && pytest | tail -5") is None
        assert _split_leading_cd("cd tests && pytest && pytest") is None

    def test_safe_invocation_keeps_its_shape_and_drops_the_cd(self) -> None:
        assert safe_test_invocation("cd tests && python runtests.py migrations") == (
            {},
            ("python", "runtests.py", "migrations"),
        )
        assert safe_test_invocation("cd tests && FLAG=1 python runtests.py") == (
            {"FLAG": "1"},
            ("python", "runtests.py"),
        )
        assert safe_test_invocation("cd /abs && pytest") is None
        assert safe_test_invocation("cd tests && pytest | tail -5") is None


class TestConfinement:
    def test_cd_resolves_to_an_existing_workspace_directory(self, tmp_path: Path) -> None:
        workspace = _django_workspace(tmp_path)
        confined = confined_test_invocation(
            "cd tests && python runtests.py migrations", str(workspace)
        )
        assert confined == (
            {},
            ("python", "runtests.py", "migrations"),
            str((workspace / "tests").resolve()),
        )
        # No cd: the working directory is the workspace, unchanged.
        assert confined_test_invocation(DJANGO_MIGRATIONS, str(workspace)) == (
            {},
            ("python", "tests/runtests.py", "migrations"),
            str(workspace),
        )

    def test_missing_directory_or_script_is_not_runnable(self, tmp_path: Path) -> None:
        assert confined_test_invocation("cd absent && pytest", str(tmp_path)) is None
        assert confined_test_invocation(DJANGO_MIGRATIONS, str(tmp_path)) is None
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "test").mkdir()  # a directory, not a file
        assert confined_test_invocation("bin/test", str(tmp_path)) is None

    def test_symlink_escape_is_refused(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        outside = tmp_path / "outside"
        workspace.mkdir()
        (outside / "tests").mkdir(parents=True)
        (outside / "tests" / "runtests.py").write_text("", encoding="utf-8")
        os.symlink(outside, workspace / "link")
        os.symlink(outside / "tests", workspace / "tests")

        assert confined_test_invocation("cd link && pytest", str(workspace)) is None
        assert confined_test_invocation(DJANGO_MIGRATIONS, str(workspace)) is None

    def test_script_outside_the_workspace_is_refused(self, tmp_path: Path) -> None:
        workspace = _django_workspace(tmp_path / "ws")
        # Recognized as a Django runner, but the script is not a workspace file.
        assert _looks_like_test_command(DJANGO_ABSOLUTE)
        assert confined_test_invocation(DJANGO_ABSOLUTE, str(workspace)) is None
        absolute_inside = f"python {workspace / 'tests' / 'runtests.py'} migrations"
        assert confined_test_invocation(absolute_inside, str(workspace)) is not None

    def test_no_workspace_refuses_workspace_dependent_forms(self) -> None:
        assert confined_test_invocation("cd tests && pytest", None) is None
        assert confined_test_invocation(DJANGO_MIGRATIONS, None) is None
        assert confined_test_invocation("pytest -q", None) == ({}, ("pytest", "-q"), None)


class TestDjangoDevRunRegression:
    """The dev-run claim shape: free-text ``tests_passed``, runner in ``commands_run``."""

    def _transcript(self) -> tuple[AgentMessage, ...]:
        # The worker's own runs were piped; Claude results carry no exit code.
        return (
            _bash_call(DJANGO_PIPED, "c1"),
            _bash_result("c1", tool_result_text="Ran 578 tests in 12.1s\n\nOK"),
            _bash_call(DJANGO_GREPPED, "c2"),
            _bash_result("c2", tool_result_text="Ran 578 tests in 12.0s\nOK"),
        )

    def test_reported_runner_commands_are_selected(self, tmp_path: Path) -> None:
        workspace = _django_workspace(tmp_path)
        final = json.dumps(
            {
                "tests_passed": ["migrations.test_writer (49 tests)", "migrations (578 tests)"],
                "commands_run": [DJANGO_WRITER, DJANGO_MIGRATIONS],
            }
        )

        selected = select_test_reexecution_commands(
            final_message=final, messages=self._transcript(), task_cwd=str(workspace)
        )

        # Piped transcript runs stay unselected; the reported runs are selected.
        assert selected == (DJANGO_WRITER, DJANGO_MIGRATIONS)

    def test_command_valued_claim_is_selected_and_capped(self, tmp_path: Path) -> None:
        workspace = _django_workspace(tmp_path)
        final = json.dumps(
            {
                "tests_passed": [DJANGO_WRITER_QUIET],
                "commands_run": [DJANGO_WRITER, DJANGO_MIGRATIONS, "python /tmp/repro.py"],
            }
        )

        selected = select_test_reexecution_commands(
            final_message=final, messages=self._transcript(), task_cwd=str(workspace)
        )

        assert selected == (DJANGO_WRITER_QUIET, DJANGO_WRITER, DJANGO_MIGRATIONS)
        assert len(selected) == MAX_REEXECUTED_COMMANDS

    def test_absolute_container_path_claim_is_not_selected_on_the_host(
        self, tmp_path: Path
    ) -> None:
        workspace = _django_workspace(tmp_path)
        final = json.dumps({"tests_passed": [DJANGO_ABSOLUTE]})

        assert (
            select_test_reexecution_commands(
                final_message=final, messages=self._transcript(), task_cwd=str(workspace)
            )
            == ()
        )

    def test_reexecuted_django_run_backs_the_command_claim(self, tmp_path: Path) -> None:
        observation = build_observation_message(
            WorkspaceObservation(
                changed_paths=frozenset(),
                command_runs=(
                    CommandObservation(
                        command=DJANGO_WRITER, returncode=0, output_tail=DJANGO_OUTPUT
                    ),
                ),
            )
        )
        messages = (*self._transcript(), observation)

        assert _runtime_messages_support_test_claim(
            value=DJANGO_WRITER, backed_commands=(), messages=messages, task_cwd=str(tmp_path)
        )
        failed = build_observation_message(
            WorkspaceObservation(
                changed_paths=frozenset(),
                command_runs=(
                    CommandObservation(
                        command=DJANGO_WRITER,
                        returncode=1,
                        output_tail="Ran 49 tests in 0.2s\n\nFAILED (failures=1)\n",
                    ),
                ),
            )
        )
        assert not _runtime_messages_support_test_claim(
            value=DJANGO_WRITER,
            backed_commands=(),
            messages=(*self._transcript(), failed),
            task_cwd=str(tmp_path),
        )


class TestExecution:
    RUNNER = (
        "import os, sys\n"
        "print('cwd=' + os.getcwd())\n"
        "print('args=' + ' '.join(sys.argv[1:]))\n"
        "print('Ran 2 tests in 0.001s')\nprint()\nprint('OK')\n"
    )

    def _workspace(self, root: Path) -> Path:
        (root / "tests").mkdir(parents=True)
        (root / "tests" / "runtests.py").write_text(self.RUNNER, encoding="utf-8")
        return root

    async def test_django_runner_runs_as_direct_argv(self, tmp_path: Path) -> None:
        workspace = self._workspace(tmp_path)
        command = f"{sys.executable} tests/runtests.py migrations"

        runs = await reexecute_test_commands(
            (command,), cwd=str(workspace), env={"PATH": "/usr/bin:/bin"}, timeout_seconds=60
        )

        assert len(runs) == 1 and runs[0].succeeded
        assert f"cwd={workspace.resolve()}" in runs[0].output_tail
        assert "args=migrations" in runs[0].output_tail

    async def test_cd_prefix_changes_only_the_working_directory(self, tmp_path: Path) -> None:
        workspace = self._workspace(tmp_path)
        marker = tmp_path / "marker"
        command = f"cd tests && {sys.executable} runtests.py migrations"

        selected = select_test_reexecution_commands(
            final_message=json.dumps({"tests_passed": [command]}),
            messages=(_bash_call("ls"), _bash_result()),
            task_cwd=str(workspace),
        )
        assert selected == (command,)
        runs = await reexecute_test_commands(
            selected, cwd=str(workspace), env={"PATH": "/usr/bin:/bin"}, timeout_seconds=60
        )

        assert len(runs) == 1 and runs[0].succeeded
        assert runs[0].command == command
        assert f"cwd={(workspace / 'tests').resolve()}" in runs[0].output_tail
        assert not marker.exists()

    async def test_escaping_or_compound_cd_is_never_executed(self, tmp_path: Path) -> None:
        workspace = self._workspace(tmp_path / "ws")
        marker = tmp_path / "marker"
        commands = (
            f"cd .. && {sys.executable} ws/tests/runtests.py",
            f"cd {workspace} && {sys.executable} tests/runtests.py",
            f"cd tests && {sys.executable} runtests.py && touch {marker}",
            f"cd tests; touch {marker}",
        )

        runs = await reexecute_test_commands(
            commands, cwd=str(workspace), env={"PATH": "/usr/bin:/bin"}, timeout_seconds=30
        )

        assert runs == ()
        assert not marker.exists()
