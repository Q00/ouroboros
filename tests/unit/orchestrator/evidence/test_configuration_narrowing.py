"""Configuration that changes what a run
imports or selects never lets that run back a test-target claim.

A command that assigns ``PYTHONPATH``, ``DJANGO_SETTINGS_MODULE``,
``NODE_OPTIONS`` (and the rest of ``NARROWING_ENVIRONMENT``), or passes a
runner's configuration or selection option (``jest -c``, ``manage.py test
--settings``, ``go test -tags``, ...), may run the named test against other
code or not at all. Plain runs keep their link.
"""

from __future__ import annotations

import os
from pathlib import Path
import shlex

import pytest

from ouroboros.orchestrator.evidence.command_replay import (
    claim_links_to_command,
    replay_candidate,
    replay_commands,
)
from ouroboros.orchestrator.evidence.replay_policy import (
    NARROWING_ENVIRONMENT,
    alters_configuration,
    claim_target_operands,
    excludes_tests,
    narrowing_variable,
)
from ouroboros.orchestrator.evidence.shell_parsing import (
    _output_filter_pipeline_is_pipefail_protected,
)
from tests.unit.orchestrator.evidence.test_command_replay import (
    DJANGO_WRITER_CLAIM,
    PYTHON_BIN,
    _bash_result,
    _dispatch_and_verify,
    _edit,
    _evidence,
    _executable,
    _ran,
    _transcript_only_verdict,
    _workspace,
)

# ``test_add`` passes only against ``stubs/calc.py``; the workspace's
# ``calc.py`` is wrong.
STUB_TESTS = (
    "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n\n"
    "def test_other():\n    assert True\n"
)


DJANGO_OK_RUNNER = (
    "import os, sys\n"
    "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
    "from calc import add\n"
    "ok = add(2, 3) == 5\n"
    "print('Found 49 test(s).')\n"
    "print('-' * 70)\n"
    "print('Ran 49 tests in 0.214s')\n"
    "print()\n"
    "print('OK' if ok else 'FAILED (failures=1)')\n"
    "sys.exit(0 if ok else 1)\n"
)


