"""Unittest-style dotted test labels are corroborated by re-executed runner labels.

A worker that verified a Django fix with ``python tests/runtests.py
migrations.test_writer`` reports ``tests_passed`` as the label it ran
(``migrations.test_writer (49 tests)``) or a test under it
(``migrations.test_writer.WriterTests.test_serialize_fields``). The harness
re-executes the runner itself; a label claim is corroborated only when a
re-executed unittest-style runner exited 0, its output proves tests ran and
passed, and one of its labels equals the claimed label or is a dotted-prefix
ancestor of it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.harness_observation import (
    CommandObservation,
    WorkspaceObservation,
    build_observation_message,
    insert_observation_message,
)
from ouroboros.orchestrator.evidence.test_detection import (
    _dotted_test_label_claim,
    _runtime_messages_support_test_claim,
    _unittest_style_runner_labels,
)
from ouroboros.orchestrator.evidence.verification import (
    _verify_atomic_evidence_against_runtime_messages,
)
from ouroboros.orchestrator.evidence_schema import EvidenceRecord
from ouroboros.orchestrator.profile_loader import load_profile

# Output tails recorded from re-executing the runner in the django__django-14580
# instance image (Django's runner prints its summary through unittest).
DJANGO_WRITER_OUTPUT = (
    "Testing against Django installed in '/testbed/django' with up to 11 processes\n"
    "Found 49 test(s).\n"
    "System check identified no issues (0 silenced).\n"
    ".................................................\n"
    "----------------------------------------------------------------------\n"
    "Ran 49 tests in 0.243s\n\nOK\n"
)
DJANGO_MIGRATIONS_OUTPUT = (
    "System check identified no issues (0 silenced).\n"
    "----------------------------------------------------------------------\n"
    "Ran 579 tests in 6.083s\n\nOK (skipped=1)\n"
    "Destroying test database for alias 'other'...\n"
)
DJANGO_FAILED_OUTPUT = "Ran 49 tests in 0.251s\n\nFAILED (failures=1)\n"


def _observation(*runs: CommandObservation) -> AgentMessage:
    return build_observation_message(
        WorkspaceObservation(changed_paths=frozenset(), command_runs=tuple(runs))
    )


def _run(
    command: str,
    *,
    returncode: int = 0,
    output_tail: str = DJANGO_WRITER_OUTPUT,
    timed_out: bool = False,
) -> CommandObservation:
    return CommandObservation(
        command=command, returncode=returncode, output_tail=output_tail, timed_out=timed_out
    )


def _supports(claim: str, *runs: CommandObservation, task_cwd: str | None = None) -> bool:
    return _runtime_messages_support_test_claim(
        value=claim,
        backed_commands=(),
        messages=(_observation(*runs),),
        task_cwd=task_cwd,
    )


class TestClaimForm:
    @pytest.mark.parametrize(
        ("claim", "label"),
        [
            ("migrations", "migrations"),
            ("migrations.test_writer", "migrations.test_writer"),
            ("migrations.test_writer (49 tests)", "migrations.test_writer"),
            ("migrations (578 tests)", "migrations"),
            ("migrations.test_writer (1 test)", "migrations.test_writer"),
            ("migrations.test_writer(49 tests)", "migrations.test_writer"),
            (
                "migrations.test_writer.WriterTests.test_serialize_fields",
                "migrations.test_writer.WriterTests.test_serialize_fields",
            ),
        ],
    )
    def test_labels_are_recognized(self, claim: str, label: str) -> None:
        assert _dotted_test_label_claim(claim) == label

    @pytest.mark.parametrize(
        "claim",
        [
            # the count annotation is stripped only when trailing and parenthesised
            "(49 tests) migrations.test_writer",
            "migrations.test_writer (49 tests) foo",
            "migrations.test_writer 49 tests",
            "migrations.test_writer [49 tests]",
            "migrations.test_writer (49 tests",
            "migrations.test_writer (tests)",
            # not dotted labels
            "tests/migrations/test_writer.py",
            "test_writer.py",
            "tests/test_x.py::test_a",
            "migrations.test_writer passed",
            "migrations..test_writer",
            "",
        ],
    )
    def test_non_labels_are_not_recognized(self, claim: str) -> None:
        assert _dotted_test_label_claim(claim) is None


class TestRunnerLabels:
    @pytest.mark.parametrize(
        ("command", "labels"),
        [
            ("python tests/runtests.py migrations.test_writer", ("migrations.test_writer",)),
            (
                "python tests/runtests.py migrations.test_writer --verbosity=0",
                ("migrations.test_writer",),
            ),
            ("python3 tests/runtests.py -v 2 migrations auth_tests", ("migrations", "auth_tests")),
            ("python tests/runtests.py --settings test_sqlite migrations", ("migrations",)),
            ("python tests/runtests.py --settings=test_sqlite migrations", ("migrations",)),
            ("./tests/runtests.py --parallel 1 migrations", ("migrations",)),
            ("cd tests && python runtests.py migrations.test_writer", ("migrations.test_writer",)),
            ("PYTHONPATH=. python tests/runtests.py migrations", ("migrations",)),
            ("python manage.py test app.tests", ("app.tests",)),
            ("django-admin test app.tests.T", ("app.tests.T",)),
            ("python -m django test app.tests", ("app.tests",)),
            ("python -m unittest tests.test_x.TestC.test_m", ("tests.test_x.TestC.test_m",)),
            ("python -m unittest -v tests.test_x", ("tests.test_x",)),
        ],
    )
    def test_runner_labels(self, command: str, labels: tuple[str, ...]) -> None:
        assert _unittest_style_runner_labels(command) == labels

    @pytest.mark.parametrize(
        "command",
        [
            # no label: a whole-suite run corroborates no specific label
            "python tests/runtests.py",
            "python -m unittest",
            "python -m unittest discover -s tests",
            # options that narrow the selection, or that this parser does not know
            "python tests/runtests.py migrations -k test_serialize",
            "python tests/runtests.py migrations --tag slow",
            "python tests/runtests.py --start-at migrations.test_writer migrations",
            "python tests/runtests.py --exclude-tag=slow migrations",
            "python tests/runtests.py --unknown-flag migrations",
            # output-filter pipelines and other shell syntax
            "python tests/runtests.py migrations | tail -5",
            "python tests/runtests.py migrations 2>&1",
            "python tests/runtests.py migrations; true",
            # not unittest-style label runners
            "pytest migrations",
            "python -m pytest migrations.test_writer",
            "bin/test sympy.core",
            "python bin/test sympy/core",
            "tox -e py38",
        ],
    )
    def test_no_labels(self, command: str) -> None:
        assert _unittest_style_runner_labels(command) == ()


class TestLabelCorroboration:
    def test_equal_label_is_covered(self) -> None:
        run = _run("python tests/runtests.py migrations.test_writer")
        assert _supports("migrations.test_writer", run)
        assert _supports("migrations.test_writer (49 tests)", run)

    @pytest.mark.parametrize(
        "command",
        [
            "python manage.py test migrations",
            "django-admin test migrations",
            "python -m django test migrations",
            "python -m unittest migrations",
            "cd tests && python runtests.py migrations",
        ],
    )
    def test_each_label_runner_covers(self, command: str) -> None:
        assert _supports("migrations.test_writer.WriterTests.test_x", _run(command))

    def test_ancestor_label_covers_descendants(self) -> None:
        module_run = _run("python tests/runtests.py migrations.test_writer")
        app_run = _run("python tests/runtests.py migrations", output_tail=DJANGO_MIGRATIONS_OUTPUT)

        assert _supports("migrations.test_writer.WriterTests.test_x", module_run)
        assert _supports("migrations.test_writer.WriterTests", module_run)
        assert _supports("migrations.test_writer.WriterTests.test_x", app_run)
        assert _supports("migrations.test_autodetector", app_run)

    def test_descendant_run_does_not_cover_its_ancestor(self) -> None:
        run = _run("python tests/runtests.py migrations.test_writer.WriterTests.test_x")
        assert not _supports("migrations.test_writer", run)
        assert not _supports("migrations", run)
        assert not _supports("migrations.test_writer.WriterTests.test_y", run)

    @pytest.mark.parametrize(
        "claim",
        [
            "migrations.test_writer2",
            "migrations.test_writerTests",
            "migration",
            "auth_tests",
            "auth_tests.test_views.LoginTest.test_x",
            "test_writer",
        ],
    )
    def test_unrelated_label_is_not_covered(self, claim: str) -> None:
        run = _run("python tests/runtests.py migrations.test_writer")
        assert not _supports(claim, run)

    @pytest.mark.parametrize(
        "run_kwargs",
        [
            {"returncode": 1, "output_tail": DJANGO_FAILED_OUTPUT},
            # a non-zero exit is rejected even when the output tail looks clean
            {"returncode": 1},
            {"returncode": 2},
            {"timed_out": True},
            {"output_tail": ""},
            {"output_tail": "Ran 0 tests in 0.000s\n\nOK\n"},
        ],
    )
    def test_failed_or_unproven_run_does_not_cover(self, run_kwargs: dict[str, object]) -> None:
        run = _run("python tests/runtests.py migrations.test_writer", **run_kwargs)  # type: ignore[arg-type]
        assert not _supports("migrations.test_writer (49 tests)", run)
        assert not _supports("migrations.test_writer.WriterTests.test_x", run)

    def test_narrowed_or_non_label_runs_do_not_cover(self) -> None:
        for command in (
            "python tests/runtests.py migrations -k test_serialize",
            "python tests/runtests.py",
            "pytest migrations",
            "bin/test migrations",
        ):
            assert not _supports("migrations.test_writer", _run(command)), command

    def test_pytest_node_id_handling_is_unchanged(self, tmp_path: Path) -> None:
        # A node-id or file claim still needs this run's file evidence; a
        # runner label does not stand in for it.
        run = _run("python tests/runtests.py migrations")
        assert not _supports("tests/migrations/test_writer.py::WriterTests::test_x", run)
        assert not _supports("test_writer.py", run, task_cwd=str(tmp_path))


class TestDevClaimRegression:
    """Exact claim strings from the django__django-14580 dev artifacts."""

    RUNS = (
        _run("python tests/runtests.py migrations.test_writer"),
        _run("python tests/runtests.py migrations", output_tail=DJANGO_MIGRATIONS_OUTPUT),
    )

    @pytest.mark.parametrize(
        "claim",
        [
            "migrations.test_writer (49 tests)",
            "migrations (578 tests)",
            "migrations.test_writer.WriterTests.test_serialize_fields",
            "migrations.test_writer.WriterTests.test_serialize_nested_field",
        ],
    )
    def test_dev_claims_are_corroborated(self, claim: str) -> None:
        assert _supports(claim, *self.RUNS)

    def test_dev_claims_rejected_when_the_rerun_fails(self) -> None:
        failed = (
            _run(
                "python tests/runtests.py migrations.test_writer",
                returncode=1,
                output_tail=DJANGO_FAILED_OUTPUT,
            ),
            _run(
                "python tests/runtests.py migrations",
                returncode=1,
                output_tail=DJANGO_FAILED_OUTPUT,
            ),
        )
        for claim in (
            "migrations.test_writer (49 tests)",
            "migrations (578 tests)",
            "migrations.test_writer.WriterTests.test_serialize_fields",
        ):
            assert not _supports(claim, *failed), claim


class TestVerifierIntegration:
    def _verify(
        self, messages: list[AgentMessage], evidence: dict[str, object], cwd: Path
    ) -> object:
        return _verify_atomic_evidence_against_runtime_messages(
            messages=tuple(messages),
            typed_evidence=EvidenceRecord(data=evidence),
            ac_content="Fix the migration writer so the serialized file imports models",
            execution_profile=load_profile("code"),
            task_cwd=str(cwd),
            adapter_working_directory=str(cwd),
        )

    def _transcript(self, cwd: Path, command: str) -> list[AgentMessage]:
        target = cwd / "writer.py"
        target.write_text("MODELS = 'from django.db import models'\n", encoding="utf-8")
        return [
            AgentMessage(
                type="tool",
                content="Edit writer.py",
                tool_name="Edit",
                data={"tool_input": {"file_path": str(target)}, "tool_call_id": "e1"},
            ),
            AgentMessage(
                type="tool_result",
                content="",
                data={"subtype": "tool_result", "tool_call_id": "e1", "exit_code": 0},
            ),
            # a Claude-style Bash call: the transcript carries no exit status
            AgentMessage(
                type="tool",
                content=f"Bash: {command}",
                tool_name="Bash",
                data={"tool_input": {"command": command}, "tool_call_id": "b1"},
            ),
            AgentMessage(
                type="tool_result",
                content="",
                data={"subtype": "tool_result", "tool_call_id": "b1"},
            ),
            AgentMessage(type="result", content="done", data={"subtype": "success"}),
        ]

    def test_label_claim_passes_only_with_a_successful_rerun(self, tmp_path: Path) -> None:
        command = "python tests/runtests.py migrations.test_writer"
        evidence = {
            "files_touched": ["writer.py"],
            "commands_run": [command],
            "tests_passed": ["migrations.test_writer (49 tests)"],
        }
        messages = self._transcript(tmp_path, command)
        before = self._verify(messages, evidence, tmp_path)
        assert before.passed is False  # type: ignore[attr-defined]
        assert "tests_passed: migrations.test_writer (49 tests)" in before.reasons[0]  # type: ignore[attr-defined]

        failed = list(messages)
        insert_observation_message(
            failed,
            WorkspaceObservation(
                changed_paths=frozenset({"writer.py"}),
                command_runs=(_run(command, returncode=1, output_tail=DJANGO_FAILED_OUTPUT),),
            ),
        )
        rejected = self._verify(failed, evidence, tmp_path)
        assert rejected.passed is False  # type: ignore[attr-defined]
        assert "tests_passed: migrations.test_writer (49 tests)" in rejected.reasons[0]  # type: ignore[attr-defined]

        passed = list(messages)
        insert_observation_message(
            passed,
            WorkspaceObservation(
                changed_paths=frozenset({"writer.py"}), command_runs=(_run(command),)
            ),
        )
        accepted = self._verify(passed, evidence, tmp_path)
        assert accepted.passed is True, accepted.reasons  # type: ignore[attr-defined]
