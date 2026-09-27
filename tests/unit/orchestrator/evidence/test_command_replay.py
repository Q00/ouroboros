"""Replay-first corroboration: claims are backed by replaying observed commands.

The verifier used to corroborate ``tests_passed`` / ``commands_run`` claims only
for recognized test runners and narrow claim formats, so ``make test``,
``npm test``, ``./run_tests.sh``, ``pytest -q | tail -5`` and free-text claims
were rejected for correct work. The harness now replays a command the
transcript shows the leaf running, in an isolated copy of the workspace, and a
linked claim is corroborated when that replay exits 0.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

import pytest

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence import command_replay
from ouroboros.orchestrator.evidence.command_replay import (
    REPLAY_OUTPUT_FILTERS,
    REPLAY_SKIPPED_NETWORK,
    ReplayCandidate,
    claim_links_to_command,
    output_filter_core,
    replay_candidate,
    replay_commands,
    select_replay_candidates,
)
from ouroboros.orchestrator.evidence.harness_observation import (
    CommandObservation,
    WorkspaceObservation,
    build_observation_message,
    insert_observation_message,
)
from ouroboros.orchestrator.evidence.replay_policy import (
    VIEWER_PROGRAMS,
    ResolvedRunner,
    claim_target_operands,
    replay_allowed,
    replay_denied,
    resolve_replay_program,
)
from ouroboros.orchestrator.evidence.shell_parsing import (
    _output_filter_pipeline_is_pipefail_protected,
    command_line_assignments,
    program_chain,
)
from ouroboros.orchestrator.evidence.test_detection import _test_command_targets_claim
from ouroboros.orchestrator.evidence.verification import (
    _verify_atomic_evidence_against_runtime_messages,
)
from ouroboros.orchestrator.evidence_schema import EvidenceRecord
from ouroboros.orchestrator.leaf_dispatcher import LeafDispatcher, LeafDispatchState
from ouroboros.orchestrator.profile_loader import load_profile
from ouroboros.orchestrator.verifier import VerifierVerdict

AC = "Fix add() in calc.py so the project's tests pass"
PYTHON_BIN = str(Path(sys.executable).parent)

# Verbatim from a Django dev run: the worker's claim and its transcript command.
DJANGO_WRITER_CLAIM = "migrations.test_writer (49 tests)"
DJANGO_WRITER = "python tests/runtests.py migrations.test_writer"
DJANGO_RUNNER = (
    "import os, sys\n"
    "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
    "print('Found 49 test(s).')\n"
    "print('Ran 49 tests in 0.214s')\n"
    "print()\n"
    "from calc import add\n"
    "sys.exit(0 if add(2, 3) == 5 else 1)\n"
)


def _bash_call(command: str, call_id: str) -> AgentMessage:
    return AgentMessage(
        type="tool",
        content=f"Bash: {command}",
        tool_name="Bash",
        data={"tool_input": {"command": command}, "tool_call_id": call_id},
    )


def _edit(path: Path, call_id: str) -> tuple[AgentMessage, ...]:
    return (
        AgentMessage(
            type="tool",
            content=f"Edit {path.name}",
            tool_name="Edit",
            data={"tool_input": {"file_path": str(path)}, "tool_call_id": call_id},
        ),
        _bash_result(call_id, exit_code=0),
    )


def _bash_result(call_id: str, **data: object) -> AgentMessage:
    payload: dict[str, object] = {"subtype": "tool_result", "tool_call_id": call_id}
    payload.update(data)
    return AgentMessage(type="tool_result", content="", data=payload)


def _ran(command: str, call_id: str, exit_code: int | None = None) -> tuple[AgentMessage, ...]:
    """A transcript Bash call as Codex records it, wrapper included."""
    result = {} if exit_code is None else {"exit_code": exit_code}
    return (
        _bash_call(f"/bin/zsh -lc {json.dumps(command)}", call_id),
        _bash_result(call_id, **result),
    )


def _final(evidence: dict[str, object]) -> AgentMessage:
    return AgentMessage(type="result", content=json.dumps(evidence), data={"subtype": "success"})


def _executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _workspace(root: Path, *, correct: bool = True) -> Path:
    """A project whose tests run through make, npm and a shell script."""
    root.mkdir(parents=True, exist_ok=True)
    body = "a + b" if correct else "a - b"
    (root / "calc.py").write_text(f"def add(a, b):\n    return {body}\n", encoding="utf-8")
    check = f"{sys.executable} -c 'from calc import add; assert add(2, 3) == 5'"
    (root / "Makefile").write_text(f"test:\n\t{check}\n", encoding="utf-8")
    _executable(root / "run_tests.sh", f"#!/bin/sh\nset -e\n{check}\necho 'all green'\n")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "runtests.py").write_text(DJANGO_RUNNER, encoding="utf-8")
    return root


@pytest.fixture
def fake_npm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An ``npm`` on PATH whose ``npm test`` runs the project's check."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _executable(
        bin_dir / "npm",
        "#!/bin/sh\n"
        '[ "$1" = test ] || exit 2\n'
        f"exec {sys.executable} -c 'from calc import add; assert add(2, 3) == 5'\n",
    )
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), PYTHON_BIN, "/usr/bin", "/bin"]))
    return bin_dir


async def _dispatch_and_verify(
    workspace: Path,
    transcript: tuple[AgentMessage, ...],
    evidence: dict[str, object],
) -> tuple[VerifierVerdict, WorkspaceObservation]:
    """Run the dispatcher's replay step, then the transcript verifier."""
    edit = AgentMessage(
        type="tool",
        content="Edit calc.py",
        tool_name="Edit",
        data={"tool_input": {"file_path": str(workspace / "calc.py")}, "tool_call_id": "edit"},
    )
    final = _final(evidence)
    messages = [edit, _bash_result("edit", exit_code=0), *transcript, final]
    state = LeafDispatchState(
        messages=messages, runtime_handle=None, final_message=final.content, success=True
    )
    executor = SimpleNamespace(_run_verify_commands=True, _verify_command_timeout_seconds=60)
    observation = await LeafDispatcher(executor)._attach_test_reexecution(  # type: ignore[arg-type]
        WorkspaceObservation(changed_paths=frozenset({"calc.py"})),
        state=state,
        task_cwd=str(workspace),
        tools=["Bash", "Edit"],
    )
    insert_observation_message(messages, observation)
    verdict = _verify_atomic_evidence_against_runtime_messages(
        messages=tuple(messages),
        typed_evidence=EvidenceRecord(data=evidence),
        ac_content=AC,
        execution_profile=load_profile("code"),
        task_cwd=str(workspace),
        adapter_working_directory=str(workspace),
        verify_gate_active=True,
    )
    return verdict, observation


def _evidence(command: str, tests: list[str] | None = None) -> dict[str, object]:
    return {
        "files_touched": ["calc.py"],
        "commands_run": [command],
        "tests_passed": tests if tests is not None else [command],
    }


class TestUnrecognizedRunners:
    @pytest.mark.parametrize("command", ["make test", "npm test", "./run_tests.sh"])
    async def test_zero_exit_replay_corroborates(
        self, tmp_path: Path, fake_npm: Path, command: str
    ) -> None:
        workspace = _workspace(tmp_path / "ws")

        verdict, observation = await _dispatch_and_verify(
            workspace, _ran(command, "c1"), _evidence(command)
        )

        assert verdict.passed is True, verdict.reasons
        assert [run.command for run in observation.command_runs] == [command]
        assert observation.command_runs[0].succeeded

    @pytest.mark.parametrize("command", ["make test", "npm test", "./run_tests.sh"])
    async def test_nonzero_exit_replay_is_rejected(
        self, tmp_path: Path, fake_npm: Path, command: str
    ) -> None:
        workspace = _workspace(tmp_path / "ws", correct=False)

        verdict, observation = await _dispatch_and_verify(
            workspace, _ran(command, "c1"), _evidence(command)
        )

        assert verdict.passed is False
        assert "tests_passed" in verdict.reasons[0]
        assert observation.command_runs and not observation.command_runs[0].succeeded


