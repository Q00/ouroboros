"""Stage 1 refuses commands whose exit status cannot reflect the project.

A model-proposed ``pytest --version`` exits 0 over a suite whose only test is
``assert False``. Before this gate the detector persisted it and Stage 1
recorded an executed pass, so model judgment granted acceptance (#2450). The
tests drive the public paths: ``ensure_mechanical_toml`` (detector),
``build_mechanical_config`` (hand-authored or previously persisted toml, and
caller overrides) and ``EvaluationPipeline`` (the resulting verdict).

Every refused command below is one the validator accepted before this change
on the same fixture, so each case fails with the gate removed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
import shlex
from typing import Any

import pytest

from ouroboros.core.types import Result
from ouroboros.evaluation import EvaluationContext, EvaluationPipeline, PipelineConfig
from ouroboros.evaluation.detector import ensure_mechanical_toml, toml_path
from ouroboros.evaluation.languages import (
    _ALLOWED_EXECUTABLES,
    build_mechanical_config,
    non_verifying_reason,
)
from ouroboros.evaluation.models import MechanicalDisposition
from ouroboros.providers.base import CompletionResponse, UsageInfo


@dataclass
class _Adapter:
    proposal: dict[str, str]
    calls: list[Any] = field(default_factory=list)

    async def complete(self, messages: Any, config: Any) -> Result[CompletionResponse, Any]:
        self.calls.append(config)
        return Result.ok(
            CompletionResponse(
                content=json.dumps(self.proposal),
                model=config.model,
                usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        )


def _project(root: Path) -> Path:
    """One repo carrying every manifest the cases below need.

    Its only test fails, so any executed pass over it is a false pass.
    """
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1"\n'
        'dependencies = ["pytest", "ruff", "mypy", "black"]\n'
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_demo.py").write_text(
        "def test_feature():\n    assert False, 'feature missing'\n"
    )
    (root / "Makefile").write_text(".PHONY: test lint\ntest:\n\tpytest\nlint:\n\truff check\n")
    (root / "Cargo.toml").write_text('[package]\nname = "demo"\nversion = "0.1.0"\n')
    (root / "go.mod").write_text("module demo\n\ngo 1.22\n")
    (root / "pom.xml").write_text("<project></project>\n")
    (root / "build.gradle").write_text("plugins { id 'java' }\n")
    (root / "build.zig").write_text("")
    (root / "mix.exs").write_text("")
    (root / "demo.csproj").write_text("<Project></Project>\n")
    (root / "package.json").write_text(
        json.dumps(
            {
                "scripts": {"test": "jest", "lint": "eslint ."},
                "devDependencies": {"jest": "29", "eslint": "9"},
            }
        )
    )
    (root / "justfile").write_text("test:\n    pytest\n")
    return root


def _detect(root: Path, proposal: dict[str, str]) -> tuple[bool, _Adapter]:
    adapter = _Adapter(proposal)
    ok = asyncio.run(ensure_mechanical_toml(root, adapter, model="m"))
    return ok, adapter


# (key, command): accepted by the validator before this change, cannot fail.
REFUSED: list[tuple[str, str]] = [
    # the probe's set
    ("test", "pytest --version"),
    ("test", "pytest --collect-only"),
    ("test", "pytest --co -q"),
    ("test", "pytest --help"),
    ("lint", "ruff --version"),
    ("test", "python -m pytest --version"),
    ("test", "pytest --setup-plan"),
    # spellings of the same modes
    ("test", "pytest -V"),
    ("test", "pytest -qh"),
    ("test", "pytest --collect-only=1"),
    ("test", "pytest --fixtures"),
    ("test", "pytest --setup-only"),
    ("test", "pytest --markers"),
    ("test", "pytest -o addopts=--co"),
    ("test", "pytest -k go --co"),
    ("test", "uv run pytest --co"),
    ("test", "python -m pytest -V"),
    ("test", "python -W ignore -c pass -m pytest"),
    ("test", "python -m pytest.__main__ --co"),
    ("test", "python -m unittest --help"),
    ("test", "python -m unittest -h"),
    ("lint", "ruff check --exit-zero"),
    ("lint", "ruff check -e"),
    ("lint", "ruff -qV"),
    ("lint", "ruff check --show-files"),
    ("lint", "ruff version"),
    ("static", "mypy --version"),
    ("static", "mypy -Vq"),
    ("static", "mypy --vers"),
    ("static", "mypy -c x=1"),
    ("lint", "black -c x"),
    # make: dry run, question/touch, ignore errors, executor and file overrides
    ("test", "make -n test"),
    ("test", "make --dry test"),
    ("test", "make -kn test"),
    ("test", "make -t test"),
    ("test", "make -i test"),
    ("test", "make --ign test"),
    ("test", "make -o test test"),
    ("test", "make SHELL=true test"),
    ("test", "make MAKEFLAGS=n test"),
    ("test", "make --eval=.IGNORE: test"),
    ("test", "make -nj4 test"),
    # forwarded to a script body
    ("test", "npm test -- --listTests"),
    ("test", "npm test --script-shell true"),
    ("test", "npm test --config.script-shell=true"),
    ("test", "npx jest --listTests"),
    ("test", "npx jest --list-tests"),
    ("test", "just test --dry-run"),
    ("test", "tox -e py -- --co"),
    ("test", "tox -e py -x testenv.commands=true"),
    ("test", "tox list"),
    # compiled runners
    ("test", "cargo test --no-run"),
    ("test", "cargo test -- --list"),
    ("test", "cargo test --config target.x.runner=true"),
    ("test", "go test -c ./..."),
    ("test", "go test -list . ./..."),
    ("test", "go test --list=. ./..."),
    ("test", "go test -exec true ./..."),
    ("test", "go test -n ./..."),
    ("test", "mvn test -DskipTests"),
    ("test", "mvn test -Dmaven.test.skip=true"),
    ("test", "mvn verify -Dmaven.test.failure.ignore=true"),
    ("test", "mvn test --fail-never"),
    ("test", "mvn test -Dtest=Nothing -Dsurefire.failIfNoSpecifiedTests=false"),
    ("test", "mvn -v verify"),
    ("test", "gradle test --dry-run"),
    ("test", "gradle test -m"),
    ("test", "dotnet test --list-tests"),
    ("test", "zig build test -l"),
]

# Informational subcommands that the per-tool subcommand allowlists carried.
REFUSED_SUBCOMMANDS: list[tuple[str, str]] = [
    ("static", "uv tree"),
    ("build", "cargo metadata"),
    ("build", "cargo tree"),
    ("build", "cargo version"),
    ("build", "cargo verify-project"),
    ("build", "go list ./..."),
    ("build", "go version"),
    ("build", "go env"),
    ("build", "zig version"),
    ("build", "mvn help:help"),
    ("build", "mvn dependency:tree"),
    ("build", "gradle tasks"),
    ("build", "gradle help"),
    ("build", "gradle clean"),
    ("build", "mix help"),
    ("build", "dotnet list"),
    ("test", "python -m timeit pass"),
    ("lint", "python -m tabnanny tests"),
    ("build", "python -m json.tool package.json"),
]

# Over-rejection guards: real checks, some sharing a spelling with a refused
# mode in another tool, that must still be persisted.
ACCEPTED: list[tuple[str, str]] = [
    ("test", "pytest"),
    ("test", "pytest -q -x"),
    ("test", "pytest -n 4"),
    ("test", "pytest -v"),
    ("test", "python -m pytest -c pyproject.toml"),
    ("test", "uv run pytest -q"),
    ("lint", "ruff check ."),
    ("static", "mypy src"),
    ("lint", "black --check ."),
    ("test", "make test"),
    ("test", "npm test"),
    ("test", "npm test -- -v"),
    ("lint", "npx eslint -c eslint.config.js ."),
    ("test", "just test"),
    ("test", "tox -e py"),
    ("test", "tox -e py -- -x"),
    ("test", "cargo test"),
    ("build", "cargo test --no-run"),
    ("build", "go test -c ./..."),
    ("test", "go test ./..."),
    ("static", "go vet ./..."),
    ("test", "mvn -B -V verify"),
    ("build", "mvn -B verify -DskipTests"),
    ("test", "gradle test"),
    ("test", "dotnet test"),
    ("test", "zig build test"),
    ("test", "mix test"),
    ("test", "python -m unittest"),
    ("build", "python -m compileall -q tests"),
    # A value option's argument is data, not a program or a mode.
    ("test", "pytest -k npm -v"),
    ("test", "uv run pytest -k npm -v"),
    ("test", "pytest -m slow -q"),
    # Task runners run every positional: a reporting task beside a check is a check.
    ("test", "gradle clean test"),
    ("build", "gradle build -x test"),
]


@pytest.mark.parametrize(("key", "command"), REFUSED + REFUSED_SUBCOMMANDS)
def test_detector_refuses_a_command_that_cannot_fail(
    tmp_path: Path, key: str, command: str
) -> None:
    root = _project(tmp_path)
    ok, adapter = _detect(root, {key: command})
    assert len(adapter.calls) == 1
    assert ok is False
    assert not toml_path(root).exists()


@pytest.mark.parametrize(("key", "command"), ACCEPTED)
def test_detector_still_persists_a_real_check(tmp_path: Path, key: str, command: str) -> None:
    root = _project(tmp_path)
    ok, _ = _detect(root, {key: command})
    assert ok is True
    config = build_mechanical_config(root)
    assert getattr(config, f"{key}_command") == tuple(shlex.split(command))


def test_refused_command_leaves_siblings_intact(tmp_path: Path) -> None:
    root = _project(tmp_path)
    ok, _ = _detect(root, {"test": "pytest --collect-only", "lint": "ruff check ."})
    assert ok is True
    body = toml_path(root).read_text()
    assert 'lint = "ruff check ."' in body
    assert "collect-only" not in body
    config = build_mechanical_config(root)
    assert config.test_command is None
    assert config.lint_command == ("ruff", "check", ".")


@pytest.mark.parametrize("command", ["pytest --version", "pytest --co", "make -n test"])
def test_failing_project_is_not_approved(tmp_path: Path, command: str) -> None:
    """The probe's end-to-end shape: proposal -> persistence -> verdict.

    Before the gate this produced ``executed_pass`` and ``final_approved``.
    Stage 2/3 are off so the verdict is Stage 1's alone.
    """
    root = _project(tmp_path)
    ok, _ = _detect(root, {"test": command})
    assert ok is False
    assert not toml_path(root).exists()
    config = build_mechanical_config(root)
    assert config.test_command is None
    pipeline = EvaluationPipeline(
        llm_adapter=None,
        config=PipelineConfig(stage2_enabled=False, stage3_enabled=False, mechanical=config),
    )
    result = asyncio.run(
        pipeline.evaluate(
            EvaluationContext(
                execution_id="e", seed_id="s", current_ac="feature works", artifact="x"
            )
        )
    ).value
    assert result.stage1_result is not None
    assert result.stage1_result.disposition is MechanicalDisposition.NO_EVIDENCE
    assert result.stage1_result.executed_checks == ()
    assert result.final_approved is False
    assert result.acceptance_state.value == "unverified"


@pytest.mark.parametrize(("key", "command"), REFUSED[:12])
def test_hand_authored_or_previously_persisted_toml_is_refused(
    tmp_path: Path, key: str, command: str
) -> None:
    """A toml written by an older detector or by hand reads through the same gate."""
    root = _project(tmp_path)
    sibling, real = ("static", "mypy src") if key == "lint" else ("lint", "ruff check .")
    toml_path(root).parent.mkdir()
    toml_path(root).write_text(f"{key} = {json.dumps(command)}\n{sibling} = {json.dumps(real)}\n")
    config = build_mechanical_config(root)
    assert getattr(config, f"{key}_command") is None
    assert getattr(config, f"{sibling}_command") == tuple(real.split())


def test_caller_overrides_are_refused_too(tmp_path: Path) -> None:
    config = build_mechanical_config(
        tmp_path, overrides={"test": "pytest --version", "lint": "ruff check ."}
    )
    assert config.test_command is None
    assert config.lint_command == ("ruff", "check", ".")


@pytest.mark.parametrize(
    "command",
    [
        "go version",
        "go env",
        "cargo version",
        "cargo metadata",
        "python3 -m timeit -n 1 -r 1 pass",
        "python -m pydoc os",
    ],
)
def test_informational_subcommands_are_refused_on_the_override_path(
    tmp_path: Path, command: str
) -> None:
    """Overrides skip the detector, so the shared gate must know these too."""
    config = build_mechanical_config(tmp_path, overrides={"test": command})
    assert config.test_command is None


@pytest.mark.parametrize(("key", "command"), REFUSED_SUBCOMMANDS)
def test_reporting_subcommands_are_refused_on_the_override_path(
    tmp_path: Path, key: str, command: str
) -> None:
    """Every subcommand pruned from the detector allowlists is refused by the shared gate."""
    config = build_mechanical_config(tmp_path, overrides={key: command})
    assert getattr(config, f"{key}_command") is None


@pytest.mark.parametrize(
    "command",
    [
        "uv tree --offline",
        "mvn -q clean dependency:tree",
        "gradle clean tasks",
        "python -m pickletools x.pickle",
        "python -m zipfile -l a.zip",
        "python -m dis x.py",
        "python -m ast x.py",
    ],
)
def test_reporting_only_lines_are_refused_on_the_override_path(
    tmp_path: Path, command: str
) -> None:
    config = build_mechanical_config(tmp_path, overrides={"test": command})
    assert config.test_command is None


def test_a_value_option_does_not_hide_a_listing_flag() -> None:
    assert non_verifying_reason(shlex.split("pytest -k npm --co"), "test") is not None


def test_build_only_modes_depend_on_the_key() -> None:
    """Modes that still compile are build evidence and nothing else."""
    for command in ("cargo test --no-run", "go test -c ./...", "mvn verify -DskipTests"):
        parts = shlex.split(command)
        assert non_verifying_reason(parts, "build") is None
        for key in ("test", "coverage", "lint", "static", None):
            assert non_verifying_reason(parts, key) is not None


@pytest.mark.parametrize("executable", sorted(_ALLOWED_EXECUTABLES))
def test_every_allowlisted_executable_refuses_version_and_help(executable: str) -> None:
    for flag in ("--version", "--help"):
        assert non_verifying_reason([executable, flag]) == flag
        assert non_verifying_reason([executable, f"{flag}=x"]) == flag


def test_an_executable_without_a_reviewed_entry_is_refused() -> None:
    assert non_verifying_reason(["unreviewed-tool", "check"]) is not None