def _split(command: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(argv, assigned names)``: leading assignments leave the argv
    as they do for a replay candidate's ``env_delta``."""
    parts = shlex.split(command)
    names: list[str] = []
    while parts and "=" in parts[0] and parts[0].partition("=")[0].isidentifier():
        names.append(parts.pop(0).partition("=")[0])
    return tuple(parts), tuple(names)


def _operands(command: str, environment: tuple[str, ...] = ()) -> frozenset[str]:
    argv, names = _split(command)
    return claim_target_operands(argv, (*environment, *names))


def _excludes(command: str) -> bool:
    return excludes_tests(*_split(command))


def _configuration(command: str) -> bool:
    return alters_configuration(*_split(command))


def _stub_workspace(root: Path, *, correct: bool = False) -> Path:
    workspace = _workspace(root, correct=correct)
    (workspace / "stubs").mkdir()
    (workspace / "stubs" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (workspace / "tests" / "test_bad.py").write_text(STUB_TESTS, encoding="utf-8")
    return workspace


class TestNarrowingEnvironment:
    @pytest.mark.parametrize("name", sorted(NARROWING_ENVIRONMENT))
    def test_each_listed_variable_disables_the_target(self, name: str) -> None:
        command = f"{name}=x pytest tests/test_bad.py"

        assert _operands(command) == frozenset()
        assert _operands(f"env {name}=x pytest tests/test_bad.py") == frozenset()
        assert _operands("pytest tests/test_bad.py", (name,)) == frozenset()

    @pytest.mark.parametrize(
        "command",
        [
            "JEST_JUNIT_OUTPUT_DIR=x jest tests/a.test.js",
            "VITEST_POOL_ID=1 vitest run tests/a.test.ts",
            "timeout 60 env NODE_OPTIONS=--require=./stub.js jest tests/a.test.js",
            "RUBYLIB=stubs rspec spec/a_spec.rb",
            "GOFLAGS=-tags=fake go test ./pkg",
            "DJANGO_SETTINGS_MODULE=alt python tests/runtests.py migrations",
        ],
    )
    def test_runner_family_variables_disable_the_target(self, command: str) -> None:
        assert _operands(command) == frozenset()
        assert _configuration(command)

    def test_prefixes_and_names(self) -> None:
        assert narrowing_variable("JEST_ANYTHING")
        assert narrowing_variable("VITEST_ANYTHING")
        assert narrowing_variable("PYTHONPATH")
        assert not narrowing_variable("FOO")
        assert not narrowing_variable("PYTHONDONTWRITEBYTECODE")

    def test_other_assignments_keep_the_target(self) -> None:
        assert _operands("FOO=1 pytest tests/test_bad.py") == {"tests/test_bad.py"}

    def test_an_export_in_the_transcript_command_disables_the_link(self) -> None:
        argv = ("pytest", "tests/test_bad.py")

        assert not claim_links_to_command(
            "tests/test_bad.py",
            transcript_command="export PYTHONPATH=stubs && pytest tests/test_bad.py",
            core_command="pytest tests/test_bad.py",
            argv=argv,
        )
        assert claim_links_to_command(
            "tests/test_bad.py",
            transcript_command="pytest tests/test_bad.py",
            core_command="pytest tests/test_bad.py",
            argv=argv,
        )


class TestRunnerOptions:
    @pytest.mark.parametrize(
        ("command", "target"),
        [
            ("python -m pytest -q tests/test_bad.py", "tests/test_bad.py"),
            ("jest tests/a.test.js", "tests/a.test.js"),
            ("npx jest tests/a.test.js", "tests/a.test.js"),
            ("vitest run tests/a.test.ts", "tests/a.test.ts"),
            ("mocha test/a.spec.js", "test/a.spec.js"),
            ("phpunit tests/FooTest.php", "tests/FooTest.php"),
            ("rspec spec/a_spec.rb", "spec/a_spec.rb"),
            ("go test -count=1 -v ./pkg", "./pkg"),
            ("python tests/runtests.py migrations", "migrations"),
            ("python tests/runtests.py --verbosity 2 migrations", "migrations"),
            ("python manage.py test app.tests", "app.tests"),
            # Options at the runner's documented default, or that only set
            # the number of processes, do not narrow: Django's
            # tests/runtests.py defaults DJANGO_SETTINGS_MODULE to test_sqlite.
            (
                "./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1 "
                "migrations.test_writer",
                "migrations.test_writer",
            ),
            ("python tests/runtests.py --settings test_sqlite migrations", "migrations"),
            ("tests/runtests.py --settings=test_sqlite migrations", "migrations"),
            ("python tests/runtests.py --parallel 4 migrations", "migrations"),
            ("python tests/runtests.py --parallel auto migrations", "migrations"),
            ("python -m pytest -n 4 tests/test_bad.py", "tests/test_bad.py"),
        ],
    )
    def test_plain_runs_keep_their_target(self, command: str, target: str) -> None:
        assert target in _operands(command)

    def test_the_default_settings_hold_after_a_cd_into_tests(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")
        command = "cd tests && python runtests.py --settings=test_sqlite --parallel 1 migrations"
        candidate = replay_candidate(command, str(workspace))
        assert candidate is not None

        assert claim_links_to_command(
            "migrations",
            transcript_command=candidate.transcript_command,
            core_command=candidate.core_command,
            argv=candidate.argv,
            environment=tuple(candidate.env_delta),
        )

    @pytest.mark.parametrize(
        "command",
        [
            # jest and vitest
            "jest -c alt.config.js tests/a.test.js",
            "jest -calt.config.js tests/a.test.js",
            "npx jest --config alt.js tests/a.test.js",
            "jest --testPathIgnorePatterns a tests/a.test.js",
            "jest --test-name-pattern x tests/a.test.js",
            "jest -t x tests/a.test.js",
            "jest --selectProjects a tests/a.test.js",
            "jest --shard=1/2 tests/a.test.js",
            "jest --onlyChanged tests/a.test.js",
            "jest --changedSince main tests/a.test.js",
            "jest --passWithNoTests tests/a.test.js",
            "vitest run --config v.config.ts tests/a.test.ts",
            "vitest run -c v.config.ts tests/a.test.ts",
            # mocha
            "mocha --config alt.yml test/a.spec.js",
            "mocha --grep x test/a.spec.js",
            "mocha -g x test/a.spec.js",
            "mocha --invert --grep x test/a.spec.js",
            "mocha --ignore test/a.spec.js test",
            "mocha --exclude test/a.spec.js test",
            "mocha --file setup.js test/a.spec.js",
            "mocha -r stub test/a.spec.js",
            # phpunit
            "phpunit --configuration alt.xml tests/FooTest.php",
            "phpunit -c alt.xml tests/FooTest.php",
            "phpunit --filter x tests/FooTest.php",
            "phpunit --group slow tests/FooTest.php",
            "phpunit --exclude-group slow tests/FooTest.php",
            "phpunit --testsuite unit tests/FooTest.php",
            # rspec
            "rspec --options alt spec/a_spec.rb",
            "rspec -O alt spec/a_spec.rb",
            "rspec -e x spec/a_spec.rb",
            "rspec --example x spec/a_spec.rb",
            "rspec -t slow spec/a_spec.rb",
            "rspec --tag slow spec/a_spec.rb",
            "rspec --pattern x spec/a_spec.rb",
            "rspec --exclude-pattern x spec/a_spec.rb",
            "rspec -Istubs spec/a_spec.rb",
            # go test
            "go test -run TestX ./pkg",
            "go test --run=TestX ./pkg",
            "go test -skip TestX ./pkg",
            "go test -tags=integration ./pkg",
            "go test -short ./pkg",
            "go test -exec true ./pkg",
            # Django: runtests.py, manage.py test, django-admin
            "python manage.py test app --settings=alt",
            "python manage.py test --settings alt app",
            "python tests/runtests.py --settings=other migrations",
            "python tests/runtests.py --settings=tests.test_sqlite migrations",
            "python manage.py test app --settings=test_sqlite",
            "python tests/runtests.py --settings=test_sqlite -k writer migrations",
            "python tests/runtests.py --settings=test_sqlite --tag slow migrations",
            "python tests/runtests.py --settings=test_sqlite --exclude-tag slow migrations",
            "python tests/runtests.py --pythonpath stubs migrations",
            "python -m django test app.tests --settings=proj.settings",
            # Python interpreter flags that change sys.path
            "python -P -m pytest tests/test_bad.py",
            "python -I -m pytest tests/test_bad.py",
        ],
    )
    def test_configuration_and_selection_options_disable_the_target(self, command: str) -> None:
        assert _operands(command) == frozenset()
        assert _excludes(command)

    @pytest.mark.parametrize(
        "command",
        [
            "cargo test foo",
            "cargo test -- foo",
            "cargo test -- --skip foo",
            "cargo test -- --exact tests::foo",
            "cargo test --features x",
            "cargo test --no-default-features",
            "cargo test --lib",
            "cargo test --bins",
            "cargo test --tests",
            "cargo test --test integration",
            "cargo test --unknown-flag",
            "mvn test -Dtest=FooTest",
            "mvn -D test=FooTest test",
            "mvn test -Pci",
            "gradle test --tests com.example.FooTest",
            "./gradlew test -Pci",
        ],
    )
    def test_build_runner_options_narrow(self, command: str) -> None:
        assert _excludes(command)

    @pytest.mark.parametrize(
        "command",
        [
            "cargo test",
            "cargo test --release",
            "cargo test -j 4 --workspace",
            "cargo test -- --nocapture",
            "mvn test",
            "mvn -q verify",
            "gradle test",
        ],
    )
    def test_plain_build_runs_do_not_narrow(self, command: str) -> None:
        assert not _excludes(command)

    @pytest.mark.parametrize(
        ("command", "configuration"),
        [
            ("pytest -k slow tests/test_bad.py", False),
            ("gradle test --tests com.example.FooTest", False),
            ("mvn test -Dtest=FooTest", False),
            ("go test -run TestX ./pkg", False),
            ("jest -t x tests/a.test.js", False),
            ("pytest -c alt.ini tests/test_bad.py", True),
            ("jest -c alt.config.js tests/a.test.js", True),
            ("mvn test -Pci", True),
            ("go test -tags=integration ./pkg", True),
            ("python manage.py test app --settings=alt", True),
            ("PYTHONPATH=stubs pytest tests/test_bad.py", True),
        ],
    )
    def test_configuration_is_told_apart_from_selection(
        self, command: str, configuration: bool
    ) -> None:
        # Selection may still be backed by output naming the claimed test;
        # configuration never is (``_test_command_targets_claim``).
        assert _excludes(command)
        assert _configuration(command) is configuration


class TestEndToEnd:
    @pytest.fixture(autouse=True)
    def _path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", os.pathsep.join([PYTHON_BIN, "/usr/bin", "/bin"]))

    async def test_a_pythonpath_stub_run_is_rejected(self, tmp_path: Path) -> None:
        # The replay passes against ``stubs/calc.py``, while the
        # workspace's ``calc.py`` is wrong.
        workspace = _stub_workspace(tmp_path / "ws")
        command = f"PYTHONPATH=stubs {PYTHON_BIN}/pytest -q -p no:cacheprovider tests/test_bad.py"

        verdict, observation = await _dispatch_and_verify(
            workspace,
            _ran(command, "c1", exit_code=0),
            {
                "files_touched": ["calc.py"],
                "commands_run": [command],
                "tests_passed": ["tests/test_bad.py"],
            },
        )

        # The claim no longer links to the run, so nothing replayed backs it.
        assert all(run.succeeded for run in observation.command_runs)
        assert verdict.passed is False

    async def test_the_plain_run_still_backs_its_file(self, tmp_path: Path) -> None:
        workspace = _stub_workspace(tmp_path / "ws", correct=True)
        command = "python -m pytest -q -p no:cacheprovider tests/test_bad.py"

        verdict, observation = await _dispatch_and_verify(
            workspace,
            (*_edit(workspace / "tests" / "test_bad.py", "e2"), *_ran(command, "c1", exit_code=0)),
            _evidence(command, ["tests/test_bad.py"]),
        )

        assert [run.succeeded for run in observation.command_runs] == [True]
        assert verdict.passed is True, verdict.reasons

    @pytest.mark.parametrize(
        "command",
        [
            "python tests/runtests.py --settings=alt migrations.test_writer",
            "python tests/runtests.py --settings=alt --parallel 1 migrations",
        ],
    )
    async def test_django_settings_run_does_not_back_the_label(
        self, tmp_path: Path, command: str
    ) -> None:
        # The runner passes, but under settings the command chose: the
        # label claim is not corroborated.
        workspace = _workspace(tmp_path / "ws")

        verdict, observation = await _dispatch_and_verify(
            workspace, _ran(command, "c1", exit_code=0), _evidence(command, [DJANGO_WRITER_CLAIM])
        )

        assert observation.command_runs
        assert all(run.succeeded for run in observation.command_runs)
        assert verdict.passed is False

    @pytest.mark.parametrize(
        ("command", "correct", "passed"),
        [
            # The SWE-bench form, naming the claimed label itself.
            (
                "python tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1 "
                "migrations.test_writer",
                True,
                True,
            ),
            # An ancestor label covers the claimed one.
            (
                "python tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1 "
                "migrations",
                True,
                True,
            ),
            (
                "python tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1 "
                "migrations.test_writer",
                False,
                False,
            ),
        ],
    )
    async def test_swe_bench_django_command_backs_the_label(
        self, tmp_path: Path, command: str, correct: bool, passed: bool
    ) -> None:
        workspace = _workspace(tmp_path / "ws", correct=correct)
        # Django's success footer, printed only when the check passes, so the
        # ancestor-label rule has runner output to read.
        (workspace / "tests" / "runtests.py").write_text(DJANGO_OK_RUNNER, encoding="utf-8")

        verdict, _ = await _dispatch_and_verify(
            workspace, _ran(command, "c1", exit_code=0), _evidence(command, [DJANGO_WRITER_CLAIM])
        )

        assert verdict.passed is passed, verdict.reasons

    @pytest.mark.parametrize(
        "command",
        [
            "PYTHONPATH=stubs pytest -v tests/test_bad.py",
            "export PYTHONPATH=stubs && pytest -v tests/test_bad.py",
            "pytest -v -c alt.ini tests/test_bad.py",
        ],
    )
    def test_transcript_only_output_naming_the_file_does_not_back_it(
        self, tmp_path: Path, command: str
    ) -> None:
        workspace = _stub_workspace(tmp_path / "ws")
        output = "tests/test_bad.py::test_add PASSED\ntests/test_bad.py::test_other PASSED\n"
        transcript = (
            *_edit(workspace / "tests" / "test_bad.py", "e2"),
            *_ran(command, "c1", exit_code=0)[:1],
            _bash_result("c1", exit_code=0, output=output + "2 passed in 0.01s"),
        )

        verdict = _transcript_only_verdict(
            workspace, transcript, _evidence(command, ["tests/test_bad.py"])
        )

        assert verdict.passed is False

    def test_transcript_only_plain_run_naming_the_file_backs_it(self, tmp_path: Path) -> None:
        workspace = _stub_workspace(tmp_path / "ws", correct=True)
        command = "pytest -v tests/test_bad.py"
        output = "tests/test_bad.py::test_add PASSED\ntests/test_bad.py::test_other PASSED\n"
        transcript = (
            *_edit(workspace / "tests" / "test_bad.py", "e2"),
            *_ran(command, "c1", exit_code=0)[:1],
            _bash_result("c1", exit_code=0, output=output + "2 passed in 0.01s"),
        )

        verdict = _transcript_only_verdict(
            workspace, transcript, _evidence(command, ["tests/test_bad.py"])
        )

        assert verdict.passed is True, verdict.reasons

    @pytest.mark.parametrize(("exported", "passed"), [(True, False), (False, True)])
    def test_an_export_in_an_earlier_call_disables_transcript_only_proof(
        self, tmp_path: Path, exported: bool, passed: bool
    ) -> None:
        # A runtime's shell may keep the export for the later call.
        workspace = _stub_workspace(tmp_path / "ws", correct=True)
        command = "python -m pytest -q tests/test_bad.py"
        setup = "export PYTHONPATH=stubs" if exported else "export FOO=1"
        transcript = (
            *_edit(workspace / "tests" / "test_bad.py", "e2"),
            *_ran(setup, "c0", exit_code=0),
            *_ran(command, "c1", exit_code=0)[:1],
            _bash_result("c1", exit_code=0, output="2 passed in 0.01s"),
        )

        verdict = _transcript_only_verdict(
            workspace, transcript, _evidence(command, ["tests/test_bad.py"])
        )

        assert verdict.passed is passed, verdict.reasons

    async def test_replay_scrubs_and_records_inherited_narrowing_variables(
        self, tmp_path: Path
    ) -> None:
        """The replay environment is an allowlist; narrowing names are recorded."""
        workspace = _workspace(tmp_path / "ws")
        _executable(
            workspace / "check_env.sh",
            "#!/bin/sh\n"
            'test -z "${PYTHONPATH+x}${DJANGO_SETTINGS_MODULE+x}${NODE_OPTIONS+x}'
            '${JEST_CONFIG+x}${GOFLAGS+x}" || exit 7\n'
            'test -z "${UNLISTED+x}" || exit 8\n'
            'test "$LANG" = C.UTF-8\n',
        )
        candidate = replay_candidate("./check_env.sh", str(workspace))
        assert candidate is not None
        inherited = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "UNLISTED": "1",
            "PYTHONPATH": "stubs",
            "DJANGO_SETTINGS_MODULE": "alt",
            "NODE_OPTIONS": "--require=./stub.js",
            "JEST_CONFIG": "alt.js",
            "GOFLAGS": "-tags=fake",
        }

        runs = await replay_commands(
            (candidate,), workspace=str(workspace), env=inherited, timeout_seconds=30
        )

        assert runs[0].returncode == 0, runs[0].output_tail
        assert runs[0].scrubbed_environment == (
            "DJANGO_SETTINGS_MODULE",
            "GOFLAGS",
            "JEST_CONFIG",
            "NODE_OPTIONS",
            "PYTHONPATH",
        )


@pytest.mark.parametrize(
    ("command", "protected"),
    [
        ("set -o pipefail; set +o pipefail; pytest | tail -5", False),
        ("set -o pipefail && set +euo pipefail && pytest | tail -5", False),
        ("set -o pipefail; pytest | tail -5", True),
        ("set +o pipefail; set -o pipefail; pytest | tail -5", True),
    ],
)
def test_a_later_set_plus_o_pipefail_removes_the_protection(command: str, protected: bool) -> None:
    assert _output_filter_pipeline_is_pipefail_protected(command) is protected
