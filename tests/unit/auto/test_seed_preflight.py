"""Deterministic Seed executability preflight.

The seed_2be2907edc07 post-mortem: a Seed reached RUN with verify scripts
that never existed (claimed in ``existing_dependencies``), an unbound
``$VAULT_PATH`` in every verify command, and brownfield context references
that were concepts ("Obsidian Vault"), not paths. The LLM QA judge passed
it. These tests pin the deterministic gate that catches each fabrication
class and phrases every finding as an open question, never a rewrite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.auto.seed_preflight import run_seed_preflight
from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    BrownfieldContext,
    ContextReference,
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)


def _seed(**overrides: object) -> Seed:
    base: dict[str, object] = {
        "goal": "Build a local CLI",
        "constraints": ("Use existing project patterns",),
        "acceptance_criteria": ("`habit list` prints stable stdout",),
        "ontology_schema": OntologySchema(
            name="CliTask",
            description="CLI task ontology",
            fields=(OntologyField(name="command", field_type="string", description="Command"),),
        ),
        "evaluation_principles": (
            EvaluationPrinciple(name="testability", description="Observable behavior"),
        ),
        "exit_conditions": (
            ExitCondition(
                name="verified",
                description="Checks pass",
                evaluation_criteria="All acceptance criteria pass",
            ),
        ),
        "metadata": SeedMetadata(ambiguity_score=0.12),
    }
    base.update(overrides)
    return Seed(**base)  # type: ignore[arg-type]


def test_plain_greenfield_seed_passes(tmp_path: Path) -> None:
    report = run_seed_preflight(_seed(), workspace_root=tmp_path)

    assert report.passed
    assert report.blocking_findings == ()
    # A description-only AC is an advisory open question, never a blocker.
    codes = {finding.code for finding in report.findings}
    assert codes == {"unverifiable_criterion"}
    assert any("What deterministic command" in q for q in report.open_questions)


def test_unresolved_context_reference_blocks(tmp_path: Path) -> None:
    seed = _seed(
        brownfield_context=BrownfieldContext(
            project_type="brownfield",
            context_references=(
                ContextReference(path="Obsidian Vault", role="primary", summary="notes"),
                ContextReference(path=str(tmp_path), role="reference", summary="real"),
            ),
        )
    )

    report = run_seed_preflight(seed, workspace_root=tmp_path)

    assert not report.passed
    blocking = report.blocking_findings
    assert [finding.code for finding in blocking] == ["context_reference_unresolved"]
    assert blocking[0].subject == "Obsidian Vault"
    assert "real, absolute path" in report.open_questions[0]


def test_claimed_missing_dependency_blocks(tmp_path: Path) -> None:
    seed = _seed(
        brownfield_context=BrownfieldContext(
            project_type="brownfield",
            existing_dependencies=(
                "InfraNodus",  # product name — never a file claim
                "scripts/verify_ontology_report.py",  # claimed but absent
            ),
        )
    )

    report = run_seed_preflight(seed, workspace_root=tmp_path)

    assert [finding.code for finding in report.blocking_findings] == ["claimed_dependency_missing"]
    assert report.blocking_findings[0].subject == "scripts/verify_ontology_report.py"


def test_claimed_dependency_that_exists_passes(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "verify.py").write_text("print('ok')\n")
    seed = _seed(
        brownfield_context=BrownfieldContext(
            project_type="brownfield",
            existing_dependencies=("scripts/verify.py",),
        )
    )

    report = run_seed_preflight(seed, workspace_root=tmp_path)

    assert report.passed
    assert all(finding.code != "claimed_dependency_missing" for finding in report.findings)


def test_unbound_env_var_blocks(tmp_path: Path) -> None:
    seed = _seed(
        acceptance_criteria=(
            AcceptanceCriterionSpec(
                description="Report satisfies readonly checks",
                verify_command='python3 check.py --vault "$VAULT_PATH" --home "$HOME"',
            ),
        )
    )

    report = run_seed_preflight(seed, workspace_root=tmp_path)

    blocking = list(report.blocking_findings)
    assert [finding.code for finding in blocking] == [
        "unbound_env_var",
        "verify_program_missing",
    ]
    # $HOME is host-bound and must not be flagged.
    assert blocking[0].subject == "$VAULT_PATH"


def test_command_bound_variables_are_not_flagged(tmp_path: Path) -> None:
    """Real-corpus false positive: ``sh -c "tmp=$(mktemp -d) && ... $tmp"``."""
    seed = _seed(
        acceptance_criteria=(
            AcceptanceCriterionSpec(
                description="Pipeline runs in an isolated temp dir",
                verify_command=(
                    'sh -c "tmp=$(mktemp -d) && cp organize.py \\"$tmp\\"/ '
                    '&& cd \\"$tmp\\" && python organize.py && echo OK"'
                ),
            ),
            AcceptanceCriterionSpec(
                description="Loop and defaulted variables are command-bound",
                verify_command=(
                    'sh -c \'for f in a b; do echo "$f"; done; echo "${OUT_DIR:-./out}"\''
                ),
            ),
        )
    )

    report = run_seed_preflight(seed, workspace_root=tmp_path)

    assert all(finding.code != "unbound_env_var" for finding in report.findings)


def test_verify_script_reference_is_advisory_not_blocking(tmp_path: Path) -> None:
    seed = _seed(
        acceptance_criteria=(
            AcceptanceCriterionSpec(
                description="Tests pass",
                verify_command="python -m pytest -q tests/test_cli.py",
            ),
        )
    )

    report = run_seed_preflight(seed, workspace_root=tmp_path)

    assert report.passed
    advisory = [finding for finding in report.findings if not finding.blocking]
    assert [finding.code for finding in advisory] == ["verify_script_unconfirmed"]
    assert advisory[0].subject == "tests/test_cli.py"


@pytest.mark.parametrize("runner", ("python", "python3.11", "python3.14", "python12.3.4"))
def test_missing_root_python_program_blocks(tmp_path: Path, runner: str) -> None:
    seed = _seed(
        acceptance_criteria=(
            AcceptanceCriterionSpec(
                description="Contract checker passes",
                verify_command=f"{runner} check.py",
            ),
        )
    )

    report = run_seed_preflight(seed, workspace_root=tmp_path)

    assert [finding.code for finding in report.blocking_findings] == ["verify_program_missing"]
    assert report.blocking_findings[0].subject == "check.py"


@pytest.mark.parametrize(
    ("command", "program"),
    (
        ("python -u check.py", "check.py"),
        ("/usr/bin/python3 check.py", "check.py"),
        ("python -X dev check.py", "check.py"),
        ("python -W error check.py", "check.py"),
        ("/usr/bin/python3 /definitely/missing/check.py", "/definitely/missing/check.py"),
        ('sh -c "python check.py"', "check.py"),
        ("bash -c ./verify", "verify"),
    ),
)
def test_missing_wrapped_verification_program_blocks(
    tmp_path: Path, command: str, program: str
) -> None:
    seed = _seed(
        acceptance_criteria=(
            AcceptanceCriterionSpec(description="Verifier passes", verify_command=command),
        )
    )

    report = run_seed_preflight(seed, workspace_root=tmp_path)

    assert [finding.code for finding in report.blocking_findings] == ["verify_program_missing"]
    assert report.blocking_findings[0].subject == program


def test_missing_extensionless_executable_blocks_unless_declared_artifact(
    tmp_path: Path,
) -> None:
    criterion = AcceptanceCriterionSpec(
        description="Custom verifier passes",
        verify_command="./verify",
    )
    blocked = run_seed_preflight(_seed(acceptance_criteria=(criterion,)), workspace_root=tmp_path)
    declared = run_seed_preflight(
        _seed(
            acceptance_criteria=(criterion.model_copy(update={"expected_artifacts": ("verify",)}),)
        ),
        workspace_root=tmp_path,
    )

    assert [finding.code for finding in blocked.blocking_findings] == ["verify_program_missing"]
    assert declared.passed


def test_verify_reference_declared_as_artifact_is_clean(tmp_path: Path) -> None:
    (tmp_path / "check.py").write_text("print('ok')\n", encoding="utf-8")
    seed = _seed(
        acceptance_criteria=(
            AcceptanceCriterionSpec(
                description="Report generated",
                verify_command="python check.py --report artifacts/report.json",
                expected_artifacts=("artifacts/report.json",),
            ),
        )
    )

    report = run_seed_preflight(seed, workspace_root=tmp_path)

    assert report.passed
    assert all(finding.code != "verify_script_unconfirmed" for finding in report.findings)


def test_shared_verify_command_is_advisory(tmp_path: Path) -> None:
    command = "python3 scripts/check.py --mode readonly"
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "check.py").write_text("print('ok')\n")
    seed = _seed(
        acceptance_criteria=(
            AcceptanceCriterionSpec(description="Happy path verified", verify_command=command),
            AcceptanceCriterionSpec(description="Failure path verified", verify_command=command),
        )
    )

    report = run_seed_preflight(seed, workspace_root=tmp_path)

    assert report.passed
    shared = [finding for finding in report.findings if finding.code == "shared_verify_command"]
    assert len(shared) == 1
    assert "share one verify command" in shared[0].question


def test_relative_paths_without_workspace_root_are_skipped() -> None:
    seed = _seed(
        brownfield_context=BrownfieldContext(
            project_type="brownfield",
            context_references=(
                ContextReference(path="Obsidian Vault", role="primary", summary="notes"),
            ),
            existing_dependencies=("scripts/verify.py",),
        )
    )

    report = run_seed_preflight(seed, workspace_root=None)

    # Undecidable is not fabrication: never block on a guess.
    assert report.passed


def test_absolute_missing_context_reference_blocks_without_workspace_root(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "definitely" / "missing"
    seed = _seed(
        brownfield_context=BrownfieldContext(
            project_type="brownfield",
            context_references=(
                ContextReference(path=str(missing), role="primary", summary="gone"),
            ),
        )
    )

    report = run_seed_preflight(seed, workspace_root=None)

    assert [finding.code for finding in report.blocking_findings] == [
        "context_reference_unresolved"
    ]


def test_failed_obsidian_seed_shape_is_blocked(tmp_path: Path) -> None:
    """Regression: the exact fabrication mix of seed_2be2907edc07 must block."""
    seed = _seed(
        brownfield_context=BrownfieldContext(
            project_type="brownfield",
            context_references=(
                ContextReference(path="Obsidian Vault", role="primary", summary="vault"),
                ContextReference(path="MacBook 환경", role="reference", summary="host"),
            ),
            existing_dependencies=(
                "Obsidian Vault",
                "InfraNodus",
                "scripts/verify_ontology_report.py",
                "scripts/validate_ontology_acceptance.py",
            ),
        ),
        acceptance_criteria=(
            AcceptanceCriterionSpec(
                description="읽기 전용 온톨로지 보고서가 기준을 충족한다",
                verify_command=(
                    'python3 scripts/verify_ontology_report.py --vault "$VAULT_PATH" '
                    "--report artifacts/ontology-report.json --mode readonly"
                ),
                expected_artifacts=("artifacts/ontology-report.json",),
            ),
            AcceptanceCriterionSpec(
                description="존재하지 않는 Vault를 안전하게 실패 처리한다",
                verify_command=(
                    'python3 scripts/verify_ontology_report.py --vault "$VAULT_PATH" '
                    "--report artifacts/ontology-report.json --mode readonly"
                ),
            ),
            AcceptanceCriterionSpec(
                description="동일 입력 재실행이 동일 해시를 생성한다",
                expected_artifacts=("artifacts/ontology-report.json",),
            ),
        ),
    )

    report = run_seed_preflight(seed, workspace_root=tmp_path)

    assert not report.passed
    codes = {finding.code for finding in report.blocking_findings}
    assert codes == {
        "context_reference_unresolved",
        "claimed_dependency_missing",
        "unbound_env_var",
    }
    # Blocking questions come first and cover every fabrication class.
    questions = report.open_questions
    assert any("Obsidian Vault" in question for question in questions)
    assert any("$VAULT_PATH" in question for question in questions)
    assert any("verify_ontology_report.py" in question for question in questions)
    # The shared verify command across the happy and failure path is surfaced.
    assert any("share one verify command" in question for question in questions)
