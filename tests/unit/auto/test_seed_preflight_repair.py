"""Repository-evidence repair of Seed preflight findings."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from ouroboros.auto.seed_preflight import (
    _command_program_tokens,
    _command_test_runner_operands,
    run_seed_preflight,
)
from ouroboros.auto.seed_preflight_repair import (
    DECLARED_ACTION,
    RELOCATED_ACTION,
    list_workspace_files,
    locate_program_candidates,
    repair_seed_preflight,
)
from ouroboros.core.seed import (
    MAX_AC_SUCCESS_CONTRACT_ARTIFACTS,
    AcceptanceCriterionSpec,
    BrownfieldContext,
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)


def _seed(*commands: str) -> Seed:
    return _spec_seed(
        *(
            AcceptanceCriterionSpec(description=f"Check {index} passes", verify_command=command)
            for index, command in enumerate(commands)
        )
    )


def _spec_seed(
    *criteria: AcceptanceCriterionSpec, existing_dependencies: tuple[str, ...] = ()
) -> Seed:
    return Seed(
        goal="Fix migration imports",
        constraints=("Use existing project patterns",),
        acceptance_criteria=criteria,
        brownfield_context=BrownfieldContext(existing_dependencies=existing_dependencies),
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
    assert full.kept_rounds == 2
    assert {repair.action for repair in full.repairs} == {RELOCATED_ACTION}
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


# --- #3058: test files the run creates, named through ``node --test`` -------


def _empty_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


def _artifacts(seed: Seed) -> list[tuple[str, ...]]:
    return [
        criterion.expected_artifacts
        for criterion in seed.acceptance_criteria
        if isinstance(criterion, AcceptanceCriterionSpec)
    ]


@pytest.mark.parametrize(
    ("command", "operands"),
    [
        ("node --test test/galaxy-scene.test.js", {"test/galaxy-scene.test.js"}),
        ("node --test ./test/galaxy-scene.test.js", {"test/galaxy-scene.test.js"}),
        ("timeout 60 node --test test/a.test.js", {"test/a.test.js"}),
        ("node --import tsx --test test/a.test.ts", {"test/a.test.ts"}),
        ("npm run build && node --test test/a.test.js", {"test/a.test.js"}),
        ("node test/a.test.js", set()),
        ("node scripts/verify.js --test", set()),
        ("node --test", set()),
        ("python3 tests/test_a.py", set()),
        ("node --test 'unterminated", set()),
    ],
)
def test_test_runner_operands_are_node_test_programs_only(command: str, operands: set[str]) -> None:
    assert _command_test_runner_operands(command) == operands
    # Collecting the test-runner operands never changes the program set.
    collected: set[str] = set()
    assert _command_program_tokens(command, test_runner_operands=collected) == (
        _command_program_tokens(command)
    )


def test_node_test_files_in_an_empty_repo_are_declared_as_expected_artifacts(
    tmp_path: Path,
) -> None:
    """The #3058 Seed: each criterion runs a test file the run has yet to write."""
    workspace = _empty_repo(tmp_path / "galaxy")
    seed = _spec_seed(
        AcceptanceCriterionSpec(
            description="The scene renders 20,000 particles in spiral arms",
            verify_command="node --test test/galaxy-scene.test.js",
            expected_artifacts=("index.html",),
        ),
        AcceptanceCriterionSpec(
            description="Dragging orbits the camera",
            verify_command="node --test ./test/orbit-controls.test.js",
        ),
        AcceptanceCriterionSpec(
            description="Scrolling zooms the camera",
            verify_command="node --test test/zoom.test.js",
            verify_cwd=".",
        ),
    )
    before = run_seed_preflight(seed, workspace_root=workspace)
    assert [finding.code for finding in before.blocking_findings] == ["verify_program_missing"] * 3

    outcome = repair_seed_preflight(seed, workspace, max_rounds=5)

    assert outcome.report.passed
    assert _commands(outcome.seed) == _commands(seed)
    assert _artifacts(outcome.seed) == [
        ("index.html", "test/galaxy-scene.test.js"),
        ("test/orbit-controls.test.js",),
        ("test/zoom.test.js",),
    ]
    assert [
        (repair.action, repair.subject, repair.evidence_path) for repair in outcome.repairs
    ] == [
        (DECLARED_ACTION, "test/galaxy-scene.test.js", None),
        (DECLARED_ACTION, "test/orbit-controls.test.js", None),
        (DECLARED_ACTION, "test/zoom.test.js", None),
    ]
    assert (
        outcome.repairs[0].commands_before
        == outcome.repairs[0].commands_after
        == ("node --test test/galaxy-scene.test.js",)
    )
    assert outcome.kept_rounds == 1
    assert outcome.seed.metadata.parent_seed_id == seed.metadata.seed_id


def test_all_declarations_share_one_repair_round(tmp_path: Path) -> None:
    """More criteria than budget rounds: the declarations still fit in one round."""
    workspace = _empty_repo(tmp_path / "galaxy")
    seed = _seed(*(f"node --test test/feature-{index}.test.js" for index in range(7)))

    outcome = repair_seed_preflight(seed, workspace, max_rounds=1)

    assert outcome.report.passed
    assert len(outcome.repairs) == 7
    assert outcome.rounds == outcome.kept_rounds == 1


def test_one_criterion_declares_a_test_file_shared_by_two(tmp_path: Path) -> None:
    workspace = _empty_repo(tmp_path / "galaxy")
    seed = _seed(
        "node --test test/galaxy.test.js",
        "node --test --test-name-pattern=arms test/galaxy.test.js",
    )

    outcome = repair_seed_preflight(seed, workspace, max_rounds=5)

    assert outcome.report.passed
    assert _artifacts(outcome.seed) == [("test/galaxy.test.js",), ("test/galaxy.test.js",)]
    assert len(outcome.repairs) == 1
    assert len(outcome.repairs[0].commands_after) == 2