class TestOutputFilterPipelines:
    @pytest.mark.parametrize(
        ("command", "core"),
        [
            ("pytest -q | tail -5", "pytest -q"),
            ("pytest -q 2>&1 | tail -n 20", "pytest -q"),
            ("make test | grep -E '(OK|FAILED)$' | head -3", "make test"),
            ('python tests/runtests.py migrations 2>&1 | grep -E "(OK|FAILED|Ran)"', None),
            ("./run_tests.sh | sed -n '1,5p' | wc -l", "./run_tests.sh"),
            ("make test", "make test"),
        ],
    )
    def test_pure_filters_reduce_to_the_core_command(self, command: str, core: str | None) -> None:
        expected = core or "python tests/runtests.py migrations"
        assert output_filter_core(command) == expected

    @pytest.mark.parametrize(
        "command",
        [
            "pytest -q | tee out.txt",
            "pytest -q | python summarize.py",
            "pytest -q || true",
            "pytest -q | tail -5; rm -rf .",
            "pytest -q | tail -5 > out.txt",
            'pytest -q | grep "$(id)"',
            "pytest -q | tail -$N",
            "pytest -q | xargs echo",
            "pytest -q |",
        ],
    )
    def test_any_other_construct_stays_unsupported(self, command: str) -> None:
        assert output_filter_core(command) is None

    def test_filter_set_is_explicit(self) -> None:
        assert {"tail", "head", "grep", "sed"} <= REPLAY_OUTPUT_FILTERS
        assert not {"tee", "awk", "xargs", "python", "sh"} & REPLAY_OUTPUT_FILTERS

    async def test_pipeline_replays_the_core_and_uses_its_exit(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")
        piped = "./run_tests.sh 2>&1 | tail -5"

        verdict, observation = await _dispatch_and_verify(
            workspace, _ran(piped, "c1", exit_code=0), _evidence(piped)
        )

        assert verdict.passed is True, verdict.reasons
        run = observation.command_runs[0]
        assert run.command == "./run_tests.sh"
        assert run.transcript_command == piped
        assert "all green" in run.output_tail

    async def test_pipeline_masking_a_failure_is_rejected(self, tmp_path: Path) -> None:
        # The pipeline's own status is tail's; the replayed core fails. The
        # completion carries no exit code, so only replay could back the claim.
        workspace = _workspace(tmp_path / "ws", correct=False)
        piped = "./run_tests.sh | tail -5"

        verdict, observation = await _dispatch_and_verify(
            workspace, _ran(piped, "c1"), _evidence(piped)
        )

        assert verdict.passed is False
        assert observation.command_runs[0].returncode != 0


class TestDenylist:
    @pytest.mark.parametrize(
        "argv",
        [
            ("rm", "-rf", "build"),
            ("/bin/rm", "x"),
            ("sudo", "make", "test"),
            ("curl", "https://example.com"),
            ("wget", "x"),
            ("ssh", "host", "make test"),
            ("docker", "run", "img"),
            ("git", "push"),
            ("git", "commit", "-m", "x"),
            ("git", "reset", "--hard"),
            ("git", "stash"),
            ("git", "-C", "..", "status"),
            ("pip", "install", "x"),
            ("python3", "-m", "pip", "install", "x"),
            ("uv", "pip", "install", "x"),
            ("uv", "add", "x"),
            ("npm", "install"),
            ("npm", "ci"),
            ("yarn",),
            ("yarn", "add", "x"),
            ("pnpm", "i"),
            ("poetry", "add", "x"),
            ("cargo", "install", "x"),
            ("brew", "install", "x"),
            ("apt-get", "install", "x"),
            ("timeout", "60", "curl", "x"),
            ("env", "nice", "-n", "5", "sudo", "true"),
            ("bash", "-c", "make test"),
            ("sh", "-ec", "make test"),
        ],
    )
    def test_denied(self, argv: tuple[str, ...]) -> None:
        assert replay_denied(argv)

    @pytest.mark.parametrize(
        "argv",
        [
            ("make", "test"),
            ("npm", "test"),
            ("npm", "run", "test"),
            ("yarn", "test"),
            ("./run_tests.sh",),
            ("bash", "run_tests.sh"),
            ("python", "-m", "pytest", "-q"),
            ("uv", "run", "pytest"),
            ("poetry", "run", "pytest"),
            ("cargo", "test"),
            ("go", "test", "./..."),
            ("git", "diff", "--stat"),
            ("timeout", "60", "make", "test"),
        ],
    )
    def test_allowed(self, argv: tuple[str, ...]) -> None:
        assert not replay_denied(argv)

    async def test_denied_command_is_never_replayed(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")
        victim = tmp_path / "victim.txt"
        victim.write_text("keep", encoding="utf-8")
        command = f"rm -f {victim}"

        selected = select_replay_candidates(
            final_message=json.dumps({"tests_passed": [command]}),
            messages=_ran(command, "c1"),
            task_cwd=str(workspace),
        )
        assert selected == ()
        candidate = replay_candidate(command, str(workspace))
        assert candidate is not None
        runs = await replay_commands(
            (candidate,), workspace=str(workspace), env=dict(os.environ), timeout_seconds=30
        )
        assert runs == ()
        assert victim.read_text(encoding="utf-8") == "keep"

    async def test_denied_claim_falls_back_to_transcript_rules(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")
        command = "curl -fsS http://localhost:9/health"

        verdict, observation = await _dispatch_and_verify(
            workspace, _ran(command, "c1", exit_code=0), _evidence(command)
        )

        assert observation.command_runs == ()
        assert verdict.passed is False


class TestProtectedBytes:
    async def test_mutating_a_pre_existing_file_is_unsupported(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")
        _executable(workspace / "run_tests.sh", "#!/bin/sh\necho '# touched' >> calc.py\necho ok\n")
        original = (workspace / "calc.py").read_bytes()

        # No exit code in the transcript: only the replay could back the claim.
        verdict, observation = await _dispatch_and_verify(
            workspace, _ran("./run_tests.sh", "c1"), _evidence("./run_tests.sh")
        )

        run = observation.command_runs[0]
        assert run.returncode == 0 and run.mutated and not run.succeeded
        assert verdict.passed is False
        # The replay ran in a copy: the workspace itself is untouched.
        assert (workspace / "calc.py").read_bytes() == original

    async def test_deleting_a_pre_existing_file_is_unsupported(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")
        _executable(workspace / "run_tests.sh", "#!/bin/sh\nmv Makefile Makefile.bak\n")
        candidate = replay_candidate("./run_tests.sh", str(workspace))
        assert candidate is not None

        runs = await replay_commands(
            (candidate,), workspace=str(workspace), env=dict(os.environ), timeout_seconds=30
        )

        assert runs[0].mutated and not runs[0].succeeded
        assert (workspace / "Makefile").exists()

    async def test_new_files_and_caches_are_not_mutation(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")
        _executable(
            workspace / "run_tests.sh",
            "#!/bin/sh\nmkdir -p __pycache__ .pytest_cache\n"
            "echo x > report.xml\necho y > __pycache__/c.pyc\necho z > .pytest_cache/v\n",
        )
        candidate = replay_candidate("./run_tests.sh", str(workspace))
        assert candidate is not None

        runs = await replay_commands(
            (candidate,), workspace=str(workspace), env=dict(os.environ), timeout_seconds=30
        )

        assert runs[0].succeeded and not runs[0].mutated
        assert not (workspace / "report.xml").exists()

    async def test_absolute_workspace_paths_point_at_the_copy(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")
        _executable(workspace / "run_tests.sh", '#!/bin/sh\necho mutated >> "$1"\n')
        target = workspace.resolve() / "calc.py"
        original = target.read_bytes()
        candidate = replay_candidate(f"./run_tests.sh {target}", str(workspace))
        assert candidate is not None

        runs = await replay_commands(
            (candidate,), workspace=str(workspace), env=dict(os.environ), timeout_seconds=30
        )

        assert runs[0].mutated
        assert target.read_bytes() == original

    async def test_dependency_directories_are_linked_not_copied(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")
        (workspace / "node_modules" / "pkg").mkdir(parents=True)
        (workspace / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")
        (workspace / ".git").mkdir()
        (workspace / ".git" / "HEAD").write_text("ref", encoding="utf-8")
        _executable(
            workspace / "run_tests.sh",
            "#!/bin/sh\ntest -L node_modules && test -f node_modules/pkg/index.js "
            "&& test ! -e .git\n",
        )
        candidate = replay_candidate("./run_tests.sh", str(workspace))
        assert candidate is not None

        runs = await replay_commands(
            (candidate,), workspace=str(workspace), env=dict(os.environ), timeout_seconds=30
        )

        assert runs[0].succeeded, runs[0].output_tail

    async def test_workspace_over_the_copy_budget_is_not_replayed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _workspace(tmp_path / "ws")
        monkeypatch.setattr(command_replay, "MAX_COPY_ENTRIES", 2)
        candidate = replay_candidate("./run_tests.sh", str(workspace))
        assert candidate is not None

        runs = await replay_commands(
            (candidate,), workspace=str(workspace), env=dict(os.environ), timeout_seconds=30
        )

        assert runs == ()


class TestFabricationNegativeControls:
    async def test_command_never_run_is_rejected(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")

        verdict, observation = await _dispatch_and_verify(
            workspace, _ran("ls -la", "c1", exit_code=0), _evidence("make test")
        )

        # "make test" passes in this workspace, but the leaf never ran it: the
        # claim text is never replayed.
        assert observation.command_runs == ()
        assert verdict.passed is False
        assert verdict.failure_class == "FABRICATION_SUSPECTED"

    async def test_run_that_exited_nonzero_is_rejected(self, tmp_path: Path) -> None:
        # No exit recorded in the transcript: the replay decides, and fails.
        workspace = _workspace(tmp_path / "ws", correct=False)

        verdict, observation = await _dispatch_and_verify(
            workspace, _ran("make test", "c1"), _evidence("make test")
        )

        assert observation.command_runs[0].returncode != 0
        assert verdict.passed is False

    async def test_recorded_nonzero_exit_is_rejected_although_a_replay_would_pass(
        self, tmp_path: Path
    ) -> None:
        # The transcript run exited 2; the final workspace passes. The run the
        # claim reports failed, so it is not replayed and the claim is rejected.
        workspace = _workspace(tmp_path / "ws")

        verdict, observation = await _dispatch_and_verify(
            workspace, _ran("make test", "c1", exit_code=2), _evidence("make test")
        )

        assert observation.command_runs == ()
        assert verdict.passed is False

    async def test_failed_tool_result_without_exit_code_is_not_replayed(
        self, tmp_path: Path
    ) -> None:
        workspace = _workspace(tmp_path / "ws")
        transcript = (
            _bash_call("/bin/zsh -lc 'make test'", "c1"),
            _bash_result("c1", is_error=True),
        )

        verdict, observation = await _dispatch_and_verify(
            workspace, transcript, _evidence("make test")
        )

        assert observation.command_runs == ()
        assert verdict.passed is False

    async def test_latest_failed_run_is_not_replaced_by_an_earlier_pass(
        self, tmp_path: Path
    ) -> None:
        workspace = _workspace(tmp_path / "ws")

        selected = select_replay_candidates(
            final_message=json.dumps({"tests_passed": ["make test"]}),
            messages=(*_ran("make test", "c1", exit_code=0), *_ran("make test", "c2", exit_code=1)),
            task_cwd=str(workspace),
        )

        assert selected == ()

    async def test_replay_exit_differing_from_the_recorded_exit_is_not_success(
        self, tmp_path: Path
    ) -> None:
        workspace = _workspace(tmp_path / "ws")
        parsed = replay_candidate("make test", str(workspace))
        assert parsed is not None
        candidate = ReplayCandidate(
            transcript_command=parsed.transcript_command,
            core_command=parsed.core_command,
            argv=parsed.argv,
            transcript_returncode=2,
        )

        runs = await replay_commands(
            (candidate,), workspace=str(workspace), env=dict(os.environ), timeout_seconds=30
        )

        assert runs[0].returncode == 0 and runs[0].transcript_returncode == 2
        assert not runs[0].succeeded
        assert not command_replay.replayed_command_supports_claim(
            "make test",
            (build_observation_message(WorkspaceObservation(frozenset(), command_runs=runs)),),
        )

    async def test_recorded_zero_exit_with_failing_replay_is_not_success(
        self, tmp_path: Path
    ) -> None:
        workspace = _workspace(tmp_path / "ws", correct=False)
        parsed = replay_candidate("make test", str(workspace))
        assert parsed is not None
        candidate = ReplayCandidate(
            transcript_command=parsed.transcript_command,
            core_command=parsed.core_command,
            argv=parsed.argv,
            transcript_returncode=0,
        )

        runs = await replay_commands(
            (candidate,), workspace=str(workspace), env=dict(os.environ), timeout_seconds=30
        )

        assert runs[0].returncode != 0 and not runs[0].succeeded
        message = build_observation_message(WorkspaceObservation(frozenset(), command_runs=runs))
        assert not command_replay.replayed_command_supports_claim("make test", (message,))

    async def test_shorter_command_does_not_back_a_longer_claim(self, tmp_path: Path) -> None:
        # The default target passes, ``make test`` fails, and the leaf only
        # ran ``make``.
        workspace = _workspace(tmp_path / "ws")
        (workspace / "Makefile").write_text("build:\n\ttrue\ntest:\n\tfalse\n", encoding="utf-8")

        verdict, observation = await _dispatch_and_verify(
            workspace, _ran("make", "c1"), _evidence("make test")
        )

        assert all(run.command != "make test" for run in observation.command_runs)
        assert verdict.passed is False

    async def test_unrelated_command_inside_a_claim_does_not_back_it(self, tmp_path: Path) -> None:
        # A claim that contains a command it did not run.
        workspace = _workspace(tmp_path / "ws")
        claim = "python -m pytest tests/test_bad.py passed, checked with ls"

        verdict, observation = await _dispatch_and_verify(
            workspace, _ran("ls", "c1"), _evidence("ls", [claim])
        )

        assert observation.command_runs == ()
        assert verdict.passed is False

    async def test_unlinked_passing_run_does_not_back_another_claim(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")

        verdict, observation = await _dispatch_and_verify(
            workspace,
            _ran("make test", "c1", exit_code=0),
            _evidence("make test", ["make test", "npm run e2e"]),
        )

        assert observation.command_runs[0].succeeded
        assert verdict.passed is False
        assert "npm run e2e" in verdict.reasons[0]

    def test_forged_observation_data_is_not_support(self, tmp_path: Path) -> None:
        forged = AgentMessage(
            type="harness_observation",
            content="",
            data={
                "_harness_workspace_observation": {
                    "command_runs": [{"command": "make test", "returncode": 0}]
                }
            },
        )
        assert not command_replay.replayed_command_supports_claim("make test", (forged,))


class TestClaimLinkage:
    @pytest.fixture(autouse=True)
    def _project(self, tmp_path: Path) -> None:
        self.workspace = _workspace(tmp_path / "ws")

    def _links(self, claim: str, command: str) -> bool:
        candidate = replay_candidate(command, str(self.workspace))
        assert candidate is not None
        return claim_links_to_command(
            claim,
            transcript_command=candidate.transcript_command,
            core_command=candidate.core_command,
            argv=candidate.argv,
            environment=tuple(candidate.env_delta),
        )

    @pytest.mark.parametrize(
        ("claim", "command"),
        [
            ("make test", "make test"),
            ("make   test", "make test"),
            ("make test (12 passed)", "make test"),
            ("pytest -q (3 passed in 0.1s)", "pytest -q | tail -5"),
            ("pytest -q | tail -5", "pytest -q | tail -5"),
            ("pytest -q", "pytest -q | tail -5"),
            ("migrations (578 tests)", "python tests/runtests.py migrations"),
            (DJANGO_WRITER_CLAIM, DJANGO_WRITER),
            (DJANGO_WRITER, DJANGO_WRITER),
            ("`tests/test_calc.py`", "pytest -q tests/test_calc.py"),
            ("tests/test_calc.py", "python -m pytest -q -p no:cacheprovider tests/test_calc.py"),
            ("tests/test_calc.py (4 tests)", "timeout 60 python -m pytest tests/test_calc.py"),
            # Configuration that does not change what is
            # collected or selected keeps the link.
            ("tests/test_calc.py", "pytest tests/test_calc.py"),
            ("tests/test_calc.py", "pytest -pno:cacheprovider tests/test_calc.py"),
            ("tests/test_calc.py", "pytest -o cache_dir=.x tests/test_calc.py"),
            ("tests/test_calc.py", "pytest --override-ini=cache_dir=.x tests/test_calc.py"),
            ("tests/test_calc.py", "FOO=1 pytest tests/test_calc.py"),
            ("tests/test_calc.py", "timeout 60 env FOO=1 pytest tests/test_calc.py"),
        ],
    )
    def test_linked(self, claim: str, command: str) -> None:
        assert self._links(claim, command)

    @pytest.mark.parametrize(
        ("claim", "command"),
        [
            ("make tests", "make test"),
            ("make test-all", "make test"),
            ("remake test", "make test"),
            ("migrations.test_writer (49 tests)", "python tests/runtests.py migrations"),
            ("migrations passed", "python tests/runtests.py migrations"),
            ("tests/test_other.py", "pytest -q tests/test_calc.py"),
            ("pytest", "pytest -q"),
            ("-q", "pytest -q"),
            ("", "make test"),
            # Containment is not linkage.
            ("Ran `make test`: 12 passed", "make test"),
            ("make test", "make"),
            ("python -m pytest tests/test_bad.py passed, checked with ls", "ls"),
            ("./run_tests.sh --integration", "./run_tests.sh"),
            ("cargo build && cargo test", "cargo build"),
            ("make test (12 passed) (and lint)", "make test"),
            # A target is linked only as an executed operand.
            ("tests/test_bad.py", "sed -n 1,40p tests/test_bad.py"),
            ("tests/test_a.py", "cat tests/test_a.py"),
            ("tests/test_a.py", "git log -- tests/test_a.py"),
            ("tests/test_a.py", "head -40 tests/test_a.py"),
            ("tests/test_bad.py", "python -m pytest -q --ignore tests/test_bad.py"),
            ("tests/test_bad.py", "python -m pytest -q --ignore=tests/test_bad.py tests"),
            ("tests/test_bad.py", "pytest --deselect tests/test_bad.py::test_x tests/test_bad.py"),
            ("tests/test_bad.py", 'pytest -k "not slow" tests/test_bad.py'),
            ("tests/test_bad.py", "pytest -m smoke tests/test_bad.py"),
            ("tests/test_bad.py", "pytest --rootdir tests/test_bad.py"),
            ("tests/test_bad.py", "pytest --some-plugin-option tests/test_bad.py"),
            ("tests/test_bad.py", "pytest --collect-only tests/test_bad.py"),
            ("tests/test_bad.py", "./run_tests.sh tests/test_bad.py"),
            ("tests/test_bad.py", "make tests/test_bad.py"),
            ("pytest", "python -m pytest -q tests/test_calc.py"),
            ("migrations", "python tests/runtests.py --exclude-tag slow migrations"),
            ("migrations", "python tests/runtests.py --start-after migrations.a migrations"),
            # Collection or selection changed through
            # configuration rather than argv exclusion.
            (
                "tests/test_bad.py",
                'pytest -o "addopts=--deselect tests/test_bad.py::t" tests/test_bad.py',
            ),
            ("tests/test_bad.py", "pytest -oaddopts=-q tests/test_bad.py"),
            ("tests/test_bad.py", "pytest --override-ini=addopts=-q tests/test_bad.py"),
            (
                "tests/test_bad.py",
                "pytest --override-ini python_functions=check_* tests/test_bad.py",
            ),
            ("tests/test_bad.py", "pytest -o python_files=x.py tests/test_bad.py"),
            ("tests/test_bad.py", "pytest -o python_classes=X tests/test_bad.py"),
            ("tests/test_bad.py", "pytest -o testpaths=x tests/test_bad.py"),
            ("tests/test_bad.py", "pytest -o norecursedirs=x tests/test_bad.py"),
            ("tests/test_bad.py", "pytest -o noequals tests/test_bad.py"),
            ("tests/test_bad.py", "pytest -c alt.ini tests/test_bad.py"),
            ("tests/test_bad.py", "pytest -calt.ini tests/test_bad.py"),
            ("tests/test_bad.py", "pytest --config-file=alt.ini tests/test_bad.py"),
            ("tests/test_bad.py", "pytest --config-file alt.ini tests/test_bad.py"),
            ("tests/test_bad.py", "pytest --rootdir=. tests/test_bad.py"),
            ("tests/test_bad.py", "pytest --confcutdir tests tests/test_bad.py"),
            ("tests/test_bad.py", "pytest -p myplugin tests/test_bad.py"),
            ("tests/test_bad.py", "pytest -pmyplugin tests/test_bad.py"),
            ("tests/test_bad.py", "pytest -p no:randomly tests/test_bad.py"),
            ("tests/test_bad.py", "pytest -kslow tests/test_bad.py"),
            ("tests/test_bad.py", "pytest --deselect=tests/test_bad.py::t tests/test_bad.py"),
            ("tests/test_bad.py", "PYTEST_ADDOPTS=-x pytest tests/test_bad.py"),
            ("tests/test_bad.py", "PYTEST_PLUGINS=myplugin pytest tests/test_bad.py"),
            ("tests/test_bad.py", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_bad.py"),
            ("tests/test_bad.py", "env PYTEST_ADDOPTS=-x pytest tests/test_bad.py"),
            ("tests/test_bad.py", "timeout 60 env PYTEST_ADDOPTS=-x pytest tests/test_bad.py"),
            ("tests/test_bad.py", "nice -n 5 env PYTEST_PLUGINS=p pytest tests/test_bad.py"),
            ("tests/test_bad.py", "pytest tests/test_bad.py -- -k slow"),
            # An unknown option never swallows an excluding option as its value.
            ("tests/test_bad.py", "pytest --unknown --ignore tests/test_bad.py tests/test_bad.py"),
            # The same principle for unittest and Django-style runners.
            ("tests.test_x", "python -m unittest -k slow tests.test_x"),
            ("tests", "python -m unittest discover -p test_a*.py tests"),
            ("migrations", "python tests/runtests.py --tag slow migrations"),
            ("migrations", "python tests/runtests.py -k writer migrations"),
            ("migrations", "python tests/runtests.py --exclude-tag=slow migrations"),
            # A workspace file named like a runner is a plain script.
            ("tests/test_bad.py", "./pytest tests/test_bad.py"),
        ],
    )
    def test_not_linked(self, claim: str, command: str) -> None:
        assert not self._links(claim, command)


class TestDevRunStrings:
    async def test_django_writer_label_claim_is_corroborated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", os.pathsep.join([PYTHON_BIN, "/usr/bin", "/bin"]))
        workspace = _workspace(tmp_path / "ws")
        evidence = _evidence(DJANGO_WRITER, [DJANGO_WRITER_CLAIM])

        verdict, observation = await _dispatch_and_verify(
            workspace, _ran(f"{DJANGO_WRITER} 2>&1 | tail -5", "c1"), evidence
        )

        assert verdict.passed is True, verdict.reasons
        assert [run.command for run in observation.command_runs] == [DJANGO_WRITER]

    async def test_django_writer_label_claim_fails_when_the_runner_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", os.pathsep.join([PYTHON_BIN, "/usr/bin", "/bin"]))
        workspace = _workspace(tmp_path / "ws", correct=False)
        evidence = _evidence(DJANGO_WRITER, [DJANGO_WRITER_CLAIM])

        verdict, _ = await _dispatch_and_verify(
            workspace, _ran(f"{DJANGO_WRITER} 2>&1 | tail -5", "c1"), evidence
        )

        assert verdict.passed is False


class TestIsolationRecord:
    async def test_no_replay_without_network_isolation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _workspace(tmp_path / "ws")
        marker = workspace / "ran"
        _executable(workspace / "run_tests.sh", "#!/bin/sh\ntouch ran\n")
        candidate = replay_candidate("./run_tests.sh", str(workspace))
        assert candidate is not None
        monkeypatch.setattr(command_replay, "network_isolation_prefix", lambda: None)

        runs = await replay_commands(
            (candidate,), workspace=str(workspace), env=dict(os.environ), timeout_seconds=30
        )

        assert runs == ()
        assert command_replay.replay_unavailable_reason() == REPLAY_SKIPPED_NETWORK
        assert not marker.exists()

    async def test_skip_is_recorded_and_claims_keep_transcript_rules(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _workspace(tmp_path / "ws")
        monkeypatch.setattr(command_replay, "network_isolation_prefix", lambda: None)

        verdict, observation = await _dispatch_and_verify(
            workspace, _ran("make test", "c1"), _evidence("make test")
        )

        assert observation.command_runs == ()
        assert observation.replay_skipped == "network_isolation_unavailable"
        message = build_observation_message(observation)
        assert "replay skipped: network_isolation_unavailable" in message.content
        # "make test" passes here, but only a replay could have backed it.
        assert verdict.passed is False

    @pytest.mark.parametrize(
        ("interfaces", "offline"),
        [
            ([(1, "lo")], True),
            ([(1, "lo"), (2, "eth0")], False),
            ([], False),
        ],
    )
    def test_linux_process_with_only_loopback_counts_as_isolated(
        self,
        monkeypatch: pytest.MonkeyPatch,
        real_replay_isolation: None,
        interfaces: list[tuple[int, str]],
        offline: bool,
    ) -> None:
        monkeypatch.setattr(command_replay.socket, "if_nameindex", lambda: interfaces)
        monkeypatch.setattr(command_replay.sys, "platform", "linux")
        monkeypatch.setattr(command_replay.shutil, "which", lambda _name: None)

        probe = command_replay.network_isolation_prefix.__wrapped__  # type: ignore[attr-defined]

        assert command_replay._process_has_only_loopback() is offline
        assert probe() == (() if offline else None)

    @pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS only")
    async def test_macos_replay_has_no_network(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_replay_isolation: None
    ) -> None:
        if command_replay.network_isolation_prefix() is None:
            pytest.skip("sandbox-exec unavailable in this process")
        monkeypatch.setenv("PATH", os.pathsep.join([PYTHON_BIN, "/usr/bin", "/bin"]))
        workspace = _workspace(tmp_path / "ws")
        probe = (
            "import socket, sys\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
            "except OSError:\n"
            "    sys.exit(0)\n"
            "sys.exit(1)\n"
        )
        (workspace / "probe.py").write_text(probe, encoding="utf-8")
        candidate = replay_candidate("python probe.py", str(workspace))
        assert candidate is not None

        runs = await replay_commands(
            (candidate,), workspace=str(workspace), env=dict(os.environ), timeout_seconds=30
        )

        assert runs[0].network_isolated and runs[0].succeeded

    def test_default_observation_fields_keep_old_constructors_valid(self) -> None:
        run = CommandObservation(command="pytest", returncode=0, output_tail="1 passed")
        assert run.succeeded and not run.mutated and run.argv == ()


PYTEST_WORKSPACE_TESTS = {
    "test_good.py": "def test_good():\n    assert True\n",
    "test_bad.py": "def test_bad():\n    assert False\n",
}


class TestTargetLinkageEndToEnd:
    """A command that names a target without executing it backs nothing."""

    @pytest.fixture(autouse=True)
    def _path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", os.pathsep.join([PYTHON_BIN, "/usr/bin", "/bin"]))

    def _pytest_workspace(self, root: Path) -> Path:
        workspace = _workspace(root)
        for name, body in PYTEST_WORKSPACE_TESTS.items():
            (workspace / "tests" / name).write_text(body, encoding="utf-8")
        return workspace

    @pytest.mark.parametrize(
        "command",
        [
            "sed -n 1,40p tests/test_bad.py",
            "cat tests/test_bad.py",
            "git log -- tests/test_bad.py",
            "python -m pytest -q -p no:cacheprovider --ignore tests/test_bad.py",
            "python -m pytest -q -p no:cacheprovider --deselect tests/test_bad.py::test_bad "
            "tests/test_bad.py",
        ],
    )
    async def test_non_executing_or_excluding_command_does_not_back_the_target(
        self, tmp_path: Path, command: str
    ) -> None:
        workspace = self._pytest_workspace(tmp_path / "ws")
        transcript = (
            *_edit(workspace / "tests" / "test_bad.py", "e2"),
            *_ran(command, "c1", exit_code=0),
        )

        verdict, observation = await _dispatch_and_verify(
            workspace, transcript, _evidence(command, ["tests/test_bad.py"])
        )

        assert verdict.passed is False
        assert all(run.command == command for run in observation.command_runs)

    async def test_executing_runner_backs_its_passing_target(self, tmp_path: Path) -> None:
        workspace = self._pytest_workspace(tmp_path / "ws")
        command = "python -m pytest -q -p no:cacheprovider tests/test_good.py"
        transcript = (
            *_edit(workspace / "tests" / "test_good.py", "e2"),
            *_ran(command, "c1"),
        )

        verdict, observation = await _dispatch_and_verify(
            workspace, transcript, _evidence(command, ["tests/test_good.py"])
        )

        assert verdict.passed is True, verdict.reasons
        assert observation.command_runs[0].succeeded

    def test_viewer_set_is_explicit(self) -> None:
        assert {"cat", "sed", "head", "tail", "less", "grep", "rg", "awk", "wc", "ls"} <= (
            VIEWER_PROGRAMS
        )
        assert {"find", "stat", "file", "diff", "git"} <= VIEWER_PROGRAMS
        for viewer in ("sed -n 1,40p tests/x.py", "cat tests/x.py", "git show HEAD:tests/x.py"):
            assert claim_target_operands(viewer.split()) == frozenset()


class TestAllowlist:
    """Only allowlisted programs are replayed."""

    @pytest.fixture(autouse=True)
    def _project(self, tmp_path: Path) -> None:
        self.workspace = _workspace(tmp_path / "ws")
        (self.workspace / "bin").mkdir()
        _executable(self.workspace / "bin" / "test", "#!/bin/sh\n")
        _executable(self.workspace / "gradlew", "#!/bin/sh\n")
        (self.workspace / "manage.py").write_text("", encoding="utf-8")

    def _allowed(self, command: str) -> bool:
        candidate = replay_candidate(command, str(self.workspace))
        return candidate is not None and command_replay.replay_admissible(
            candidate, str(self.workspace)
        )

    @pytest.mark.parametrize(
        "command",
        [
            "make test",
            "make",
            "make -j4 check",
            "npm test",
            "npm run test",
            "pnpm test",
            "yarn test",
            "bun test",
            "./run_tests.sh",
            "./run_tests.sh --integration",
            "bash run_tests.sh",
            "tests/runtests.py migrations",
            "python tests/runtests.py migrations",
            "python -m pytest -q",
            "python3 -u -m pytest -q",
            "python -m unittest -v",
            "python -m django test app",
            "python manage.py test app",
            "pytest -q tests/test_calc.py",
            "tox -e py311",
            "nox -s tests",
            "go test ./...",
            "cargo test",
            "cargo +nightly test",
            "mvn -q test",
            "./gradlew test",
            "gradle check",
            "dotnet test",
            "rspec",
            "bundle exec rspec",
            "uv run pytest -q",
            "uv run --with pytest-xdist python -m pytest -q",
            "poetry run pytest",
            "npx jest",
            "bin/test",
            "timeout 60 make test",
            "timeout -s TERM 5 make test",
            "stdbuf -o L make test",
            "stdbuf -oL make test",
            "time -o /dev/null make test",
            "nice -n 5 pytest",
            "FOO=1 make test",
        ],
    )
    def test_admitted(self, command: str) -> None:
        assert self._allowed(command), command

    @pytest.mark.parametrize(
        "command",
        [
            # Wrapper options that take a separate value.
            "timeout -s TERM 5 rm -rf x",
            "stdbuf -o L rm -rf x",
            "time -o /dev/null rm -rf x",
            "time -f %e rm -rf x",
            "env -u X rm x",
            "timeout -s TERM make test --unknown-wrapper-form",
            "timeout --bogus 5 make test",
            "nice -Z make test",
            "xargs pytest",
            "watch make test",
            # Not on the allowlist, or an install/deploy/publish form.
            "make install",
            "make install-deps",
            "make deploy",
            "make -n test",
            "make -C .. test",
            "npm install",
            "npm run deploy",
            "npm exec -- rimraf x",
            "npx rimraf x",
            "uv run pip install x",
            "uv run --directory /tmp pytest",
            "poetry run pip install x",
            "pipenv install x",
            "pdm add x",
            "bun install",
            "pip3.11 install x",
            "mvn install",
            "gradle publish",
            "cargo install x",
            "go get x",
            "find . -delete",
            "busybox rm x",
            "truncate -s0 calc.py",
            "mv calc.py /tmp/x",
            "chmod -R 000 .",
            "twine upload dist/a.whl",
            "gh repo delete x --yes",
            "git diff --stat",
            "ls",
            "sed -n 1,40p calc.py",
            "python3 -c 'print(1)'",
            "python",
            "python tests/absent.py",
            "sh",
            # ``env`` is peeled into an environment delta before the argv, so
            # an ``env`` option is left as the program.
            "env -u HOME make test",
            "pytest --collect-only",
            "pytest --basetemp=/tmp/elsewhere",
            "/tmp/evil/python3 -m pytest",
            "/bin/sh run_tests.sh",
            "./absent.sh",
        ],
    )
    def test_refused(self, command: str) -> None:
        assert not self._allowed(command), command

    def test_denylist_refuses_what_the_allowlist_admits(self) -> None:
        # A workspace script is admitted by the allowlist (rule b); the
        # denylist, a second layer, still refuses a pip install through it.
        (self.workspace / "tools").mkdir()
        _executable(self.workspace / "tools" / "pip", "#!/bin/sh\n")
        argv = ("tools/pip", "install", "x")
        candidate = replay_candidate(" ".join(argv), str(self.workspace))
        assert candidate is not None

        assert replay_allowed(argv, workspace=str(self.workspace))
        assert replay_denied(argv)
        assert not command_replay.replay_admissible(candidate, str(self.workspace))

    def test_launcher_hidden_installer_is_refused(self) -> None:
        # The allowlist and the denylist judge the same resolved programs, so
        # a launcher cannot hide an installer from the denylist.
        bin_dir = self.workspace / ".venv" / "bin"
        bin_dir.mkdir(parents=True)
        _executable(bin_dir / "pip", "#!/bin/sh\n")
        _executable(bin_dir / "pytest", "#!/bin/sh\n")
        ws = str(self.workspace)
        for command in (
            "uv run .venv/bin/pip install local-package.whl",
            ".venv/bin/pip install x",
            "uv run pip install x",
            "uv run python -m pip install x",
            "timeout 60 uv run env X=1 .venv/bin/pip install x",
            "poetry run pip install x",
        ):
            argv = tuple(command.split())
            assert replay_denied(argv), command
            assert not replay_allowed(argv, workspace=ws), command
            assert not self._allowed(command), command
        # An installed program that is not an allowlisted runner is not a
        # workspace script, whatever its arguments.
        _executable(bin_dir / "coverage", "#!/bin/sh\n")
        assert resolve_replay_program((".venv/bin/coverage", "run"), workspace=ws) is None
        assert not self._allowed("uv run .venv/bin/coverage run -m pytest")
        # Launcher-wrapped test runs stay admitted.
        for command in (
            "uv run .venv/bin/pytest -q",
            ".venv/bin/pytest -q",
            "uv run pytest -q",
            "poetry run pytest -q",
            "uv run python -m pytest -q",
        ):
            assert not replay_denied(tuple(command.split())), command
            assert self._allowed(command), command

    def test_allowlist_and_denylist_share_one_resolution(self) -> None:
        bin_dir = self.workspace / ".venv" / "bin"
        bin_dir.mkdir(parents=True)
        _executable(bin_dir / "pytest", "#!/bin/sh\n")
        argv = ("timeout", "60", "uv", "run", "env", "X=1", ".venv/bin/pytest", "-q")
        assert program_chain(argv) == (
            ("uv", "run", "env", "X=1", ".venv/bin/pytest", "-q"),
            (".venv/bin/pytest", "-q"),
        )
        runner = resolve_replay_program(argv, workspace=str(self.workspace))
        assert runner == ResolvedRunner("pytest", ("-q",), programs=program_chain(argv) or ())
        assert command_line_assignments(argv) == ("X=1",)

    def _conda_bin(self, root: Path) -> Path:
        """A SWE-bench-style conda environment with a python and a pytest."""
        bin_dir = root / "miniconda3" / "envs" / "testbed" / "bin"
        bin_dir.mkdir(parents=True)
        _executable(bin_dir / "python3.9", "#!/bin/sh\n")
        (bin_dir / "python").symlink_to("python3.9")
        _executable(bin_dir / "pytest", "#!/bin/sh\n")
        return bin_dir

    def test_conda_style_interpreter_on_path_is_admitted(self, tmp_path: Path) -> None:
        bin_dir = self._conda_bin(tmp_path / "opt")
        environment = {"PATH": f"{bin_dir}:/usr/bin:/bin"}
        for argv in (
            (str(bin_dir / "python"), "-m", "pytest", "-q"),
            (str(bin_dir / "python3.9"), "tests/runtests.py", "migrations"),
            (str(bin_dir / "pytest"), "-q"),
        ):
            assert replay_allowed(argv, workspace=str(self.workspace), environment=environment)
        # The environment roots also come from CONDA_PREFIX.
        prefix = bin_dir.parent
        assert replay_allowed(
            (str(bin_dir / "python"), "-m", "pytest"),
            workspace=str(self.workspace),
            environment={"PATH": "/usr/bin:/bin", "CONDA_PREFIX": str(prefix)},
        )

    def test_verifying_interpreter_is_admitted(self) -> None:
        assert replay_allowed(
            (sys.executable, "-m", "pytest"),
            workspace=str(self.workspace),
            environment={"PATH": "/usr/bin:/bin"},
        )

    def test_interpreter_outside_the_environment_roots_is_refused(self, tmp_path: Path) -> None:
        bin_dir = self._conda_bin(tmp_path / "opt")
        evil = tmp_path / "evil"
        evil.mkdir()
        _executable(evil / "python3", "#!/bin/sh\n")
        environment = {"PATH": f"{bin_dir}:/usr/bin:/bin"}
        for argv in (
            (str(evil / "python3"), "-m", "pytest"),
            ("/tmp/evil/python3", "-m", "pytest"),
            # Not an interpreter or runner, although it is on PATH.
            (str(bin_dir / "pip"), "install", "x"),
        ):
            assert not replay_allowed(
                argv, workspace=str(self.workspace), environment=environment
            ), argv

    def test_symlink_resolving_outside_the_roots_is_refused(self, tmp_path: Path) -> None:
        bin_dir = self._conda_bin(tmp_path / "opt")
        evil = tmp_path / "evil"
        evil.mkdir()
        _executable(evil / "python3", "#!/bin/sh\n")
        (bin_dir / "python3").symlink_to(evil / "python3")

        assert not replay_allowed(
            (str(bin_dir / "python3"), "-m", "pytest"),
            workspace=str(self.workspace),
            environment={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        )

    def test_environment_assignment_outside_the_roots_is_refused(self, tmp_path: Path) -> None:
        for command, admitted in (
            ("PYTHONPATH=. make test", True),
            (f"PATH={tmp_path / 'evil'} make test", False),
        ):
            candidate = replay_candidate(command, str(self.workspace))
            assert candidate is not None
            assert (
                command_replay.replay_admissible(
                    candidate, str(self.workspace), {"PATH": "/usr/bin:/bin"}
                )
                is admitted
            ), command

    def test_absolute_workspace_program_is_admitted(self) -> None:
        script = self.workspace.resolve() / "run_tests.sh"
        assert replay_allowed((str(script),), workspace=str(self.workspace))

    async def test_refused_command_is_never_run(self, tmp_path: Path) -> None:
        victim = tmp_path / "victim.txt"
        victim.write_text("keep", encoding="utf-8")
        command = f"timeout -s TERM 5 rm -f {victim}"
        candidate = replay_candidate(command, str(self.workspace))
        assert candidate is not None

        runs = await replay_commands(
            (candidate,), workspace=str(self.workspace), env=dict(os.environ), timeout_seconds=30
        )

        assert runs == ()
        assert victim.read_text(encoding="utf-8") == "keep"


class TestLinkedLiveTrees:
    """Live trees the copy links to are protected."""

    async def _run(self, workspace: Path, script: str) -> CommandObservation:
        _executable(workspace / "run_tests.sh", script)
        candidate = replay_candidate("./run_tests.sh", str(workspace))
        assert candidate is not None
        runs = await replay_commands(
            (candidate,), workspace=str(workspace), env=dict(os.environ), timeout_seconds=30
        )
        assert len(runs) == 1
        return runs[0]

    def _venv(self, workspace: Path) -> Path:
        venv = workspace / ".venv"
        (venv / "lib").mkdir(parents=True)
        (venv / "marker.txt").write_text("live", encoding="utf-8")
        return venv

    async def test_deleting_a_file_under_a_linked_directory_is_detected(
        self, tmp_path: Path
    ) -> None:
        workspace = _workspace(tmp_path / "ws")
        self._venv(workspace)

        run = await self._run(workspace, "#!/bin/sh\nfind .venv/ -name marker.txt -delete\n")

        assert run.returncode == 0 and run.mutated and not run.succeeded

    async def test_creating_a_file_under_a_linked_directory_is_detected(
        self, tmp_path: Path
    ) -> None:
        workspace = _workspace(tmp_path / "ws")
        venv = self._venv(workspace)

        run = await self._run(workspace, "#!/bin/sh\ntouch .venv/new_from_replay\n")

        assert run.mutated and not run.succeeded
        assert (venv / "new_from_replay").exists()

    async def test_writing_through_an_absolute_symlink_is_detected(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")
        outside = tmp_path / "shared"
        outside.mkdir()
        (outside / "data.txt").write_text("live", encoding="utf-8")
        (workspace / "shared").symlink_to(outside)

        run = await self._run(workspace, "#!/bin/sh\necho x >> shared/data.txt\n")

        assert run.mutated and not run.succeeded

    async def test_reading_linked_trees_is_not_mutation(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")
        venv = self._venv(workspace)
        (venv / "lib" / "livemod.py").write_text("VALUE = 5\n", encoding="utf-8")
        script = (
            "#!/bin/sh\n"
            f'{sys.executable} -c \'import sys; sys.path.insert(0, ".venv/lib"); '
            "import livemod; assert livemod.VALUE == 5'\n"
            "cat .venv/marker.txt\n"
        )

        run = await self._run(workspace, script)

        assert run.succeeded, run.output_tail
        # PYTHONDONTWRITEBYTECODE keeps imports from writing into the live tree.
        assert not (venv / "lib" / "__pycache__").exists()

    async def test_linked_tree_over_the_fingerprint_budget_is_not_replayed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _workspace(tmp_path / "ws")
        venv = self._venv(workspace)
        for index in range(5):
            (venv / f"f{index}").write_text("x", encoding="utf-8")
        monkeypatch.setattr(command_replay, "MAX_PROTECTED_LINK_ENTRIES", 3)
        candidate = replay_candidate("./run_tests.sh", str(workspace))
        assert candidate is not None

        runs = await replay_commands(
            (candidate,), workspace=str(workspace), env=dict(os.environ), timeout_seconds=30
        )

        assert runs == ()

    @pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS only")
    async def test_macos_sandbox_denies_writes_to_linked_trees_and_the_workspace(
        self, tmp_path: Path, real_replay_isolation: None
    ) -> None:
        if command_replay.network_isolation_prefix() is None:
            pytest.skip("sandbox-exec unavailable in this process")
        workspace = _workspace(tmp_path / "ws")
        venv = self._venv(workspace)
        live_calc = workspace.resolve() / "calc.py"
        original = live_calc.read_bytes()

        run = await self._run(
            workspace,
            "#!/bin/sh\n"
            "find .venv/ -name marker.txt -delete\n"
            "touch .venv/new_from_replay\n"
            f"echo '# live' >> {live_calc}\n"
            "echo copy-ok > copy_write.txt\n"
            "exit 0\n",
        )

        assert (venv / "marker.txt").read_text(encoding="utf-8") == "live"
        assert not (venv / "new_from_replay").exists()
        assert live_calc.read_bytes() == original
        assert run.network_isolated
        # Writes inside the copy are not blocked, and the denied writes left
        # nothing changed, so the run itself succeeds.
        assert run.succeeded, run.output_tail


def _transcript_only_verdict(
    workspace: Path, transcript: tuple[AgentMessage, ...], evidence: dict[str, object]
) -> VerifierVerdict:
    """The transcript verifier alone: a harness observation with no replayed runs."""
    final = _final(evidence)
    messages = [*_edit(workspace / "calc.py", "edit"), *transcript, final]
    insert_observation_message(messages, WorkspaceObservation(changed_paths=frozenset({"calc.py"})))
    return _verify_atomic_evidence_against_runtime_messages(
        messages=tuple(messages),
        typed_evidence=EvidenceRecord(data=evidence),
        ac_content=AC,
        execution_profile=load_profile("code"),
        task_cwd=str(workspace),
        adapter_working_directory=str(workspace),
        verify_gate_active=True,
    )


class TestTranscriptOnlyHoles:
    """False accepts that predate replay, closed on the transcript-only path."""

    def _pytest_run(self, command: str) -> tuple[AgentMessage, ...]:
        return _ran(command, "c1", exit_code=0)[:1] + (
            _bash_result("c1", exit_code=0, output="1 passed in 0.01s"),
        )

    def test_excluding_run_does_not_back_the_excluded_file(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")
        (workspace / "tests" / "test_bad.py").write_text("def test_bad():\n    assert False\n")
        command = "python -m pytest -q --ignore tests/test_bad.py"
        transcript = (*_edit(workspace / "tests" / "test_bad.py", "e2"), *self._pytest_run(command))

        verdict = _transcript_only_verdict(
            workspace, transcript, _evidence(command, ["tests/test_bad.py"])
        )

        assert verdict.passed is False

    def test_executing_run_still_backs_its_file(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")
        (workspace / "tests" / "test_good.py").write_text("def test_good():\n    assert True\n")
        command = "python -m pytest -q tests/test_good.py"
        transcript = (
            *_edit(workspace / "tests" / "test_good.py", "e2"),
            *self._pytest_run(command),
        )

        verdict = _transcript_only_verdict(
            workspace, transcript, _evidence(command, ["tests/test_good.py"])
        )

        assert verdict.passed is True, verdict.reasons

    def test_pipeline_exit_does_not_back_its_core_script(self, tmp_path: Path) -> None:
        # The script fails; the pipeline's recorded exit is tail's.
        workspace = _workspace(tmp_path / "ws", correct=False)

        verdict = _transcript_only_verdict(
            workspace,
            _ran("./run_tests.sh | tail -5", "c1", exit_code=0),
            _evidence("./run_tests.sh"),
        )

        assert verdict.passed is False

    def test_direct_script_run_still_backs_its_claim(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")

        verdict = _transcript_only_verdict(
            workspace, _ran("./run_tests.sh", "c1", exit_code=0), _evidence("./run_tests.sh")
        )

        assert verdict.passed is True, verdict.reasons


# ``test_add`` fails and ``test_other`` passes; ``alt.ini`` deselects the failure.
NARROWING_TESTS = (
    "from calc import add\n\n"
    "def test_add():\n    assert add(2, 3) == 5\n\n"
    "def test_other():\n    assert True\n"
)
DESELECT_ADD = "tests/test_bad.py::test_add"


class TestConfigurationNarrowing:
    """A file claim is not backed by a run whose collection or
    selection was changed through configuration instead of argv."""

    @pytest.fixture(autouse=True)
    def _path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", os.pathsep.join([PYTHON_BIN, "/usr/bin", "/bin"]))

    def _narrowing_workspace(self, root: Path) -> Path:
        workspace = _workspace(root, correct=False)
        (workspace / "tests" / "test_bad.py").write_text(NARROWING_TESTS, encoding="utf-8")
        (workspace / "tests" / "test_good.py").write_text(
            PYTEST_WORKSPACE_TESTS["test_good.py"], encoding="utf-8"
        )
        (workspace / "conftest.py").write_text(
            "import os, sys\nsys.path.insert(0, os.path.dirname(__file__))\n", encoding="utf-8"
        )
        (workspace / "alt.ini").write_text(
            f"[pytest]\naddopts = --deselect {DESELECT_ADD}\n", encoding="utf-8"
        )
        return workspace

    @pytest.mark.parametrize(
        "command",
        [
            # ``-o addopts``, ``PYTEST_ADDOPTS`` and ``-c`` forms.
            f'python -m pytest -q -p no:cacheprovider -o "addopts=--deselect {DESELECT_ADD}" '
            "tests/test_bad.py",
            f"PYTEST_ADDOPTS=--deselect={DESELECT_ADD} python -m pytest -q -p no:cacheprovider "
            "tests/test_bad.py",
            "python -m pytest -q -p no:cacheprovider -c alt.ini tests/test_bad.py",
            # The assignment consumed by an ``env`` wrapper, not by the parser.
            f"nice -n 5 env PYTEST_ADDOPTS=--deselect={DESELECT_ADD} python -m pytest -q "
            "-p no:cacheprovider tests/test_bad.py",
        ],
    )
    async def test_narrowed_run_does_not_back_the_file(self, tmp_path: Path, command: str) -> None:
        workspace = self._narrowing_workspace(tmp_path / "ws")
        transcript = (
            *_edit(workspace / "tests" / "test_bad.py", "e2"),
            *_ran(command, "c1", exit_code=0),
        )

        verdict, observation = await _dispatch_and_verify(
            workspace, transcript, _evidence(command, ["tests/test_bad.py"])
        )

        # A narrowed run that was replayed passed; it only backs nothing.
        assert all(run.succeeded for run in observation.command_runs)
        assert verdict.passed is False

    async def test_plain_pytest_run_still_backs_its_file(self, tmp_path: Path) -> None:
        workspace = self._narrowing_workspace(tmp_path / "ws")
        command = "python -m pytest -q tests/test_good.py"
        transcript = (
            *_edit(workspace / "tests" / "test_good.py", "e2"),
            *_ran(command, "c1", exit_code=0),
        )

        verdict, observation = await _dispatch_and_verify(
            workspace, transcript, _evidence(command, ["tests/test_good.py"])
        )

        assert observation.command_runs[0].succeeded
        assert verdict.passed is True, verdict.reasons

    @pytest.mark.parametrize(
        "command",
        [
            f"PYTEST_ADDOPTS=--deselect={DESELECT_ADD} python -m pytest -q tests/test_bad.py",
            f'python -m pytest -q -o "addopts=--deselect {DESELECT_ADD}" tests/test_bad.py',
            f"export PYTEST_ADDOPTS=--deselect={DESELECT_ADD} && python -m pytest -q "
            "tests/test_bad.py",
        ],
    )
    def test_transcript_only_narrowed_run_does_not_back_the_file(
        self, tmp_path: Path, command: str
    ) -> None:
        workspace = self._narrowing_workspace(tmp_path / "ws")
        transcript = (
            *_edit(workspace / "tests" / "test_bad.py", "e2"),
            *_ran(command, "c1", exit_code=0)[:1],
            _bash_result("c1", exit_code=0, output="1 passed, 1 deselected in 0.01s"),
        )

        verdict = _transcript_only_verdict(
            workspace, transcript, _evidence(command, ["tests/test_bad.py"])
        )

        assert verdict.passed is False

    async def test_replay_scrubs_inherited_pytest_configuration(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")
        _executable(
            workspace / "check_env.sh",
            "#!/bin/sh\n"
            'test -z "${PYTEST_ADDOPTS+x}${PYTEST_PLUGINS+x}'
            '${PYTEST_DISABLE_PLUGIN_AUTOLOAD+x}" || exit 7\n',
        )
        candidate = replay_candidate("./check_env.sh", str(workspace))
        assert candidate is not None
        inherited = {
            "PATH": "/usr/bin:/bin",
            "PYTEST_ADDOPTS": f"--deselect={DESELECT_ADD}",
            "PYTEST_PLUGINS": "evil",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }

        runs = await replay_commands(
            (candidate,), workspace=str(workspace), env=inherited, timeout_seconds=30
        )

        assert runs[0].returncode == 0, runs[0].output_tail
        assert runs[0].scrubbed_environment == (
            "PYTEST_ADDOPTS",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
            "PYTEST_PLUGINS",
        )

    async def test_command_line_assignment_is_kept_and_recorded(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")
        _executable(workspace / "check_env.sh", '#!/bin/sh\ntest "$PYTEST_PLUGINS" = mine\n')
        candidate = replay_candidate("PYTEST_PLUGINS=mine ./check_env.sh", str(workspace))
        assert candidate is not None

        runs = await replay_commands(
            (candidate,),
            workspace=str(workspace),
            env={"PATH": "/usr/bin:/bin"},
            timeout_seconds=30,
        )

        # Replayed as written; the assignment disables target linkage instead.
        assert runs[0].succeeded
        assert runs[0].env_delta == (("PYTEST_PLUGINS", "mine"),)

    def test_uv_env_file_is_refused(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")
        assert not replay_allowed(
            ("uv", "run", "--env-file", ".env", "pytest"), workspace=str(workspace)
        )


class TestAssignmentsScriptsAndPipefail:
    """Env-wrapper assignments, runner-named workspace files, pipefail
    clusters and the bare-word rule."""

    def test_env_wrapper_assignment_outside_the_roots_is_refused(self, tmp_path: Path) -> None:
        # Assignments consumed by an ``env`` wrapper get the same check
        # as leading ones.
        workspace = _workspace(tmp_path / "ws")
        evil = tmp_path / "evil"
        for command, admitted in (
            ("timeout 60 env FOO=1 make test", True),
            (f"timeout 60 env PATH={evil} make test", False),
            (f"nice env PYTHONPATH={evil} make test", False),
            (f"uv run env PATH={evil} pytest", False),
        ):
            candidate = replay_candidate(command, str(workspace))
            assert candidate is not None
            assert (
                command_replay.replay_admissible(
                    candidate, str(workspace), {"PATH": "/usr/bin:/bin"}
                )
                is admitted
            ), command

    def test_workspace_file_named_like_a_runner_is_a_plain_script(self, tmp_path: Path) -> None:
        # ``./pytest`` is replayed as a script, never as pytest, and a
        # workspace ``./python`` is not an interpreter.
        workspace = _workspace(tmp_path / "ws")
        _executable(workspace / "pytest", "#!/bin/sh\nexit 0\n")
        _executable(workspace / "python", "#!/bin/sh\nexit 0\n")
        (workspace / ".venv" / "bin").mkdir(parents=True)
        _executable(workspace / ".venv" / "bin" / "pytest", "#!/bin/sh\nexit 0\n")
        ws = str(workspace)

        script = resolve_replay_program(("./pytest", "tests/x.py"), workspace=ws)
        assert script is not None and (script.kind, script.arguments) == ("script", ("tests/x.py",))
        assert resolve_replay_program(("./python", "-m", "pytest"), workspace=ws) is None
        runner = resolve_replay_program((".venv/bin/pytest", "tests/x.py"), workspace=ws)
        assert runner is not None and (runner.kind, runner.arguments) == ("pytest", ("tests/x.py",))
        assert claim_target_operands(("./pytest", "tests/x.py")) == frozenset()
        assert claim_target_operands((".venv/bin/pytest", "tests/x.py")) == {"tests/x.py"}
        assert claim_target_operands(("node_modules/.bin/jest", "a.test.js")) == {"a.test.js"}

    @pytest.mark.parametrize(
        ("command", "protected"),
        [
            ("set -o pipefail && pytest -q | tail -5", True),
            ("set -euo pipefail; pytest -q | tail -5", True),
            ("set -e -o pipefail && pytest -q | tail -5", True),
            ("set -eu -o nounset -o pipefail && pytest -q | tail -5", True),
            ("set -eu; pytest -q | tail -5", False),
            ("set -xo pipefail; pytest -q | tail -5", False),
            ("set +o pipefail; pytest -q | tail -5", False),
            ("pytest -q | tail -5", False),
        ],
    )
    def test_set_option_clusters_enable_pipefail(self, command: str, protected: bool) -> None:
        assert _output_filter_pipeline_is_pipefail_protected(command) is protected

    @pytest.mark.parametrize(
        ("claim", "command", "covered"),
        [
            ("test_add", "pytest tests/test_address.py", False),
            ("test_add", "pytest tests/test_calc.py::test_add", True),
            ("unit", "make unit-tests", False),
            ("pytest", "python -m pytest -q", True),
        ],
    )
    def test_bare_word_claim_links_only_as_a_whole_word(
        self, tmp_path: Path, claim: str, command: str, covered: bool
    ) -> None:
        # The bare-word rule of the transcript path.
        assert (
            _test_command_targets_claim(
                command=command,
                claim=claim,
                chunk_test_proof_text="1 passed in 0.01s",
                messages=(),
                task_cwd=str(tmp_path),
            )
            is covered
        )
