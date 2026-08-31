"""Harness re-execution of claimed test commands backs ``tests_passed``.

The transcript verifier needs runtime output that proves a test run passed.
Codex completions without an ``exit_code`` or with truncated output leave a
real ``pytest`` run unprovable; the harness re-runs the command itself and
judges its own exit status and output by the same rules.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.harness_observation import (
    CommandObservation,
    WorkspaceObservation,
    build_observation_message,
    insert_observation_message,
)
from ouroboros.orchestrator.evidence.test_detection import (
    _runtime_messages_support_test_claim,
)
from ouroboros.orchestrator.evidence.test_reexecution import (
    MAX_REEXECUTED_COMMANDS,
    reexecute_test_commands,
    safe_test_argv,
    safe_test_invocation,
    select_test_reexecution_commands,
)
from ouroboros.orchestrator.evidence.verification import (
    _verify_atomic_evidence_against_runtime_messages,
)
from ouroboros.orchestrator.evidence_schema import EvidenceRecord
from ouroboros.orchestrator.leaf_dispatcher import LeafDispatcher, LeafDispatchState
from ouroboros.orchestrator.profile_loader import load_profile

TEST_COMMAND = "python3 -m pytest -q test_hello.py"


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


def _final(evidence: dict[str, object]) -> AgentMessage:
    return AgentMessage(type="result", content=json.dumps(evidence), data={"subtype": "success"})


def _codex_unprovable_transcript() -> tuple[AgentMessage, ...]:
    """A real pytest run whose completion carries neither exit_code nor output."""
    return (
        _bash_call(f"/bin/zsh -lc '{TEST_COMMAND}'"),
        _bash_result(),
    )


class TestSelection:
    def test_no_claims_or_no_evidence_selects_nothing(self) -> None:
        assert (
            select_test_reexecution_commands(final_message=None, messages=(), task_cwd=None) == ()
        )
        assert (
            select_test_reexecution_commands(final_message="not json", messages=(), task_cwd=None)
            == ()
        )
        assert (
            select_test_reexecution_commands(
                final_message=json.dumps({"files_touched": ["a.py"]}),
                messages=(),
                task_cwd=None,
            )
            == ()
        )

    def test_already_proven_claim_is_not_reexecuted(self, tmp_path: Path) -> None:
        messages = (
            _bash_call(TEST_COMMAND),
            _bash_result(exit_code=0, output="1 passed in 0.01s"),
        )
        final = json.dumps({"tests_passed": [TEST_COMMAND]})

        assert (
            select_test_reexecution_commands(
                final_message=final, messages=messages, task_cwd=str(tmp_path)
            )
            == ()
        )

    def test_unprovable_claim_selects_the_claim_and_transcript_commands(
        self, tmp_path: Path
    ) -> None:
        final = json.dumps(
            {
                "tests_passed": [TEST_COMMAND],
                "commands_run": ["pytest -q", "ls -la"],
            }
        )

        selected = select_test_reexecution_commands(
            final_message=final,
            messages=_codex_unprovable_transcript(),
            task_cwd=str(tmp_path),
        )

        # The claim itself first, then reported test commands, then the
        # unwrapped transcript command; non-test commands and duplicates drop.
        assert selected == (TEST_COMMAND, "pytest -q")

    def test_selection_is_capped(self, tmp_path: Path) -> None:
        commands = [f"pytest -q tests/test_{index}.py" for index in range(6)]
        final = json.dumps({"tests_passed": ["tests/test_x.py::test_y"], "commands_run": commands})

        selected = select_test_reexecution_commands(
            final_message=final, messages=(_bash_call("ls"), _bash_result()), task_cwd=str(tmp_path)
        )

        assert len(selected) == MAX_REEXECUTED_COMMANDS


class TestSafeArgv:
    def test_plain_test_commands_tokenize(self) -> None:
        assert safe_test_argv("python3 -m pytest -q test_hello.py") == (
            "python3",
            "-m",
            "pytest",
            "-q",
            "test_hello.py",
        )
        assert safe_test_argv("pytest tests/test_a.py::test_b") is not None

    @pytest.mark.parametrize(
        "command",
        [
            'pytest -q "$(touch harness_escape_marker)"',
            "pytest; rm -rf .",
            "pytest && curl evil",
            "pytest `id`",
            "pytest | tee out",
            "pytest > out.txt",
            "t=$(mktemp -d) && pytest",
            "pytest\nrm -rf .",
            'pytest "unclosed',
            "",
        ],
    )
    def test_shell_dependent_text_is_rejected(self, command: str) -> None:
        assert safe_test_argv(command) is None

    def test_env_prefix_becomes_a_delta_not_an_executable(self) -> None:
        """Round-2 blocker: the recognizer accepts env prefixes, so must execution."""
        assert safe_test_invocation("REEXEC_FLAG=yes python -m pytest -q test_env.py") == (
            {"REEXEC_FLAG": "yes"},
            ("python", "-m", "pytest", "-q", "test_env.py"),
        )
        assert safe_test_invocation("env A=1 B=2 pytest -q") == (
            {"A": "1", "B": "2"},
            ("pytest", "-q"),
        )
        assert safe_test_invocation("pytest -q") == ({}, ("pytest", "-q"))
        # Assignments alone are not a runnable command.
        assert safe_test_invocation("REEXEC_FLAG=yes") is None
        assert safe_test_invocation('FLAG="$(id)" pytest -q') is None


class TestReexecution:
    async def test_records_exit_status_and_output(self, tmp_path: Path) -> None:
        (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
        python = sys.executable

        runs = await reexecute_test_commands(
            (
                f"{python} -m pytest -q -p no:cacheprovider test_ok.py",
                f"{python} -m pytest -q -p no:cacheprovider test_absent.py",
            ),
            cwd=str(tmp_path),
            env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
            timeout_seconds=60,
        )

        assert len(runs) == 2
        assert runs[0].returncode == 0 and "1 passed" in runs[0].output_tail
        assert runs[1].returncode != 0
        assert runs[0].succeeded and not runs[1].succeeded

    async def test_env_prefixed_command_runs_with_the_delta_applied(self, tmp_path: Path) -> None:
        """The round-2 repro: an accepted env-prefixed claim must actually run."""
        (tmp_path / "test_env.py").write_text(
            "import os\n\ndef test_env():\n    assert os.environ['REEXEC_FLAG'] == 'yes'\n",
            encoding="utf-8",
        )
        python = sys.executable
        command = f"REEXEC_FLAG=yes {python} -m pytest -q -p no:cacheprovider test_env.py"

        selected = select_test_reexecution_commands(
            final_message=json.dumps({"tests_passed": [command]}),
            messages=(_bash_call("ls"), _bash_result()),
            task_cwd=str(tmp_path),
        )
        assert selected == (command,)

        runs = await reexecute_test_commands(
            selected,
            cwd=str(tmp_path),
            env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
            timeout_seconds=60,
        )

        assert len(runs) == 1
        assert runs[0].returncode == 0 and "1 passed" in runs[0].output_tail
        assert runs[0].succeeded

    async def test_timeout_is_recorded_not_raised(self, tmp_path: Path) -> None:
        runs = await reexecute_test_commands(
            ("sleep 5",),
            cwd=str(tmp_path),
            env={"PATH": "/usr/bin:/bin"},
            timeout_seconds=0.2,
        )

        assert len(runs) == 1
        assert runs[0].timed_out is True
        assert runs[0].succeeded is False

    async def test_injection_text_is_never_executed(self, tmp_path: Path) -> None:
        """The blocker regression: substitution text must not run at all."""
        marker = tmp_path / "harness_escape_marker"
        selected = select_test_reexecution_commands(
            final_message=json.dumps(
                {"tests_passed": ['pytest -q "$(touch harness_escape_marker)"']}
            ),
            messages=(_bash_call("ls"), _bash_result()),
            task_cwd=str(tmp_path),
        )
        assert selected == ()

        runs = await reexecute_test_commands(
            ('pytest -q "$(touch harness_escape_marker)"',),
            cwd=str(tmp_path),
            env={"PATH": "/usr/bin:/bin"},
            timeout_seconds=30,
        )
        assert runs == ()
        assert not marker.exists()


class TestAuthorityGates:
    def _state(self) -> LeafDispatchState:
        final = _final({"tests_passed": [TEST_COMMAND]})
        return LeafDispatchState(
            messages=[*_codex_unprovable_transcript(), final],
            runtime_handle=None,
            final_message=final.content,
            success=True,
        )

    async def _attach(self, executor: object, tools: list[str]) -> WorkspaceObservation:
        dispatcher = LeafDispatcher(executor)  # type: ignore[arg-type]
        return await dispatcher._attach_test_reexecution(
            WorkspaceObservation(changed_paths=frozenset()),
            state=self._state(),
            task_cwd="/tmp",
            tools=tools,
        )

    async def test_no_bash_authority_skips_reexecution(self) -> None:
        executor = SimpleNamespace(_run_verify_commands=True, _verify_command_timeout_seconds=1)
        observation = await self._attach(executor, tools=["Read", "Edit"])
        assert observation.command_runs == ()
        observation = await self._attach(executor, tools=[])
        assert observation.command_runs == ()

    async def test_disabled_verification_skips_reexecution(self) -> None:
        executor = SimpleNamespace(_run_verify_commands=False, _verify_command_timeout_seconds=1)
        observation = await self._attach(executor, tools=["Bash"])
        assert observation.command_runs == ()


class TestClaimSupport:
    def _observation(self, **run_kwargs: object) -> AgentMessage:
        defaults: dict[str, object] = {
            "command": TEST_COMMAND,
            "returncode": 0,
            "output_tail": "1 passed in 0.02s",
            "timed_out": False,
        }
        defaults.update(run_kwargs)
        run = CommandObservation(**defaults)  # type: ignore[arg-type]
        return build_observation_message(
            WorkspaceObservation(changed_paths=frozenset(), command_runs=(run,))
        )

    def test_successful_reexecution_backs_the_claim(self, tmp_path: Path) -> None:
        messages = (*_codex_unprovable_transcript(), self._observation())

        assert _runtime_messages_support_test_claim(
            value=TEST_COMMAND, backed_commands=(), messages=messages, task_cwd=str(tmp_path)
        )
        # A file/node-id claim additionally needs the file to be this run's work.
        assert not _runtime_messages_support_test_claim(
            value="test_hello.py", backed_commands=(), messages=messages, task_cwd=str(tmp_path)
        )
        touched = build_observation_message(
            WorkspaceObservation(
                changed_paths=frozenset({"test_hello.py"}),
                command_runs=(
                    CommandObservation(
                        command=TEST_COMMAND, returncode=0, output_tail="1 passed in 0.02s"
                    ),
                ),
            )
        )
        assert _runtime_messages_support_test_claim(
            value="test_hello.py::test_greet",
            backed_commands=(),
            messages=(*_codex_unprovable_transcript(), touched),
            task_cwd=str(tmp_path),
        )

    @pytest.mark.parametrize(
        "run_kwargs",
        [
            {"returncode": 1, "output_tail": "1 passed, 1 failed"},
            {"timed_out": True},
            {"output_tail": ""},
            {"output_tail": "collected 0 items"},
            {"command": "ls -la", "output_tail": "1 passed"},
        ],
    )
    def test_failed_or_unproven_reexecution_does_not_back_the_claim(
        self, tmp_path: Path, run_kwargs: dict[str, object]
    ) -> None:
        messages = (*_codex_unprovable_transcript(), self._observation(**run_kwargs))

        assert not _runtime_messages_support_test_claim(
            value=TEST_COMMAND, backed_commands=(), messages=messages, task_cwd=str(tmp_path)
        )

    def test_reexecution_targets_only_its_own_test(self, tmp_path: Path) -> None:
        messages = (*_codex_unprovable_transcript(), self._observation())

        assert not _runtime_messages_support_test_claim(
            value="tests/test_other.py::test_z",
            backed_commands=(),
            messages=messages,
            task_cwd=str(tmp_path),
        )


class TestVerifierIntegration:
    def test_unprovable_codex_transcript_passes_with_reexecution(self, tmp_path: Path) -> None:
        (tmp_path / "hello.py").write_text("def greet():\n    return 'hi'\n", encoding="utf-8")
        evidence = {
            "files_touched": ["hello.py"],
            "commands_run": [TEST_COMMAND],
            "tests_passed": [TEST_COMMAND],
        }
        edit = AgentMessage(
            type="tool",
            content="Edit hello.py",
            tool_name="Edit",
            data={"tool_input": {"file_path": str(tmp_path / "hello.py")}, "tool_call_id": "e1"},
        )
        edit_done = _bash_result(call_id="e1", exit_code=0)
        messages = [edit, edit_done, *_codex_unprovable_transcript(), _final(evidence)]

        def verify(items: tuple[AgentMessage, ...]) -> object:
            return _verify_atomic_evidence_against_runtime_messages(
                messages=items,
                typed_evidence=EvidenceRecord(data=evidence),
                ac_content="Implement greet() in hello.py with tests",
                execution_profile=load_profile("code"),
                task_cwd=str(tmp_path),
                adapter_working_directory=str(tmp_path),
            )

        rejected = verify(tuple(messages))
        assert rejected.passed is False
        assert "tests_passed" in rejected.reasons[0]

        insert_observation_message(
            messages,
            WorkspaceObservation(
                changed_paths=frozenset({"hello.py"}),
                command_runs=(
                    CommandObservation(
                        command=TEST_COMMAND, returncode=0, output_tail="1 passed in 0.02s"
                    ),
                ),
            ),
        )
        accepted = verify(tuple(messages))
        assert accepted.passed is True, accepted.reasons
