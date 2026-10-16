"""Repository-evidence repair of Seed preflight findings."""

from __future__ import annotations

from pathlib import Path
import subprocess

from ouroboros.auto.seed_preflight import run_seed_preflight
from ouroboros.auto.seed_preflight_repair import (
    list_workspace_files,
    locate_program_candidates,
    repair_seed_preflight,
)
from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)


def _seed(*commands: str) -> Seed:
    return Seed(
        goal="Fix migration imports",
        constraints=("Use existing project patterns",),
        acceptance_criteria=tuple(
            AcceptanceCriterionSpec(description=f"Check {index} passes", verify_command=command)
            for index, command in enumerate(commands)
        ),
        ontology_schema=OntologySchema(
            name="Task",
            description="Task ontology",
            fields=(OntologyField(name="command", field_type="string", description="Command"),),
        ),
        evaluation_principles=(EvaluationPrinciple(name="testability", description="Observable"),),
        exit_conditions=(
            ExitCondition(name="verified", description="Checks pass", evaluation_criteria="Pass"),
        ),
        metadata=SeedMetadata(ambiguity_score=0.1),
    )


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")


def _commands(seed: Seed) -> list[str | None]:
    return [
        criterion.verify_command
        for criterion in seed.acceptance_criteria
        if isinstance(criterion, AcceptanceCriterionSpec)
    ]


def test_missing_root_script_is_rewritten_to_its_checkout_location(tmp_path: Path) -> None:
    _touch(tmp_path / "tests" / "runtests.py")
    seed = _seed("./runtests.py migrations.test_writer", "python runtests.py migrations")
    assert not run_seed_preflight(seed, workspace_root=tmp_path).passed

    outcome = repair_seed_preflight(seed, tmp_path, max_rounds=5)

    assert outcome.report.passed
    assert _commands(outcome.seed) == [
        "./tests/runtests.py migrations.test_writer",
        "python tests/runtests.py migrations",
    ]
    assert outcome.seed.metadata.parent_seed_id == seed.metadata.seed_id
    assert outcome.seed.metadata.seed_id != seed.metadata.seed_id
    assert [repair.evidence_path for repair in outcome.repairs] == ["tests/runtests.py"]


def test_rewrite_leaves_arguments_and_redirection_targets_intact(tmp_path: Path) -> None:
    _touch(tmp_path / "tests" / "runtests.py")
    original = (tmp_path / "tests" / "runtests.py").read_text(encoding="utf-8")
    seed = _seed(
        "./runtests.py > runtests.py",
        "python runtests.py runtests.py",
        "./runtests.py a && cat runtests.py",
    )

    outcome = repair_seed_preflight(seed, tmp_path, max_rounds=5)

    assert outcome.report.passed
    assert _commands(outcome.seed) == [
        "./tests/runtests.py > runtests.py",
        "python tests/runtests.py runtests.py",
        "./tests/runtests.py a && cat runtests.py",
    ]
    assert (tmp_path / "tests" / "runtests.py").read_text(encoding="utf-8") == original


def test_script_absent_from_checkout_stays_blocking(tmp_path: Path) -> None:
    seed = _seed("./runtests.py migrations.test_writer")

    outcome = repair_seed_preflight(seed, tmp_path, max_rounds=5)

    assert not outcome.changed
    assert outcome.seed is seed
    assert [finding.code for finding in outcome.report.blocking_findings] == [
        "verify_program_missing"
    ]


def test_several_candidates_do_not_decide(tmp_path: Path) -> None:
    _touch(tmp_path / "a" / "runtests.py")
    _touch(tmp_path / "b" / "runtests.py")

    outcome = repair_seed_preflight(_seed("./runtests.py"), tmp_path, max_rounds=5)

    assert not outcome.changed
    assert not outcome.report.passed


def test_zero_budget_repairs_nothing(tmp_path: Path) -> None:
    _touch(tmp_path / "tests" / "runtests.py")

    outcome = repair_seed_preflight(_seed("./runtests.py"), tmp_path, max_rounds=0)

    assert not outcome.changed
    assert not outcome.report.passed


def test_budget_bounds_rounds_one_repair_per_round(tmp_path: Path) -> None:
    _touch(tmp_path / "tests" / "runtests.py")
    _touch(tmp_path / "tools" / "lint.sh")
    seed = _seed("./runtests.py", "./lint.sh")

    limited = repair_seed_preflight(seed, tmp_path, max_rounds=1)
    assert len(limited.repairs) == 1
    assert limited.rounds == 1
    assert not limited.report.passed

    full = repair_seed_preflight(seed, tmp_path, max_rounds=5)
    assert len(full.repairs) == 2
    assert full.report.passed


def test_suffix_match_requires_whole_path_components(tmp_path: Path) -> None:
    _touch(tmp_path / "pkg" / "scripts" / "run.sh")
    _touch(tmp_path / "pkg" / "other" / "run.sh")
    _touch(tmp_path / "myruntests.py")
    listed = list_workspace_files(tmp_path)
    assert listed is not None

    assert locate_program_candidates("scripts/run.sh", tmp_path, listed) == ("pkg/scripts/run.sh",)
    assert locate_program_candidates("runtests.py", tmp_path, listed) == ()
    assert locate_program_candidates("../run.sh", tmp_path, listed) == ()


def test_walk_skips_hidden_directories(tmp_path: Path) -> None:
    _touch(tmp_path / ".venv" / "bin" / "runtests.py")
    _touch(tmp_path / "tests" / "runtests.py")
    listed = list_workspace_files(tmp_path)
    assert listed is not None

    assert locate_program_candidates("runtests.py", tmp_path, listed) == ("tests/runtests.py",)


def test_git_listing_honors_ignore_rules(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")
    _touch(tmp_path / "build" / "runtests.py")
    _touch(tmp_path / "tests" / "runtests.py")
    listed = list_workspace_files(tmp_path)
    assert listed is not None

    assert locate_program_candidates("runtests.py", tmp_path, listed) == ("tests/runtests.py",)