@pytest.mark.parametrize(
    "command",
    [
        "node scripts/verify-galaxy.js",
        "node test/galaxy-scene.test.js",
        "./scripts/check.sh",
        "python3 tools/verify.py",
    ],
)
def test_invented_program_not_run_by_node_test_still_blocks(tmp_path: Path, command: str) -> None:
    workspace = _empty_repo(tmp_path / "galaxy")

    outcome = repair_seed_preflight(_seed(command), workspace, max_rounds=5)

    assert not outcome.changed
    assert [finding.code for finding in outcome.report.blocking_findings] == [
        "verify_program_missing"
    ]


def test_invented_existing_code_path_blocks_while_test_files_are_declared(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "app"
    _touch(workspace / "src" / "app.js")
    seed = _seed("node --test test/app.test.js", "node src/server.js --self-check")

    outcome = repair_seed_preflight(seed, workspace, max_rounds=5)

    assert [repair.subject for repair in outcome.repairs] == ["test/app.test.js"]
    assert [finding.subject for finding in outcome.report.blocking_findings] == ["src/server.js"]
    assert _artifacts(outcome.seed) == [("test/app.test.js",), ()]


def test_test_file_the_seed_claims_already_exists_is_not_declared(tmp_path: Path) -> None:
    """Declaring a claimed path would silence the claim check, which must still block.

    The ``cd`` keeps the program finding apart from the claim: declaring the
    root path clears ``claimed_dependency_missing`` alone, which is still
    "fewer blocking findings", so only the claim guard stops it.
    """
    workspace = _empty_repo(tmp_path / "app")
    seed = _spec_seed(
        AcceptanceCriterionSpec(
            description="Existing suite passes",
            verify_command="cd web && node --test test/legacy.test.js",
        ),
        existing_dependencies=("test/legacy.test.js",),
    )

    outcome = repair_seed_preflight(seed, workspace, max_rounds=5)

    assert not outcome.changed
    assert sorted(finding.code for finding in outcome.report.blocking_findings) == [
        "claimed_dependency_missing",
        "verify_program_missing",
    ]


def test_test_file_kept_elsewhere_in_the_checkout_is_relocated_not_declared(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "app"
    _touch(workspace / "web" / "test" / "galaxy.test.js")

    outcome = repair_seed_preflight(
        _seed("node --test test/galaxy.test.js"), workspace, max_rounds=5
    )

    assert outcome.report.passed
    assert [repair.action for repair in outcome.repairs] == [RELOCATED_ACTION]
    assert _commands(outcome.seed) == ["node --test web/test/galaxy.test.js"]
    assert _artifacts(outcome.seed) == [()]


def test_test_file_with_several_checkout_candidates_is_not_declared(tmp_path: Path) -> None:
    workspace = tmp_path / "app"
    _touch(workspace / "a" / "test" / "galaxy.test.js")
    _touch(workspace / "b" / "test" / "galaxy.test.js")

    outcome = repair_seed_preflight(
        _seed("node --test test/galaxy.test.js"), workspace, max_rounds=5
    )

    assert not outcome.changed
    assert not outcome.report.passed


@pytest.mark.parametrize(
    "command",
    ["node --test ../shared/galaxy.test.js", "node --test /opt/suite/galaxy.test.js"],
)
def test_test_file_outside_the_workspace_is_not_declared(tmp_path: Path, command: str) -> None:
    workspace = _empty_repo(tmp_path / "app")

    outcome = repair_seed_preflight(_seed(command), workspace, max_rounds=5)

    assert not outcome.changed
    assert not outcome.report.passed


def test_criterion_with_its_own_verify_cwd_is_not_declared(tmp_path: Path) -> None:
    """Preflight resolves the path from the root; ``app/`` would never satisfy it."""
    workspace = _empty_repo(tmp_path / "repo")
    seed = _spec_seed(
        AcceptanceCriterionSpec(
            description="App tests pass",
            verify_command="node --test test/app.test.js",
            verify_cwd="app",
        )
    )

    outcome = repair_seed_preflight(seed, workspace, max_rounds=5)

    assert not outcome.changed
    assert not outcome.report.passed


def test_declaration_that_does_not_resolve_its_finding_is_dropped_alone(tmp_path: Path) -> None:
    """``cd app`` moves the runner; declaring the root path proves nothing, so only it is dropped."""
    workspace = _empty_repo(tmp_path / "repo")
    seed = _seed("cd app && node --test test/app.test.js", "node --test test/root.test.js")

    outcome = repair_seed_preflight(seed, workspace, max_rounds=5)

    assert [repair.subject for repair in outcome.repairs] == ["test/root.test.js"]
    assert [finding.subject for finding in outcome.report.blocking_findings] == ["test/app.test.js"]
    assert _artifacts(outcome.seed) == [(), ("test/root.test.js",)]


def test_declaration_over_the_artifact_limit_is_not_made(tmp_path: Path) -> None:
    workspace = _empty_repo(tmp_path / "repo")
    full = tuple(f"out/{index}.txt" for index in range(MAX_AC_SUCCESS_CONTRACT_ARTIFACTS))
    seed = _spec_seed(
        AcceptanceCriterionSpec(
            description="Generator writes every file",
            verify_command="node --test test/generator.test.js",
            expected_artifacts=full,
        )
    )

    outcome = repair_seed_preflight(seed, workspace, max_rounds=5)

    assert not outcome.changed
    assert not outcome.report.passed
